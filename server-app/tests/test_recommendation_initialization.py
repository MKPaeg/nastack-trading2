from sqlalchemy import delete, select
from test_vectors import payload, seed_vector_data

from nastack_trading.models import BrokerSnapshot, StockFactorProfile, User, WatchlistItem


def test_initial_vector_assigns_without_watchlist_and_preserves_manual(admin, setup):
    seed_vector_data(setup[1])
    result = admin.post("/api/vectors/analyze", json=payload()).json()
    identity = result["persistence"]["id"]
    assert admin.post("/api/vectors/attributes", json={"result_id": identity, "auto_initial": True}).status_code == 403
    with setup[1].begin() as db:
        user = db.scalar(select(User))
        db.execute(delete(WatchlistItem).where(WatchlistItem.owner_id == user.id))
        db.add(BrokerSnapshot(owner_id=user.id, tenant_id=user.tenant_id, symbol="123456", kind="analysis_entry",
            environment="public", value={"code": "123456", "name": "분석전용", "market": "KRX", "asset_type": "stock", "status": "active"}))
        db.add(StockFactorProfile(owner_id=user.id, tenant_id=user.tenant_id, code="123456", value={
            "origin": "llm_default", "status": "ready", "selected_dimensions": ["sector-01"],
            "business_context": {"description": "검증용 사업 설명"}}))
    # The same analysis APIs work through explicit owner-private enrollment, not a watchlist bypass.
    assert admin.post("/api/vectors/analyze", json=payload()).status_code == 200
    response = admin.post("/api/vectors/attributes", json={"result_id": identity, "auto_initial": True})
    assert response.status_code == 200, response.text
    assert response.json()["origin"] == "recommendation_initial"
    assert response.json()["business_context"]["description"] == "검증용 사업 설명"
    manual = admin.post("/api/vectors/attributes", json={"result_id": identity}).json()
    assert manual["origin"] == "manual"
    repeated = admin.post("/api/vectors/attributes", json={"result_id": identity, "auto_initial": True}).json()
    assert repeated == manual
    with setup[1]() as db:
        assert db.scalar(select(WatchlistItem)) is None
