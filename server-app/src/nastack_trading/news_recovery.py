"""Explicit recovery of a saved news run, pinned to its original model and evidence."""

import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select

from .db import now
from .models import CollectionWorkflow, NewsArticle, NewsRun, PersonalSource, User
from .news import (
    PROMPT_VERSION,
    NewsRequest,
    article_signature,
    capability,
    collect_news,
    prompt_policy,
    resolve_model,
    stock_context,
)
from .personal_connections import external_config
from .security import audit


def resume_news_run(db, user, run_id, config, *, cloud_confirmed=False, transport=None):
    """No search/planning/settings writes. Caller must explicitly approve original-model use."""
    owner_id, tenant_id = user.id, user.tenant_id
    user = db.get(User, owner_id, populate_existing=True, with_for_update=True)
    if not user or user.tenant_id != tenant_id or not user.active or user.role == "viewer":
        raise HTTPException(403, "수집·평가 권한이 없습니다.")
    original = db.get(NewsRun, run_id)
    if not original or original.owner_id != owner_id or original.tenant_id != tenant_id:
        raise HTTPException(404, "본인의 평가 이력이 아닙니다.")
    saved = original.value
    code = (saved.get("instrument") or {}).get("code")
    if not code or original.status not in {"partial", "model_failed"} or not saved.get("pending_articles"):
        raise HTTPException(409, "미평가가 있는 개인 종목 이력만 복구할 수 있습니다.")
    choice = saved.get("provider", "vllm") + ":" + saved["model"]
    scoped = external_config(db, user, config)
    secret = (
        scoped.openai_api_key if saved.get("provider") == "openai" else scoped.vllm_api_key
    ).get_secret_value()
    ids = set(saved["article_ids"])
    rows = list(
        db.scalars(
            select(NewsArticle).where(
                NewsArticle.owner_id == owner_id, NewsArticle.tenant_id == tenant_id, NewsArticle.id.in_(ids)
            )
        )
    )
    if len(rows) != len(ids) or any(
        saved.get("article_signatures", {}).get(a.id) != article_signature(a.value) for a in rows
    ):
        raise HTTPException(409, "원문이 변경되어 기존 평가와 그대로 이어 처리할 수 없습니다.")

    def checkpoint():
        db.expire_all()
        current = db.get(User, owner_id, populate_existing=True)
        if not current or current.tenant_id != tenant_id or not current.active or current.role == "viewer":
            raise HTTPException(403, "평가 권한이 변경되었습니다.")
        current_config = external_config(db, current, config)
        resolve_model(db, current, current_config, choice, cloud_confirmed)
        endpoint_signature = hashlib.sha256(
            (
                saved.get("provider", "vllm")
                + ":"
                + (
                    current_config.vllm_base_url
                    if saved.get("provider", "vllm") == "vllm"
                    else "https://api.openai.com/v1/responses"
                )
            ).encode()
        ).hexdigest()
        if endpoint_signature != saved.get("endpoint_signature"):
            raise HTTPException(409, "모델 연결 주소가 변경되었습니다.")
        current_secret = (
            current_config.openai_api_key
            if saved.get("provider") == "openai"
            else current_config.vllm_api_key
        ).get_secret_value()
        if current_secret != secret or (saved.get("provider") == "openai" and not current_secret):
            raise HTTPException(409, "개인 모델 연결이 변경되었습니다.")
        context = stock_context(db, current, code)
        signature = hashlib.sha256(
            json.dumps(
                {"instrument": context, "prompt_sha256": prompt_policy(db, current)["sha256"]},
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if signature != saved["context_signature"] or saved.get("prompt_version") != PROMPT_VERSION:
            raise HTTPException(409, "사업정보·평가 지침이 변경되어 이전 평가와 혼합할 수 없습니다.")
        for snapshot in saved["sources"]:
            source = db.get(PersonalSource, snapshot["id"], populate_existing=True)
            if (
                not source
                or source.owner_id != owner_id
                or source.tenant_id != tenant_id
                or source.version != snapshot["version"]
                or not capability(source)["eligible"]
            ):
                raise HTTPException(409, "원래 평가의 출처 승인이 변경되었습니다.")
        active = db.scalar(
            select(CollectionWorkflow.code).where(
                CollectionWorkflow.owner_id == owner_id,
                CollectionWorkflow.tenant_id == tenant_id,
                CollectionWorkflow.status.in_(["running", "queued"]),
            )
        )
        if active:
            raise HTTPException(409, "기존 수집 작업이 종료된 후 복구하세요.")

    checkpoint()
    audit(
        db,
        user,
        "news.explicit_recovery_started",
        run_id,
        {"code": code, "model": saved["model"], "pending": saved["pending_articles"], "search": False},
    )
    db.commit()
    result = collect_news(
        db,
        user,
        NewsRequest(
            code=code,
            source_ids=[s["id"] for s in saved["sources"]],
            model=choice,
            cloud_confirmed=cloud_confirmed,
            stored_only=True,
            max_articles=min(30, saved["pending_articles"]),
        ),
        config,
        transport,
        article_ids=ids,
        incremental=True,
        checkpoint=checkpoint,
    )
    checkpoint()
    result.value = {**result.value, "recovered_from": run_id}
    workflow = db.get(CollectionWorkflow, (owner_id, code), with_for_update=True)
    if workflow and workflow.tenant_id == tenant_id:
        pending = result.value.get("pending_articles", 0)
        workflow.status = "failed" if result.value.get("error") else "partial" if pending else "completed"
        # Deliberately differs from the currently selected model's workflow key.
        # A later normal refresh must recheck model/plan policy, not mislabel this run.
        workflow.value = {
            **workflow.value,
            "signature": "original-model-recovery:" + result.id,
            "evaluation_signature": None,
            "news_run_id": result.id,
            "evaluation_model": saved["model"],
            "pending": pending,
            "error": result.value.get("error"),
            "evaluation_diagnostics": result.value.get("evaluation_diagnostics"),
            "counts": {**workflow.value.get("counts", {}), "materials": len(result.value["article_ids"])},
            "stage": "기존 모델·저장 원문으로 미평가 복구 · 개인 모델 설정 유지",
        }
        workflow.updated_at = now()
    audit(
        db,
        user,
        "news.explicit_recovery_finished",
        result.id,
        {
            "original_run": run_id,
            "model": saved["model"],
            "pending": result.value.get("pending_articles"),
            "status": result.status,
        },
    )
    db.commit()
    return result
