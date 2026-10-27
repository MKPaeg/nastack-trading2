from copy import deepcopy

import httpx
import pytest
import test_collection_pipeline as collection_tests
from fastapi import HTTPException
from sqlalchemy import select

from nastack_trading.collection_pipeline import inputs, save_search, stored_material_ids
from nastack_trading.collection_search import retrieve, search_request
from nastack_trading.db import now
from nastack_trading.models import CollectionSearch, NewsArticle, NewsRun, PersonalSource, Tenant, User
from nastack_trading.news import article_signature, canonical_article_url, daily_view

workflow_client = collection_tests.workflow_client


def test_legacy_duplicate_does_not_make_every_reentry_due(workflow_client, setup):
    client, calls, _, uid, tid, _ = workflow_client
    assert collection_tests.refresh(client)["status"] == "completed"
    with setup[1].begin() as db:
        original = db.scalar(select(NewsArticle).where(NewsArticle.owner_id == uid))
        base = "https://www.yna.co.kr/view/AKR202610240001"
        original.value = {**original.value, "url": base}
        db.add(
            NewsArticle(
                owner_id=uid,
                tenant_id=tid,
                source_id=original.source_id,
                fingerprint="legacy-tracking-duplicate",
                published_at=original.published_at,
                news_date=original.news_date,
                value={**original.value, "url": base + "?input=1195m"},
            )
        )
    result = collection_tests.refresh(client)
    assert result["status"] == "completed", result
    assert result["counts"]["materials"] == 1
    settled = dict(calls)
    assert collection_tests.refresh(client)["status"] == "completed"
    assert calls == settled


def test_tracking_urls_do_not_double_count_stock_impact(workflow_client, setup):
    _, _, control, uid, tid, _ = workflow_client
    base = "https://www.yna.co.kr/view/AKR202610240001"
    assert canonical_article_url(base + "?input=1195m") == base
    assert canonical_article_url(base.replace("/view/", "/amp/view/") + "?input=1195m") == base
    with setup[1]() as db:
        user = db.get(User, uid)
        source = PersonalSource(
            owner_id=uid,
            tenant_id=tid,
            domain="www.yna.co.kr",
            value={
                "name": "공개 뉴스",
                "url": "https://www.yna.co.kr",
                "status": "active",
                "disposition": "use",
            },
        )
        db.add(source)
        db.flush()
        rows = []
        for i, url in enumerate([base, base + "?input=1195m"]):
            row = NewsArticle(
                owner_id=uid,
                tenant_id=tid,
                source_id=source.id,
                fingerprint=f"tracking-{i}",
                published_at=now(),
                news_date=now().date(),
                value={
                    "title": "시험기업 신규 수주",
                    "excerpt": "신규 수주를 발표",
                    "published_at": now().isoformat(),
                    "url": url,
                    "content_type": "news",
                },
            )
            db.add(row)
            rows.append(row)
        db.flush()
        db.add(
            NewsRun(
                owner_id=uid,
                tenant_id=tid,
                news_date=now().date(),
                status="completed",
                value={
                    "instrument": {"code": "123456", "name": "시험기업"},
                    "model": "test-model",
                    "sources": [{"id": source.id, "version": source.version}],
                    "article_signatures": {r.id: article_signature(r.value) for r in rows},
                    "assessments": [
                        {
                            "article_id": r.id,
                            "dimension_id": "stock-123456",
                            "score9": -2,
                            "relevance": "direct",
                            "reason": "원문 검토",
                            "evidence": "신규 수주",
                        }
                        for r in rows
                    ],
                },
            )
        )
        db.commit()
        result = daily_view(db, user, control["config"], 9, "123456")
        assert result["total_sum"] == -2
        assert result["material_counts"]["reviewed"] == 1
        assert len(result["articles"]) == 1
        assert len(db.scalars(select(NewsArticle)).all()) == 2  # Original audit evidence retained.


def test_different_company_query_does_not_replace_same_urls_original(workflow_client, setup):
    _, _, control, uid, tid, _ = workflow_client
    content = {"text": "첫 검색에서 저장한 동일 원문의 발췌"}

    def respond(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "공개 기사",
                        "url": "https://www.yna.co.kr/view/AKR20261024001",
                        "content": content["text"],
                        "published_date": now().isoformat(),
                    }
                ]
            },
        )

    with setup[1]() as db:
        user = db.get(User, uid)
        source = PersonalSource(
            owner_id=uid,
            tenant_id=tid,
            domain="www.yna.co.kr",
            value={
                "name": "공개 뉴스",
                "url": "https://www.yna.co.kr",
                "status": "active",
                "disposition": "use",
            },
        )
        db.add(source)
        db.commit()
        data = inputs(db, user, "123456", control["config"], False)
        first = save_search(db, user, "123456", source, "첫 기업", data, httpx.MockTransport(respond))
        db.commit()
        content["text"] = "다른 검색 질의로 반환된 다른 문단 · 실제 원문 변경 아님"
        second_data = {**data, "instrument": {**data["instrument"], "name": "다른 기업"}}
        second = save_search(
            db, user, "123456", source, "다른 기업", second_data, httpx.MockTransport(respond)
        )
        assert first["ids"] == second["ids"]
        assert db.get(NewsArticle, first["ids"][0]).value["excerpt"] == "첫 검색에서 저장한 동일 원문의 발췌"


def test_domestic_news_source_pool_requires_company_or_business_topic(workflow_client, setup):
    _, _, control, uid, tid, _ = workflow_client
    with setup[1]() as db:
        user = db.get(User, uid)
        source = PersonalSource(
            owner_id=uid,
            tenant_id=tid,
            domain="www.yna.co.kr",
            value={
                "name": "국내 공개 뉴스",
                "url": "https://www.yna.co.kr",
                "status": "active",
                "disposition": "use",
            },
        )
        db.add(source)
        db.flush()
        rows = []
        for title in ["시험기업 신규 수주", "다른 회사의 문화·스포츠 뉴스"]:
            row = NewsArticle(
                owner_id=uid,
                tenant_id=tid,
                source_id=source.id,
                fingerprint=title,
                published_at=now(),
                news_date=now().date(),
                value={"title": title, "excerpt": "공개 기사 근거", "content_type": "news"},
            )
            db.add(row)
            rows.append(row)
        db.commit()
        data = inputs(db, user, "123456", control["config"], False)
        assert stored_material_ids(db, user, source.id, data) == {rows[0].id}


def test_official_disclosure_feed_is_shared_across_company_queries(workflow_client, setup):
    _, _, control, uid, _, _ = workflow_client
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET" and request.url.host == "kind.krx.co.kr"
        return httpx.Response(
            200,
            content=(
                "<rss><channel><item><title>[유]시험기업 공시</title><author>[유]시험기업</author>"
                "<link>https://kind.krx.co.kr/common/disclsviewer.do?acptno=202610240001</link>"
                f"<pubDate>{now().isoformat()}</pubDate><description>공시 제목</description>"
                "</item></channel></rss>"
            ).encode(),
        )

    with setup[1]() as db:
        user = db.get(User, uid)
        source = db.scalar(
            select(PersonalSource).where(
                PersonalSource.owner_id == uid, PersonalSource.domain == "kind.krx.co.kr"
            )
        )
        if not source:
            source = PersonalSource(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                domain="kind.krx.co.kr",
                value={"name": "KRX", "url": "https://kind.krx.co.kr"},
            )
            db.add(source)
        source.value = {**source.value, "status": "active", "disposition": "use"}
        db.commit()
        data = inputs(db, user, "123456", control["config"], False)
        first, hit = retrieve(
            db, user, source, "시험기업 장문의 사업 공시", data, httpx.MockTransport(respond)
        )
        assert not hit and first["method"] == "official_feed" and len(first["results"]) == 1
        second_data = {**data, "instrument": {**data["instrument"], "name": "다른 회사", "code": "654321"}}
        second, hit = retrieve(
            db, user, source, "완전히 다른 공시 검색어", second_data, httpx.MockTransport(respond)
        )
        assert hit and second == first and len(calls) == 1


@pytest.mark.parametrize("same_tenant", [True, False])
def test_shared_search_never_accepts_another_owners_source(workflow_client, setup, same_tenant):
    _, calls, control, uid, tid, sid = workflow_client
    with setup[1]() as db:
        original = db.get(User, uid)
        data = inputs(db, original, "123456", control["config"], False)
        if not same_tenant:
            tenant = Tenant(name="Search isolation", timezone="Asia/Seoul")
            db.add(tenant)
            db.flush()
            tid = tenant.id
        user = User(
            tenant_id=tid,
            email="search-other@example.test",
            role="trader",
            active=True,
            code_hash="test-only",
        )
        db.add(user)
        db.commit()
        with pytest.raises(HTTPException) as error:
            retrieve(db, user, db.get(PersonalSource, sid), "public", data)
        assert error.value.status_code == 403 and calls["search"] == 0
        assert db.scalar(select(CollectionSearch)) is None


def test_industry_discovery_is_source_wide_not_company_score_cache(workflow_client, setup):
    _, _, control, uid, _, sid = workflow_client
    with setup[1]() as db:
        data = inputs(db, db.get(User, uid), "123456", control["config"], False)
        source = db.get(PersonalSource, sid)
        first = search_request(source, "기업A 메모리", data)
        other = deepcopy(data)
        other["instrument"]["name"] = "다른 반도체 기업"
        assert search_request(source, "기업B DRAM NAND", other) == first
        assert first["topic"] == "general" and first["include_published_date"] is True
