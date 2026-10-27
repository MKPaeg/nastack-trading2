import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from conftest import EMAIL, ORIGIN
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from test_vectors import payload, seed_vector_data

from nastack_trading.app import create_app
from nastack_trading.db import now
from nastack_trading.dimension_catalog import BY_ID
from nastack_trading.dimensions import missing_ranges
from nastack_trading.models import (
    DimensionCollection,
    NewsArticle,
    NewsRun,
    PersonalSource,
    User,
    VectorResult,
)
from nastack_trading.news import parse_feed


def clock():
    return datetime(2026, 10, 20, 18, tzinfo=timezone.utc)  # Oct 21 03:00 KST


def feed():
    return b"""<rss><channel>
    <item><title>Oil supply disruption</title><link>https://www.eia.gov/todayinenergy/detail.php?id=1</link><description>Oil supply disruption increases crude prices.</description><pubDate>Tue, 20 Oct 2026 17:00:00 GMT</pubDate></item>
    <item><title>Old news</title><link>https://www.eia.gov/old</link><pubDate>Sun, 20 Apr 2026 10:00:00 GMT</pubDate></item>
    <item><title>Future news</title><link>https://www.eia.gov/future</link><pubDate>Tue, 20 Oct 2026 19:00:00 GMT</pubDate></item>
    <item><title>Unknown date</title><link>https://www.eia.gov/unknown</link></item>
    <item><title>Other host</title><link>https://attacker.example/news</link><pubDate>Tue, 20 Oct 2026 17:00:00 GMT</pubDate></item>
    </channel></rss>"""


def test_feed_only_today_explicit_timezone_no_future_no_foreign_host():
    rows, rejected = parse_feed(
        feed(), date(2026, 10, 21), clock(), "https://www.eia.gov/rss/todayinenergy.xml"
    )
    assert len(rows) == 1 and rejected == 4
    assert rows[0]["title"] == "Oil supply disruption"
    for raw in [b"<html>OK</html>", b'<!DOCTYPE rss [<!ENTITY x "bad">]><rss/>']:
        with pytest.raises(ValueError):
            parse_feed(raw, date(2026, 10, 21), clock(), "https://www.eia.gov/rss/todayinenergy.xml")


def test_six_month_window_boundary():
    raw = feed().replace(b"20 Apr 2026 10:00:00 GMT", b"20 Apr 2026 15:00:00 GMT")
    rows, _ = parse_feed(raw, date(2026, 10, 21), clock(), "https://www.eia.gov/rss/todayinenergy.xml")
    assert len(rows) == 2  # Apr 21 00:00 KST is the inclusive lower bound.
    rows, _ = parse_feed(raw, date(2026, 10, 22), clock(), "https://www.eia.gov/rss/todayinenergy.xml")
    assert len(rows) == 1


def test_vector_selection_result_roundtrip_and_version(admin, setup):
    seed_vector_data(setup[1])
    assert admin.get("/api/vectors/state").json()["selection"]["version"] == 0
    value = {
        "code": "123456",
        "start": "2026-01-01",
        "end": "2026-10-19",
        "dimensions": ["sector-01"],
        "horizons": [1],
        "window": 60,
        "levels": 5,
    }
    saved = admin.put("/api/vectors/selection", json={"version": 0, "value": value})
    assert saved.status_code == 200
    assert saved.json()["version"] == 1
    assert admin.put("/api/vectors/selection", json={"version": 0, "value": value}).status_code == 409
    first = admin.post("/api/vectors/analyze", json=payload()).json()
    second = admin.post("/api/vectors/analyze", json=payload()).json()
    assert first["persistence"]["saved"] is True
    assert first["persistence"]["id"] == second["persistence"]["id"]
    state = admin.get("/api/vectors/state").json()
    assert state["selection"]["value"]["dimensions"] == ["sector-01"]
    assert state["latest_result"]["id"] == first["persistence"]["id"]
    assert admin.get("/api/vectors/history").json()["items"][0]["id"] == first["persistence"]["id"]
    with setup[1]() as db:
        assert db.scalar(select(func.count()).select_from(VectorResult)) == 1
    other = admin.post("/api/users", json={"email": "storage-other@example.com", "role": "trader"}).json()
    admin.post("/api/auth/logout")
    admin.post("/api/auth/login", json={"email": other["email"], "access_code": other["access_code"]})
    assert admin.get("/api/vectors/state").json()["selection"]["version"] == 0
    assert admin.get(f"/api/vectors/history/{first['persistence']['id']}").status_code == 404


def test_incremental_retries_unreturned_prefix_and_delayed_tail(setup):
    with setup[1].begin() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        d = BY_ID["commodity-17"]
        db.add(
            DimensionCollection(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                dimension_id=d["id"],
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 10),
                status="completed",
                created_at=now() - timedelta(days=1),
                value={"series_code": d["series_code"], "points": [{"date": "2026-01-08", "value": 10}]},
            )
        )
        db.flush()
        assert missing_ranges(db, user, d, date(2026, 1, 1), date(2026, 1, 15), "Asia/Seoul") == [
            (date(2026, 1, 1), date(2026, 1, 7)),
            (date(2026, 1, 9), date(2026, 1, 15)),
        ]
        assert missing_ranges(db, user, d, date(2026, 1, 2), date(2026, 1, 8), "Asia/Seoul") == [
            (date(2026, 1, 2), date(2026, 1, 7))
        ]


@pytest.mark.parametrize("problem", [None, "invalid_score", "invented_evidence", "model_http"])
def test_news_storage_scoring_reuse_and_source_policy(setup, monkeypatch, problem):
    monkeypatch.setattr("nastack_trading.news.now", clock)
    config = setup[0].model_copy(
        update={
            "news_enabled": True,
            "vllm_base_url": "http://local-model.test/v1",
            "vllm_model_id": "test-local",
        }
    )
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.method == "GET":
            assert request.url.host == "www.eia.gov"
            return httpx.Response(200, content=feed())
        assert str(request.url) == "http://local-model.test/v1/chat/completions"
        body = json.loads(request.content)
        data = json.loads(body["messages"][1]["content"])
        article_id = data["articles"][0]["id"]
        report = {
            "assessments": [
                {
                    "article_id": article_id,
                    "dimension_id": "commodity-17",
                    "score9": 9 if problem == "invalid_score" else 3,
                    "reason": "공급 차질은 WTI 가격 상승 압력으로 추론",
                    "evidence": "made up" if problem == "invented_evidence" else "Oil supply disruption",
                },
                {
                    "article_id": article_id,
                    "dimension_id": "sector-22",
                    "score9": -2,
                    "reason": "연료 비용 상승은 해운 전망에 부정적일 수 있음",
                    "evidence": "increases crude prices",
                },
            ]
        }
        return httpx.Response(
            503 if problem == "model_http" else 200,
            json={"choices": [{"message": {"content": json.dumps(report)}}]},
        )

    with TestClient(
        create_app(config, news_transport=httpx.MockTransport(respond)), headers={"origin": ORIGIN}
    ) as client:
        from conftest import CODE

        client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE})
        source = client.post(
            "/api/sources",
            json={
                "name": "EIA",
                "domain": "www.eia.gov",
                "url": "https://www.eia.gov/",
                "region": "US",
                "category": "에너지",
                "method": "rss",
                "status": "candidate",
                "disposition": "reference",
            },
        ).json()
        request = {"source_ids": [source["id"]]}
        assert client.post("/api/news/collect", json=request).status_code == 409
        assert not calls
        value = {
            key: source[key]
            for key in [
                "name",
                "domain",
                "url",
                "region",
                "category",
                "method",
                "status",
                "disposition",
                "access_notes",
            ]
        }
        assert (
            client.put(
                f"/api/sources/{source['id']}",
                json={
                    **value,
                    "version": 1,
                    "status": "active",
                    "disposition": "use",
                    "reason": "test approval",
                },
            ).status_code
            == 200
        )
        response = client.post("/api/news/collect", json=request)
        assert response.status_code == 200, response.text
        daily = client.get("/api/news/today").json()
        assert len(daily["articles"]) == 1
        if problem:
            assert response.json()["status"] == "model_failed"
            assert daily["total_sum"] is None and daily["assessments"] == []
        else:
            assert response.json()["status"] == "completed"
            assert daily["total_sum"] == 1  # +2 WTI and -1 shipping in five-level display.
            assert client.get("/api/news/today?levels=9").json()["total_sum"] == 1  # +3 and -2.
            assert next(row for row in daily["aggregate"] if row["id"] == "sector-01")["sum"] is None
            with setup[1].begin() as db:
                db.execute(update(NewsRun).values(created_at=clock() - timedelta(minutes=2)))
            again = client.post("/api/news/collect", json=request)
            assert again.json()["status"] == "completed"
            assert calls.count("/v1/chat/completions") == 1
            assert len(client.get("/api/news/today").json()["articles"]) == 1
            with setup[1].begin() as db:
                row = db.scalar(select(PersonalSource).where(PersonalSource.id == source["id"]))
                row.value = {**row.value, "disposition": "exclude"}
                row.version += 1
            assert client.get("/api/news/today").json()["assessments"] == []
        with setup[1]() as db:
            assert db.scalar(select(func.count()).select_from(NewsArticle)) == 1


def test_news_week_rolloff_preserves_database_history(admin, setup, monkeypatch):
    with setup[1].begin() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        db.add(
            NewsRun(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                news_date=date(2026, 10, 20),
                status="completed",
                value={"assessments": []},
            )
        )
    monkeypatch.setattr("nastack_trading.news.now", clock)
    data = admin.get("/api/news/today").json()
    assert data["date"] == "2026-10-21" and len(data["runs"]) == 1 and data["total_sum"] is None
    assert data["start_date"] == "2026-10-08"
    monkeypatch.setattr("nastack_trading.news.now", lambda: clock() + timedelta(days=190))
    assert admin.get("/api/news/today").json()["runs"] == []
    with setup[1]() as db:
        assert db.scalar(select(func.count()).select_from(NewsRun)) == 1
