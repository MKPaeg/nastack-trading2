from test_vectors import payload, seed_vector_data


def test_unchanged_inputs_reuse_without_calculating_again(admin, setup, monkeypatch):
    import nastack_trading.vectors as vectors

    seed_vector_data(setup[1])
    original = vectors.analyze
    calls = []

    def measured(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(vectors, "analyze", measured)
    miss = admin.post("/api/vectors/analyze", json={**payload(), "cache_only": True})
    assert miss.json() == {"cache_hit": False}
    assert not calls
    first = admin.post("/api/vectors/analyze", json=payload())
    assert first.status_code == 200, first.text
    second = admin.post("/api/vectors/analyze", json=payload())
    assert second.status_code == 200, second.text
    assert second.json()["persistence"]["reused"] is True
    assert len(calls) == 1
    assert first.json()["persistence"]["id"] == second.json()["persistence"]["id"]
    probe = admin.post("/api/vectors/analyze", json={**payload(), "cache_only": True})
    assert probe.json()["persistence"]["id"] == first.json()["persistence"]["id"]
    assert len(calls) == 1
    # A fresh retrieval of identical values must not invalidate the numeric cache.
    from sqlalchemy import select

    from nastack_trading.models import DimensionCollection

    with setup[1].begin() as db:
        row = db.scalar(select(DimensionCollection))
        db.add(
            DimensionCollection(
                owner_id=row.owner_id,
                tenant_id=row.tenant_id,
                dimension_id=row.dimension_id,
                start_date=row.start_date,
                end_date=row.end_date,
                status=row.status,
                value=row.value,
            )
        )
    same_points = admin.post("/api/vectors/analyze", json=payload())
    assert same_points.json()["persistence"]["id"] == first.json()["persistence"]["id"]
    assert same_points.json()["freshness"]["network_called"] is False
    assert len(calls) == 1
    changed = admin.post("/api/vectors/analyze", json=payload(window=120))
    assert changed.status_code == 200, changed.text
    assert len(calls) == 2 and changed.json()["persistence"]["reused"] is False
    from nastack_trading.models import BrokerSnapshot

    with setup[1]() as db:
        row = db.scalar(select(BrokerSnapshot).where(BrokerSnapshot.kind == "market"))
        row.value = {
            **row.value,
            "chart": [{**point, "close": point["close"] * 1.01} for point in row.value["chart"]],
        }
        db.commit()
    refreshed = admin.post("/api/vectors/analyze", json=payload())
    assert refreshed.status_code == 200, refreshed.text
    assert len(calls) == 3 and refreshed.json()["persistence"]["reused"] is False
