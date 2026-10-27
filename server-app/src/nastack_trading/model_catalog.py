"""Read-only account model discovery. Selection never enables paid inference."""

import hashlib
import json
import re
from time import monotonic

import httpx

CACHE = {}


def gpt_at_least_56(model):
    match = re.match(r"^gpt-(\d+)(?:\.(\d+))?(?:-|$)", model)
    return bool(match and (int(match[1]), int(match[2] or 0)) >= (5, 6))


def cloud_catalog(config, transport=None, refresh=False, discover=False):
    secret = config.openai_api_key.get_secret_value()
    key = hashlib.sha256(secret.encode()).hexdigest()
    cached = CACHE.get(key)
    error = None
    if secret and (refresh or (discover and (not cached or monotonic() - cached["time"] > cached["ttl"]))):
        try:
            with httpx.Client(
                timeout=5, follow_redirects=False, trust_env=False, transport=transport
            ) as client:
                with client.stream(
                    "GET", "https://api.openai.com/v1/models", headers={"Authorization": "Bearer " + secret}
                ) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 1_000_000:
                            raise ValueError
            ids = sorted(
                {
                    item["id"]
                    for item in json.loads(raw)["data"]
                    if isinstance(item.get("id"), str)
                    and len(item["id"]) <= 120
                    and gpt_at_least_56(item["id"])
                },
                reverse=True,
            )
            cached = {"ids": ids, "time": monotonic(), "ttl": 300, "error": None}
            CACHE[key] = cached
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            error = "OpenAI 모델 목록 조회 실패 · 기존 목록을 유지합니다."
            cached = {"ids": cached["ids"] if cached else [], "time": monotonic(), "ttl": 30, "error": error}
            CACHE[key] = cached
    ids = (
        sorted(set(config.cloud_model_allowlist) | set(cached["ids"] if cached else []), reverse=True)
        if secret
        else []
    )
    return {
        "ids": ids,
        "discovered": cached["ids"] if cached else [],
        "error": error or (cached.get("error") if cached else None),
        "note": "계정 API 모델 목록의 GPT 5.6 이상 및 기존 서버 허용 모델. 목록 조회는 추론이 아니며, 모델별 Responses 지원·용도·이용 권한은 실행 시 별도 검증됩니다.",
    }


def cloud_model_allowed(config, model, transport=None):
    if model in config.cloud_model_allowlist:
        return True
    return gpt_at_least_56(model) and model in cloud_catalog(config, transport, discover=True)["discovered"]
