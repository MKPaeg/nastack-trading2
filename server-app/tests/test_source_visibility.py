from datetime import timedelta

from sqlalchemy import func, select

from nastack_trading.db import now
from nastack_trading.models import NewsArticle, PersonalSource, StatisticSeries, User
from nastack_trading.source_visibility import stored_source_counts, successful_source_ids


def test_successful_sources_hide_metadata_only_and_preserve_old_evidence(admin, setup):
    admin.get("/api/sources")
    with setup[1].begin() as db:
        user = db.scalar(select(User))
        sources = []
        for host in ["eia.gov", "datahelpdesk.worldbank.org", "uncollected.example"]:
            source = db.scalar(
                select(PersonalSource).where(
                    PersonalSource.owner_id == user.id, PersonalSource.domain == host
                )
            )
            if not source:
                source = PersonalSource(
                    owner_id=user.id,
                    tenant_id=user.tenant_id,
                    domain=host,
                    value={"name": host, "domain": host, "url": "https://" + host, "status": "active"},
                )
                db.add(source)
                db.flush()
            sources.append(source)
        stamp = now() - timedelta(days=30)
        db.add(
            NewsArticle(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                source_id=sources[0].id,
                fingerprint="a" * 64,
                published_at=stamp,
                news_date=stamp.date(),
                value={"title": "Old persisted evidence"},
            )
        )
        db.add(
            StatisticSeries(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                series_id="WB_KOR_MANUFACTURING",
                value={"points": [{"date": "2025-01-01", "value": 123}]},
            )
        )
        wanted = {sources[0].id, sources[1].id}
        db.add(
            StatisticSeries(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                series_id="WB_KOR_EXPORT_GROWTH",
                value={"points": [{"date": "2024-01-01", "value": 2}, {"date": "2025-01-01", "value": 3}]},
            )
        )
    total = admin.get("/api/sources").json()["total"]
    result = admin.get("/api/sources?successful_only=true").json()
    assert {s["id"] for s in result["items"]} == wanted
    assert result["total"] == 2 and result["hidden_count"] == total - 2
    with setup[1]() as db:
        user = db.scalar(select(User))
        assert successful_source_ids(db, user) == wanted
        assert stored_source_counts(db, user)[sources[1].id] == 3
        assert db.scalar(select(func.count()).select_from(PersonalSource)) == total
        assert db.scalar(select(func.count()).select_from(NewsArticle)) == 1
