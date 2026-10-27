from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from nastack_trading.models import (
    AnalysisRun,
    NewsArticle,
    NewsRun,
    PersonalSource,
    RealizedSale,
    Tenant,
    TradeOrder,
    TradePlan,
    User,
    VectorResult,
)
from nastack_trading.security import create_session

STAMP = datetime(2026, 10, 23, 16, tzinfo=timezone.utc)  # Oct 24 in Korea.
PARAMS = {"start": "2026-10-24", "end": "2026-10-24"}


def seed(db, user):
    owner = {"owner_id": user.id, "tenant_id": user.tenant_id}
    vector = VectorResult(
        **owner,
        fingerprint="v" * 64,
        created_at=STAMP,
        value={
            "request": {"code": "005930", "method": "pearson"},
            "instrument": {"name": "시험 종목"},
            "status": "completed",
            "summary": {"samples": 60},
            "today": "2026-10-26",
            "data_cutoff": "2026-10-23",
            "credential_scope": "SECRET_NOT_FOR_REPORTS",
        },
    )
    source = PersonalSource(**owner, domain="evidence.example", value={"name": "Evidence"})
    db.add_all([vector, source])
    db.flush()
    article = NewsArticle(
        **owner,
        source_id=source.id,
        fingerprint="a" * 64,
        published_at=STAMP,
        news_date=date(2026, 10, 24),
        value={"title": "시험 기사", "excerpt": "시험 근거", "url": "https://evidence.example/a"},
    )
    db.add(article)
    db.flush()
    news = NewsRun(
        **owner,
        news_date=date(2026, 10, 24),
        status="partial",
        created_at=STAMP,
        value={
            "instrument": {"code": "005930", "name": "시험 종목"},
            "article_ids": [article.id],
            "assessments": [
                {
                    "article_id": article.id,
                    "dimension_id": "stock-005930",
                    "score9": None,
                    "reason": "판단 근거 부족",
                    "evidence": "시험 근거",
                }
            ],
            "model": "test-model",
            "system_prompt": {"text": "PRIVATE_PROMPT", "sha256": "hash"},
        },
    )
    research = AnalysisRun(
        **owner,
        symbol="005930",
        kind="research",
        request_key="research",
        settings_version=2,
        settings_snapshot={},
        created_at=STAMP,
        status="completed",
        result={"summary": "실제 연구 요약", "evidence": []},
    )
    demo = AnalysisRun(
        **owner,
        symbol="005930",
        kind="pipeline_demo",
        request_key="demo",
        settings_version=1,
        settings_snapshot={},
        created_at=STAMP,
        status="completed",
        result={"simulation": True, "summary": "검증용", "evidence": []},
    )
    plan = TradePlan(**owner, broker="kiwoom", code="005930", value={})
    db.add_all([news, research, demo, plan])
    db.flush()
    order = TradeOrder(**owner, plan_id=plan.id, request_key="sale", status="filled", value={})
    db.add(order)
    db.flush()
    sale = RealizedSale(
        **owner,
        plan_id=plan.id,
        order_id=order.id,
        code="005930",
        broker="kiwoom",
        trade_day=date(2026, 10, 24),
        quantity=1,
        proceeds=Decimal("110"),
        cost=None,
        profit=None,
        updated_at=STAMP,
        value={"name": "시험 종목", "fees_tax": "excluded"},
    )
    db.add(sale)
    db.flush()
    return {"vector": vector.id, "news": news.id, "research": research.id, "sale": sale.id}, demo.id


def test_reports_connect_saved_results_and_never_execute(admin, setup, monkeypatch):
    from nastack_trading.research import ResearchPipeline
    from nastack_trading.trade_gateway import TradeGateway

    monkeypatch.setattr(ResearchPipeline, "post", lambda *a, **k: pytest.fail("no paid analysis"))
    monkeypatch.setattr(TradeGateway, "submit", lambda *a, **k: pytest.fail("no orders"))
    with setup[1].begin() as db:
        user = db.scalar(select(User))
        ids, demo = seed(db, user)
    response = admin.get("/api/reports", params=PARAMS)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 4
    assert response.json()["counts"] == {"vector": 1, "news": 1, "research": 1, "sale": 1}
    assert all(not row["simulation"] for row in response.json()["items"])
    assert admin.get("/api/reports", params={**PARAMS, "include_demo": True}).json()["total"] == 5
    first = admin.get("/api/reports", params={**PARAMS, "include_demo": True, "page_size": 5}).json()
    assert len({item["id"] for item in first["items"]}) == 5
    assert (
        admin.get("/api/reports", params={**PARAMS, "include_demo": True, "page_size": 5, "page": 2}).json()[
            "items"
        ]
        == []
    )
    assert admin.get("/api/reports", params={**PARAMS, "code": "999999"}).json()["total"] == 0
    assert admin.get("/api/reports", params={**PARAMS, "kind": "vector"}).json()["total"] == 1
    # Calendar boundaries must use KST rather than the UTC save date.
    assert admin.get("/api/reports", params={"start": "2026-10-23", "end": "2026-10-23"}).json()["total"] == 0
    for kind, identity in ids.items():
        detail = admin.get(f"/api/reports/{kind}/{identity}")
        assert detail.status_code == 200
        assert "SECRET_NOT_FOR_REPORTS" not in detail.text and "PRIVATE_PROMPT" not in detail.text
    news = admin.get(f"/api/reports/news/{ids['news']}").json()
    assert news["data"]["assessments"][0]["score9"] is None
    assert news["articles"][0]["title"] == "시험 기사"
    sale = admin.get(f"/api/reports/sale/{ids['sale']}").json()
    assert sale["data"]["profit"] is None and sale["data"]["cost"] is None
    assert sale["item"]["status"] == "missing_cost"
    assert admin.get(f"/api/reports/research/{demo}").json()["item"]["simulation"] is True
    assert admin.get("/api/reports", params={"start": "2026-10-25", "end": "2026-10-24"}).status_code == 422
    with setup[1]() as db:
        assert db.scalar(select(func.count()).select_from(TradeOrder)) == 1
        assert db.scalar(select(func.count()).select_from(AnalysisRun)) == 2


@pytest.mark.parametrize("role", ["trader", "viewer", "admin"])
@pytest.mark.parametrize("other_tenant", [False, True])
def test_report_list_details_and_article_references_are_owner_scoped(admin, setup, role, other_tenant):
    with setup[1].begin() as db:
        original = db.scalar(select(User))
        foreign_ids, _ = seed(db, original)
        tid = original.tenant_id
        if other_tenant:
            tenant = Tenant(name="Other", timezone="Asia/Seoul")
            db.add(tenant)
            db.flush()
            tid = tenant.id
        user = User(tenant_id=tid, email="other@example.com", role=role, active=True, code_hash="unused")
        db.add(user)
        db.flush()
        own_ids, _ = seed(db, user)
        # Even a corrupted JSON reference cannot fetch another owner's article.
        foreign_article = db.scalar(select(NewsArticle).where(NewsArticle.owner_id == original.id))
        own_news = db.get(NewsRun, own_ids["news"])
        own_news.value = {
            **own_news.value,
            "article_ids": [foreign_article.id],
            "assessments": [{"article_id": foreign_article.id, "dimension_id": "stock-005930", "score9": 4}],
        }
        token = create_session(db, user, 24)
    admin.cookies.set("trading_session", token)
    data = admin.get("/api/reports", params=PARAMS).json()
    assert {row["id"] for row in data["items"]} == set(own_ids.values())
    for kind, identity in foreign_ids.items():
        assert admin.get(f"/api/reports/{kind}/{identity}").status_code == 404
        assert admin.get(f"/api/reports/{kind}/{own_ids[kind]}").status_code == 200
    detail = admin.get(f"/api/reports/news/{own_ids['news']}").json()
    assert detail["articles"] == [] and detail["data"]["assessments"] == []
    assert admin.post("/api/reports", json={}).status_code == 405


def test_report_filters_persist_without_replacing_other_settings(admin, setup):
    before = admin.get("/api/settings").json()["value"]
    filters = {
        "start": "2026-01-01",
        "end": "2026-10-24",
        "kind": "news",
        "code": "005930",
        "include_demo": False,
    }
    assert (
        admin.put("/api/settings/screen-state", json={"view": "reports", "report": filters}).status_code
        == 200
    )
    restored = admin.get("/api/settings").json()["value"]
    assert restored["screen_state"]["report"] == filters
    for key in before.keys() - {"screen_state"}:
        assert restored[key] == before[key]
    admin.post("/api/auth/logout")
    with setup[1].begin() as db:
        user = db.scalar(select(User))
        token = create_session(db, user, 24)
    admin.cookies.set("trading_session", token)
    assert admin.get("/api/settings").json()["value"]["screen_state"]["report"] == filters
    # An older open browser that omits report fields must preserve the saved filter.
    assert admin.put("/api/settings/screen-state", json={"view": "orders"}).status_code == 200
    assert admin.get("/api/settings").json()["value"]["screen_state"]["report"] == filters


def test_zero_profit_is_not_missing_and_excluded_sales_stay_hidden(admin, setup):
    with setup[1].begin() as db:
        user = db.scalar(select(User))
        ids, _ = seed(db, user)
        sale = db.get(RealizedSale, ids["sale"])
        sale.cost, sale.profit = Decimal("110"), Decimal("0")
    detail = admin.get(f"/api/reports/sale/{ids['sale']}").json()
    assert detail["item"]["status"] == "confirmed"
    assert Decimal(detail["data"]["profit"]) == 0
    with setup[1].begin() as db:
        sale = db.get(RealizedSale, ids["sale"])
        sale.value = {**sale.value, "excluded_at": STAMP.isoformat()}
    assert admin.get("/api/reports", params={**PARAMS, "kind": "sale"}).json()["total"] == 0
    assert admin.get(f"/api/reports/sale/{ids['sale']}").status_code == 404
