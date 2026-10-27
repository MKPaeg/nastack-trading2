"""Empirical, period-dependent attributes, not verified fundamental exposures."""

import math
from datetime import date, timedelta

from .db import now
from .models import StockFactorProfile


def build_profile(result, result_id):
    candidates = []
    sources = {item["id"]: item for item in result.get("dimensions", [])}
    for feature in result.get("features", []):
        source = sources.get(feature["dimension_id"], {})
        coefficients = feature.get("correlations", {})
        values = [coefficients.get(method) for method in ("pearson", "spearman", "kendall")]
        finite = [value for value in values if isinstance(value, (float, int)) and math.isfinite(value)]
        strength = sum(abs(value) for value in finite) / len(finite) if finite else None
        direction = (
            "positive"
            if finite and all(v > 0 for v in finite)
            else "negative"
            if finite and all(v < 0 for v in finite)
            else "mixed"
        )
        candidates.append(
            {
                "dimension_id": feature["dimension_id"],
                "name": feature["name"],
                "horizon": feature["horizon"],
                "direction": direction,
                "same_instrument": bool(source.get("same_instrument")),
                "strength": strength,
                "correlations": coefficients,
                "samples": feature["samples"],
                "source": source.get("source"),
                "source_url": source.get("source_url"),
                "series_code": source.get("series_code"),
                "representation": source.get("representation"),
                "proxy_definition": source.get("proxy_definition"),
                "last_observation": source.get("last_observation"),
                "first_pair": feature.get("first") or result.get("summary", {}).get("first"),
                "last_pair": feature.get("last") or result.get("summary", {}).get("last"),
                "lag": source.get("lag"),
            }
        )
    candidates.sort(key=lambda item: (-(item["strength"] or 0), item["dimension_id"], item["horizon"]))
    return {
        "schema_version": 1,
        "origin": "manual",
        "code": result["instrument"]["code"],
        "result_id": result_id,
        "data_cutoff": result["data_cutoff"],
        "request": result["request"],
        "factors": candidates,
        "selected_dimensions": result["request"]["dimensions"],
        "comparison": result.get("comparison", []),
        "scope": "사용자가 버튼을 눌러 저장한 전체 차원 조합·변화 기간·혼합 벡터 상관도",
        "rule": "차원 자동 추림·자동 저장 없음 · 동일 종목의 재저장은 기존 속성을 교체",
        "warning": "탐색적 관측 상관이며 기업의 실제 사업 노출·인과·뉴스 전달 경로·예측력을 검증하지 않았습니다. 음의 상관도 중요하며 계절·시장 국면에 따라 바뀔 수 있습니다. 점수를 상관계수로 기계적으로 곱하지 않습니다.",
    }


def save_profile(db, user, result, result_id):
    profile = build_profile(result, result_id)
    row = db.get(StockFactorProfile, (user.id, profile["code"]))
    if not row:
        row = StockFactorProfile(owner_id=user.id, code=profile["code"], tenant_id=user.tenant_id)
        db.add(row)
    if row.value and row.value.get("business_context"):
        profile["business_context"] = row.value["business_context"]
    row.value, row.updated_at = profile, now()
    return profile


def profile_view(db, user, code, today):
    from .business_research import latest_business
    from .dimensions import excluded_ids

    researched = latest_business(db, user, code)
    row = db.get(StockFactorProfile, (user.id, code))
    if not row or row.tenant_id != user.tenant_id:
        return {
            "status": "missing",
            "business_context": researched,
            "factors": [],
            "note": "트레이딩 벡터에서 상관도를 확인하고 ‘다차원 속성 저장’을 누르세요. 분석만으로 속성은 저장되지 않습니다.",
        }
    if row.value.get("origin") == "llm_default":
        excluded = excluded_ids(db, user)
        return {
            **row.value,
            "business_context": researched or row.value.get("business_context"),
            "selected_dimensions": [
                key for key in row.value.get("selected_dimensions", []) if key not in excluded
            ],
            "usable_for_news": False,
            "usable_as_topic_hint": row.value.get("status") == "ready",
            "note": "출처 있는 사업 설명 기반의 초기 차원 제안입니다. 규칙 또는 관리자 LLM으로 생성되며 관측 상관도·사업별 노출 비중을 검증한 속성은 아닙니다. 수동 저장 시 이 기본값을 교체합니다.",
        }
    value = {
        **row.value,
        "business_context": researched or row.value.get("business_context"),
        "factors": [
            dict(item)
            for item in row.value["factors"]
            if item["dimension_id"] not in excluded_ids(db, user)
            and not item.get("same_instrument")
            and item.get("samples", 0) >= 20
            and item.get("strength") is not None
        ],
    }
    cutoff = date.fromisoformat(value["data_cutoff"])
    for factor in value["factors"]:
        last = date.fromisoformat(factor.get("last_pair") or value["data_cutoff"])
        factor["fresh"] = today - timedelta(days=14) <= last < today
    value["factors"] = [factor for factor in value["factors"] if factor["fresh"]]
    status = (
        "future"
        if cutoff >= today
        else "stale"
        if cutoff < today - timedelta(days=14)
        else "ready"
        if value["factors"]
        else "insufficient"
    )
    return {
        **value,
        "status": status,
        "usable_for_news": status == "ready",
        "note": "14일 이상 지난 분석·오늘/미래 자료·표본 부족 요인은 뉴스 판단 근거에서 제외합니다.",
    }
