"""Dedicated news choice; fixed server endpoints, no implicit provider fallback."""

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from .model_catalog import cloud_catalog, cloud_model_allowed
from .models import UserSettings

MODEL_CACHE = {}


def model_options(db, user, config, refresh=False, transport=None):
    from .personal_connections import external_config

    config = external_config(db, user, config)
    settings = db.scalar(
        select(UserSettings).where(UserSettings.user_id == user.id, UserSettings.tenant_id == user.tenant_id)
    )
    value = settings.value if settings else {}
    profile = value.get("analysis_model", {})
    provider = profile.get("provider", "vllm")
    default_id = (
        value.get("cloud_model") if provider == "openai" else profile.get("model_id")
    ) or config.vllm_model_id
    ids = {config.vllm_model_id} if config.vllm_model_id else set()
    if provider == "vllm" and default_id:
        ids.add(default_id)
    error = None
    if refresh and config.vllm_base_url:
        try:
            with httpx.Client(
                timeout=5, follow_redirects=False, trust_env=False, transport=transport
            ) as client:
                with client.stream(
                    "GET",
                    config.vllm_base_url.rstrip("/") + "/models",
                    headers={"Authorization": "Bearer " + config.vllm_api_key.get_secret_value()},
                ) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 1_000_000:
                            raise ValueError
                    import json

                    ids.update(
                        item["id"]
                        for item in json.loads(raw).get("data", [])[:100]
                        if isinstance(item.get("id"), str) and 0 < len(item["id"]) <= 120
                    )
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            error = "로컬 모델 목록 연결 실패 · 서버 설정 모델만 표시합니다."
    # Process-local discovery cache; choices persist in DB, URLs never come from clients.
    own = db.get(UserSettings, user.id)
    key = (user.id, config.vllm_base_url)
    discovered = MODEL_CACHE.get(key, [])
    if not refresh:
        ids.update(discovered)
    elif not error:
        MODEL_CACHE[key] = sorted(ids)
    items = [
        {"id": "vllm:" + model, "provider": "vllm", "model": model, "label": "로컬 · " + model}
        for model in sorted(ids)
    ]
    items.extend(
        {
            "id": "openai:" + model,
            "provider": "openai",
            "model": model,
            "label": "클라우드 · " + model + " (유료·외부 전송)",
        }
        for model in cloud_catalog(config)["ids"]
        if config.openai_api_key.get_secret_value()
    )
    default = next(
        (item for item in items if item["provider"] == provider and item["model"] == default_id), None
    )
    if not default and provider == "openai" and cloud_model_allowed(config, default_id, transport):
        default = {
            "id": "openai:" + default_id,
            "provider": "openai",
            "model": default_id,
            "label": "클라우드 · " + default_id + " (유료·외부 전송)",
        }
        if default not in items:
            items.append(default)
    return {
        "selected": own.value.get("news_model", "default") if own else "default",
        "cloud_enabled": bool(own and own.value.get("cloud_enabled")),
        "default": default,
        "items": items,
        "error": error,
        "default_source": "같은 테넌트의 최초 관리자 분석 모델 설정 · 미설정 시 서버 기본",
    }


def resolve_model(db, user, config, choice, cloud_confirmed=False):
    options = model_options(db, user, config)
    choice = choice or options["selected"]
    model = (
        options["default"]
        if choice == "default"
        else next((item for item in options["items"] if item["id"] == choice), None)
    )
    if (
        not model
        and choice.startswith("openai:")
        and cloud_model_allowed(config, choice.removeprefix("openai:"))
    ):
        model = {"provider": "openai", "model": choice.removeprefix("openai:")}
    if not model:
        raise HTTPException(409, "선택 모델이 허용 목록에 없습니다. 모델 목록을 새로고침하세요.")
    own = db.get(UserSettings, user.id)
    if model["provider"] == "openai" and not (own and own.value.get("cloud_enabled")):
        raise HTTPException(409, "설정의 클라우드 사용을 먼저 허용하세요. 자동으로 설정을 변경하지 않습니다.")
    if model["provider"] == "openai" and not cloud_confirmed:
        raise HTTPException(409, "클라우드 모델의 비용·외부 전송 확인이 필요합니다.")
    return model
