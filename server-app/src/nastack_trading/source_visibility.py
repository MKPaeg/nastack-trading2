"""Visibility is based on persisted observations, never a homepage HTTP status."""

from sqlalchemy import func, select

from .models import NewsArticle, PersonalSource, StatisticSeries
from .monthly_statistics import MONTHLY_SERIES

STATISTIC_SOURCE_DOMAINS = {
    "INDPRO": "fred.stlouisfed.org",
    "WB_KOR_MANUFACTURING": "datahelpdesk.worldbank.org",
    "WB_KOR_EXPORT_GROWTH": "datahelpdesk.worldbank.org",
}
STATISTIC_SOURCE_DOMAINS.update({key: value["domain"] for key, value in MONTHLY_SERIES.items()})


def successful_source_ids(db, user):
    return set(stored_source_counts(db, user))


def stored_source_counts(db, user):
    counts = dict(
        db.execute(
            select(NewsArticle.source_id, func.count())
            .where(
                NewsArticle.owner_id == user.id,
                NewsArticle.tenant_id == user.tenant_id,
            )
            .group_by(NewsArticle.source_id)
        ).all()
    )
    domains = {}
    for row in db.scalars(
        select(StatisticSeries).where(
            StatisticSeries.owner_id == user.id,
            StatisticSeries.tenant_id == user.tenant_id,
        )
    ):
        if row.series_id in STATISTIC_SOURCE_DOMAINS and row.value.get("points"):
            domain = STATISTIC_SOURCE_DOMAINS[row.series_id]
            domains[domain] = domains.get(domain, 0) + len(row.value["points"])
    counts.update(
        (row.id, counts.get(row.id, 0) + domains[row.domain.removeprefix("www.")])
        for row in db.scalars(
            select(PersonalSource).where(
                PersonalSource.owner_id == user.id,
                PersonalSource.tenant_id == user.tenant_id,
            )
        )
        if row.domain.removeprefix("www.") in domains
    )
    return counts
