"""Owner-scoped task instructions; output and execution safeguards are immutable."""

import hashlib
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from .db import now
from .models import AuditEvent, UserSettings
from .security import audit

Task = Literal[
    "business",
    "attributes",
    "collection_plan",
    "statistics_evaluation",
    "news",
    "translation",
    "research",
    "connection_test",
]
TASKS = {
    "collection_plan": {
        "name": "사업 기반 수집 계획",
        "purpose": "저장된 사업설명에서 직접·간접 수요·공급 경로를 찾아 승인된 뉴스·공시 출처와 연결된 통계 항목을 선택합니다.",
        "trigger": "종목 상세 증분 갱신 시 계획이 없거나 사업설명·지침·모델·출처 정책이 바뀐 경우",
        "model_source": "내 기본 분석 모델",
        "default": "제공된 사업설명을 근거로 관련 자료의 수집 계획을 작성하세요. 기업 자체 공시와 산업 수요·공급·원가·규제의 간접 경로를 구분하세요. 제공된 승인 출처와 연결 통계 ID만 선택하고, 뉴스 검색어에는 구체적인 사업 주제를 포함하세요. 관련성이 없는 통계를 억지로 고르지 마세요. 제공되지 않은 출처·통계는 실행하지 말고 gaps에 필요한 자료와 한계를 기록하세요. ETF는 제조기업이 아니며 기초지수·편입산업과 레버리지 위험을 구분하세요.",
    },
    "statistics_evaluation": {
        "name": "사업 관련 통계 해석",
        "purpose": "수집된 원천 통계의 관측값·주기를 보존해 해당 사업과의 관련성·불확실성을 해석합니다. 뉴스 합계와 분리합니다.",
        "trigger": "수집 계획의 통계 관측값·사업설명·평가 지침·모델이 바뀌었거나 해석이 없는 경우",
        "model_source": "뉴스 화면에서 선택한 전용 모델",
        "default": "제공된 원천 통계 관측값과 사업설명만으로 선택 종목에 대한 긍정·부정·판단 보류를 설명하세요. 월간·연간 관측기간을 발표일이나 거래일로 오인하지 마세요. 발표 시각·노출 경로가 확인되지 않으면 불확실성을 명시하고 근거 없는 인과나 가격 전망을 만들지 마세요. 수치를 재작성하거나 보간하지 마세요. 통계 점수는 뉴스 영향 점수 합계와 별개인 참고 해석입니다.",
    },
    "business": {
        "name": "사업 설명 인터넷 조사",
        "purpose": "공식 공시·회사 자료의 검색 발췌를 바탕으로 제품·수요처·투입요소·사업 불확실성을 정리합니다.",
        "trigger": "명시적 인터넷 조사, 또는 별도 자동 웹조사 동의 시 신규 종목의 벡터 초기 차원 제안 전",
        "model_source": "내 기본 분석 모델",
        "default": "제공된 인터넷 검색 근거에서 선택 기업의 주요 사업, 제품과 서비스, 최종 수요처, 주요 투입요소를 한국어로 정리하세요. 기업명과 종목 코드를 대조하고 동명이인과 계열사를 혼동하지 마세요. 고객사·매출비중·계약·생산능력은 명시된 근거가 있을 때만 포함하고, 오래된 자료나 확인 불가한 내용은 uncertainties에 남기세요. 각 설명에 근거 ID와 실제 원문 인용을 붙이세요.",
    },
    "attributes": {
        "name": "기본 종목 속성 제안",
        "purpose": "출처가 있는 사업 설명에서 등록된 섹터·원자재·시장 차원을 최대 8개 제안합니다.",
        "trigger": "기본 속성 자동 생성 허용 시 신규 관심·보유종목 등록, 또는 명시적 초기화·재시도",
        "model_source": "내 기본 분석 모델 · 자동 호출은 기존 허용 설정 적용",
        "default": "출처가 있는 business_context의 사업 설명을 근거로 초기 탐색용 차원을 제안하세요. 종목명만으로 사업을 추정하지 마세요. 관련 섹터와 원가·수요 경로를 검토하고 시장지수만으로 완료하지 마세요. ETF는 원지수·원자재의 대체 지표일 수 있습니다. 검증된 상관관계나 추천으로 표현하지 말고, 확신이 없으면 빈 목록과 부족한 정보를 반환하세요.",
    },
    "news": {
        "name": "뉴스 영향 평가",
        "purpose": "수집된 기사의 사업 영향을 -4~+4 또는 판단 보류로 평가하고 이유·인용을 남깁니다.",
        "trigger": "종목의 뉴스 수집·평가 실행 · 동일 조건의 저장 평가는 재사용",
        "model_source": "뉴스 화면에서 선택한 전용 모델",
    },
    "translation": {
        "name": "뉴스 한국어 번역·요약",
        "purpose": "외국어 제목·발췌를 한국어로 번역합니다. 영향 점수는 바꾸지 않습니다.",
        "trigger": "뉴스 화면의 한글 번역 버튼 · 프롬프트 변경 후 명시적 실행 시 다시 번역",
        "model_source": "뉴스 화면에서 선택한 전용 모델",
        "default": "각 기사의 title과 excerpt를 한국어로 충실하게 번역하고 3문장 이내로 요약하세요. 숫자·시점·지역·전망과 사실의 구분을 보존하고, 종목 영향이나 투자 의견을 추가하지 마세요. 발췌에 없는 내용은 만들지 말고 제목만 있는 자료는 그 한계를 그대로 유지하세요.",
    },
    "research": {
        "name": "근거 기반 연구 보고서",
        "purpose": "검색 근거를 요약하고 동향·위험을 작성합니다. 서버 연구 기능이 활성화된 경우에만 실행됩니다.",
        "trigger": "별도 연구 작업 요청 후 워커 실행 · 저장한 당시 프롬프트 사용",
        "model_source": "연구 요청 당시 내 기본 분석 모델",
        "default": "제공된 종목 연구 근거를 한국어로 요약하세요. 사실과 전망을 구분하고, 서로 다른 출처의 모순과 자료 부족을 명시하세요. 근거로 지지되는 동향과 위험만 설명하고, 현재 주가나 수익을 만들어내거나 주문 지시를 작성하지 마세요. 참고 전용 출처는 결론을 뒷받침하는 근거로 사용하지 마세요.",
    },
    "connection_test": {
        "name": "모델 연결 테스트",
        "purpose": "짧은 요청으로 실제 모델 응답 여부를 확인합니다. 분석 작업은 수행하지 않습니다.",
        "trigger": "사용자 설정의 LLM 연결 테스트 버튼",
        "model_source": "내 기본 분석 모델",
        "default": "연결 상태를 확인하기 위한 짧은 테스트입니다. 분석·검색·추론 결과를 작성하지 말고 OK라는 단어로만 간단히 응답하세요.",
    },
}


def task_policy(db, user, task):
    if task == "news":
        from .news_prompt import prompt_policy

        return prompt_policy(db, user)
    row = db.scalar(
        select(UserSettings)
        .where(UserSettings.user_id == user.id, UserSettings.tenant_id == user.tenant_id)
        .execution_options(populate_existing=True)
    )
    saved = (row.value.get("llm_task_prompts", {}).get(task) or {}) if row else {}
    text = saved.get("text") or TASKS[task]["default"]
    return {
        "version": saved.get("version", 1),
        "text": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "updated_at": saved.get("updated_at"),
        "default_text": TASKS[task]["default"],
    }


def prompt_snapshot(policy):
    return {key: policy[key] for key in ("version", "text", "sha256")}


class PromptUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    text: str = Field(min_length=20, max_length=12000)


def tasks_router(current_user, db_session, config):
    router = APIRouter(prefix="/api/user/llm-tasks")

    @router.get("")
    def listing(user=Depends(current_user), db=Depends(db_session)):
        row = db.get(UserSettings, user.id)
        settings = row.value if row and row.tenant_id == user.tenant_id else {}
        profile = settings.get("analysis_model", {})
        default_model = (
            settings.get("cloud_model") if profile.get("provider") == "openai" else profile.get("model_id")
        )
        return {
            "items": [
                {
                    "id": key,
                    **{k: v for k, v in definition.items() if k != "default"},
                    "policy": task_policy(db, user, key),
                }
                for key, definition in TASKS.items()
            ],
            "default_model": default_model or config.vllm_model_id,
            "default_provider": profile.get("provider", "vllm"),
            "news_model": settings.get("news_model", "default"),
            "research_enabled": config.research_enabled,
            "search_configured": bool(config.tavily_api_key.get_secret_value()),
            "fixed_rules": "외부 자료의 지시 무시, 서버 고정 출력 형식·근거 검증, 주문 실행 금지, 사용자별 연결·실행 권한은 프롬프트로 변경할 수 없습니다.",
        }

    @router.put("/{task}")
    def save(task: Task, payload: PromptUpdate, user=Depends(current_user), db=Depends(db_session)):
        if not user.active or user.role == "viewer":
            raise HTTPException(403, "프롬프트 변경 권한이 없습니다.")
        row = db.scalar(
            select(UserSettings)
            .where(UserSettings.user_id == user.id, UserSettings.tenant_id == user.tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not row:
            raise HTTPException(409, "사용자 설정이 없습니다.")
        policy = task_policy(db, user, task)
        if payload.version != policy["version"]:
            raise HTTPException(409, "프롬프트가 변경되었습니다. 다시 조회하세요.")
        text = payload.text.strip()
        if len(text) < (100 if task == "news" else 20):
            raise HTTPException(
                422, "프롬프트 길이를 확인하세요. 뉴스는 100자 이상, 다른 작업은 20자 이상입니다."
            )
        record = {"text": text, "version": payload.version + 1, "updated_at": now().isoformat()}
        value = {**row.value}
        if task == "news":
            value["news_system_prompt"] = record  # Same record as the legacy news prompt editor.
        else:
            value["llm_task_prompts"] = {**value.get("llm_task_prompts", {}), task: record}
        # CAS also prevents lost updates on SQLite, where SELECT FOR UPDATE is ignored.
        changed = db.execute(
            update(UserSettings)
            .where(
                UserSettings.user_id == user.id,
                UserSettings.tenant_id == user.tenant_id,
                UserSettings.version == row.version,
            )
            .values(value=value, version=row.version + 1, updated_at=now())
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "설정이 변경되었습니다. 다시 조회하세요.")
        audit(
            db,
            user,
            "llm.prompt_updated",
            task,
            {"task": task, "before": prompt_snapshot(policy), "after": record},
        )
        db.commit()
        return task_policy(db, user, task)

    @router.get("/{task}/history")
    def history(task: Task, user=Depends(current_user), db=Depends(db_session)):
        rows = db.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.actor_id == user.id,
                AuditEvent.tenant_id == user.tenant_id,
                AuditEvent.action == "llm.prompt_updated",
                AuditEvent.target_id == task,
            )
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(20)
        ).all()
        return {"items": [{"created_at": r.created_at.isoformat(), **r.detail} for r in rows]}

    return router
