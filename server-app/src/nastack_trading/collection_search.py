"""Owner-scoped single flight for public retrieval, not company impact scores."""

import hashlib
import json
import unicodedata
from datetime import timedelta

import httpx
from fastapi import HTTPException
from sqlalchemy import select, update

from .db import aware, now
from .models import CollectionSearch, PersonalSource, User
from .news import KST, feed_for, parse_feed
from .research import ResearchError, ResearchPipeline

VERSION = "general-dated-v2"
TTL = timedelta(hours=12)
INDUSTRY_QUERIES = {
    "eia.gov": "electricity demand renewable energy natural gas",
    "trendforce.com": "semiconductor DRAM NAND HBM demand supply prices",
}


def search_request(source, query, data):
    domain = source.domain.removeprefix("www.")
    # These are source-wide discovery queries, not semantic equivalence claims
    # about two companies. The original targeted plan remains visible separately.
    query = INDUSTRY_QUERIES.get(domain, query)
    if domain == "kind.krx.co.kr":
        query = "KRX official recent disclosures · all issuers"
    elif domain == "yna.co.kr":
        # Generic finance terms diluted the company query into unrelated stocks.
        # Require its exact public name; do not invent a proxy company's news.
        query = '"' + data["instrument"]["name"].replace('"', "") + '"'
    query = " ".join(unicodedata.normalize("NFKC", query).casefold().split())
    return {
        "query": query,
        "include_domains": [domain],
        "topic": "general",
        "time_range": "month",
        "include_published_date": True,
        "max_results": 12,
        "search_depth": "basic",
        "include_raw_content": False,
        "include_answer": False,
    }


def retrieve(db, user, source, query, data, transport=None):
    expected_tenant, expected_version = user.tenant_id, source.version
    request = search_request(source, query, data)
    key = hashlib.sha256(
        json.dumps(
            {
                "owner": user.id,
                "tenant": user.tenant_id,
                "source": source.id,
                "version": source.version,
                "request": request,
                "collector": VERSION,
                "model": data["plan_model"],
                "prompt": data["policies"]["collection_plan"]["sha256"],
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    db.execute(
        update(User).where(User.id == user.id, User.tenant_id == user.tenant_id).values(active=User.active)
    )
    current = db.get(User, user.id, populate_existing=True)
    policy = db.scalar(
        select(PersonalSource)
        .where(
            PersonalSource.id == source.id,
            PersonalSource.owner_id == user.id,
            PersonalSource.tenant_id == user.tenant_id,
        )
        .execution_options(populate_existing=True)
    )
    if (
        current.tenant_id != expected_tenant
        or not current.active
        or current.role == "viewer"
        or not policy
        or policy.version != expected_version
        or policy.value.get("status") != "active"
        or policy.value.get("disposition") != "use"
    ):
        db.rollback()
        raise HTTPException(403, "수집 권한·출처 승인이 변경되었습니다.")
    row = db.get(CollectionSearch, (user.id, key))
    if row and row.tenant_id != user.tenant_id:
        db.rollback()
        raise HTTPException(404, "본인의 검색이 아닙니다.")
    if row and row.status != "completed":
        db.rollback()
        raise ResearchError("SHARED_SEARCH_INCOMPLETE")
    from .data_collection import policy

    ttl = timedelta(hours=policy(db, user)["news_hours"])
    if row and aware(row.updated_at) + ttl > now():
        value = dict(row.value)
        db.commit()
        return value, True
    from .data_collection import charge

    charge(db, user, "search")
    if not row:
        row = CollectionSearch(owner_id=user.id, tenant_id=user.tenant_id, fingerprint=key)
        db.add(row)
    row.status, row.updated_at = "running", now()
    row.value = {"request": request, "collector": VERSION}
    db.commit()  # Unknown paid requests are never automatically submitted twice.
    try:
        if source.domain == "kind.krx.co.kr":
            with httpx.Client(
                timeout=20, trust_env=False, follow_redirects=False, transport=transport
            ) as client:
                with client.stream("GET", feed_for(source.domain)[0]) as response:
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 1_000_000:
                            raise ValueError("Oversized disclosure feed")
            items, rejected = parse_feed(
                bytes(body), now().astimezone(KST).date(), now(), feed_for(source.domain)[0]
            )
            raw = {
                "results": [
                    {**item, "content": item["excerpt"], "published_date": item["published_at"]}
                    for item in items
                ]
            }
            row.value = {**row.value, "method": "official_feed", "feed_rejected": rejected}
        else:
            raw = ResearchPipeline(data["config"], transport).post(
                "https://api.tavily.com/search",
                data["config"].tavily_api_key.get_secret_value(),
                request,
                30,
            )
        results = [
            {
                k: str(r.get(k) or "")[:5000]
                for k in ("title", "url", "content", "published_date", "issuer", "receipt_no")
            }
            for r in raw.get("results", [])[: 100 if source.domain == "kind.krx.co.kr" else 12]
        ]
        row.status, row.updated_at = "completed", now()
        row.value = {**row.value, "results": results, "checked_at": now().isoformat()}
        db.commit()
        return dict(row.value), False
    except Exception:
        db.rollback()
        row = db.get(CollectionSearch, (user.id, key))
        row.status, row.updated_at = "failed", now()
        db.commit()
        raise
