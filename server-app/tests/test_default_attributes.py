import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from test_vectors import seed_vector_data

from nastack_trading.app import create_app
from nastack_trading.models import BrokerSnapshot, StockFactorProfile, User


@pytest.fixture(autouse=True)
def synthetic_business_evidence(monkeypatch):
    from nastack_trading.business_context import REVIEWED

    for code, name in [("123456", "합성 종목"), ("654321", "합성 반도체"), ("111111", "합성 등록 종목")]:
        monkeypatch.setitem(
            REVIEWED, code, (name, "합성 반도체 제조 사업", "https://issuer.test/business", ["반도체"])
        )


def test_one_run_cloud_consent_preserves_automatic_permissions(admin, setup):
    from nastack_trading.default_attributes import generate
    from nastack_trading.models import UserSettings

    config = setup[0].model_copy(
        update={"openai_api_key": SecretStr("test-key"), "cloud_model_allowlist": ["gpt-5.6-luna"]}
    )
    with setup[1]() as db:
        user = db.scalar(select(User))
        owner = user.id
        settings = db.get(UserSettings, owner)
        settings.value = {
            **settings.value,
            "analysis_model": {"provider": "openai", "model_id": "gpt-5.6-luna"},
            "cloud_model": "gpt-5.6-luna",
            "cloud_enabled": False,
            "default_attributes_cloud_enabled": False,
        }
        db.add(
            StockFactorProfile(
                owner_id=owner,
                tenant_id=user.tenant_id,
                code="123456",
                value={
                    "origin": "llm_default",
                    "status": "pending",
                    "instrument": {"code": "123456", "name": "합성 종목"},
                },
            )
        )
        db.commit()

    def handler(request):
        assert str(request.url) == "https://api.openai.com/v1/responses"
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "items": [
                                            {
                                                "code": "123456",
                                                "dimension_ids": ["sector-01"],
                                                "reason": "합성 제안",
                                            }
                                        ]
                                    }
                                ),
                            }
                        ]
                    }
                ]
            },
        )

    generate(setup[1], config, owner, ["123456"], httpx.MockTransport(handler), cloud_confirmed=True)
    with setup[1]() as db:
        assert db.get(StockFactorProfile, (owner, "123456")).value["status"] == "ready"
        value = db.get(UserSettings, owner).value
        assert value["cloud_enabled"] is False
        assert value["default_attributes_cloud_enabled"] is False


def client_for(admin, setup, handler):
    config = setup[0].model_copy(
        update={
            "vllm_base_url": "http://model.test/v1",
            "vllm_model_id": "admin-default-test",
            "openai_api_key": SecretStr(""),
        }
    )
    return TestClient(
        create_app(config, news_transport=httpx.MockTransport(handler)),
        headers={"origin": "http://testserver"},
        cookies=admin.cookies,
    )


def test_attribute_policy_save_without_model_preserves_the_single_analysis_model(admin, setup):
    from nastack_trading.models import UserSettings

    with setup[1]() as db:
        user = db.scalar(select(User))
        before = db.get(UserSettings, user.id).value
        analysis_model = before["analysis_model"]
        cloud_model = before["cloud_model"]
    current = admin.get("/api/user/vector-defaults").json()
    response = admin.put(
        "/api/user/vector-defaults",
        json={
            "version": current["version"],
            "enabled": False,
            "allow_cloud": False,
            "research_business": False,
        },
    )
    assert response.status_code == 200, response.text
    with setup[1]() as db:
        saved = db.get(UserSettings, user.id).value
        assert saved["analysis_model"] == analysis_model
        assert saved["cloud_model"] == cloud_model
        assert saved["default_attributes_enabled"] is False


@pytest.mark.parametrize("self_reference", [True, False])
def test_same_instrument_blocked_but_related_etf_proxy_allowed(admin, setup, monkeypatch, self_reference):
    from nastack_trading import default_attributes
    from nastack_trading.models import UserSettings

    monkeypatch.setattr(
        default_attributes,
        "DIMENSIONS",
        [
            {
                "id": "sector-01",
                "name": "반도체",
                "can_collect": True,
                "series_code": "123456" if self_reference else "654321",
            }
        ],
    )
    with setup[1].begin() as db:
        user = db.scalar(select(User))
        uid = user.id
        settings = db.get(UserSettings, uid)
        settings.value = {**settings.value, "analysis_model": {"provider": "vllm", "model_id": "test-model"}}
        db.add(
            StockFactorProfile(
                owner_id=uid,
                tenant_id=user.tenant_id,
                code="123456",
                value={
                    "origin": "llm_default",
                    "status": "pending",
                    "instrument": {"code": "123456", "name": "합성 종목", "asset_type": "etf"},
                },
            )
        )
    # Source-backed context is supplied independently of ETF name inference.
    monkeypatch.setattr(
        default_attributes,
        "resolve_business",
        lambda *a: {
            "description": "반도체 지수 ETF",
            "sources": ["https://issuer.test/fund"],
            "topics": ["반도체"],
        },
    )

    def handler(request):
        body = json.loads(request.content)
        assert "자기참조" in body["messages"][0]["content"]
        assert "같은 산업의 다른 ETF" in body["messages"][0]["content"]
        text = json.dumps(
            {
                "items": [
                    {
                        "code": "123456",
                        "dimension_ids": ["sector-01"],
                        "reason": "다른 ETF 대체지표이며 중복 노출·레버리지 위험 검토 필요",
                    }
                ]
            }
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    config = setup[0].model_copy(
        update={"vllm_base_url": "http://model.test/v1", "vllm_model_id": "test-model"}
    )
    default_attributes.generate(setup[1], config, uid, ["123456"], httpx.MockTransport(handler))
    with setup[1]() as db:
        value = db.get(StockFactorProfile, (uid, "123456")).value
        assert value["selected_dimensions"] == ([] if self_reference else ["sector-01"])
        assert value["status"] == ("failed" if self_reference else "ready")


def test_initialize_existing_watch_and_holdings_without_private_data(admin, setup):
    seed_vector_data(setup[1])
    with setup[1]() as db:
        user = db.scalar(select(User))
        db.add(
            BrokerSnapshot(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                kind="holdings",
                symbol="",
                environment="live",
                value={
                    "account_last4": "9999",
                    "items": [{"code": "654321", "name": "합성 반도체", "quantity": 12345}],
                },
            )
        )
        db.commit()
    calls = []

    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "admin-default-test"
        content = json.loads(body["messages"][1]["content"])
        assert all(item["business_context"]["description"] for item in content["instruments"])
        assert all(item["business_context"]["sources"] for item in content["instruments"])
        assert "9999" not in body["messages"][1]["content"]
        assert "quantity" not in body["messages"][1]["content"]
        calls.append(content)
        report = {
            "items": [
                {"code": item["code"], "dimension_ids": ["sector-01"], "reason": "합성 미검증 기본 제안"}
                for item in content["instruments"]
            ]
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(report)}}]})

    with client_for(admin, setup, handler) as client:
        result = client.post("/api/vectors/defaults-initialize")
        assert result.status_code == 200, result.text
        assert result.json()["total"] == 2
        assert len(calls) == 1
        for code in ["123456", "654321"]:
            profile = client.get(f"/api/vectors/defaults/{code}").json()["profile"]
            assert profile["origin"] == "llm_default"
            assert profile["status"] == "ready"
            assert profile["selected_dimensions"] == ["sector-01"]
        assert len(client.get("/api/vectors/instruments").json()["items"]) == 2
        client.post("/api/vectors/defaults-initialize")
        assert len(calls) == 1  # Ready defaults are not regenerated on every refresh.


def test_manual_attributes_never_overwritten_and_failure_does_not_block_registration(admin, setup):
    seed_vector_data(setup[1])
    with setup[1]() as db:
        user = db.scalar(select(User))
        db.add(
            StockFactorProfile(
                owner_id=user.id,
                tenant_id=user.tenant_id,
                code="123456",
                value={"origin": "manual", "selected_dimensions": ["sector-02"], "factors": []},
            )
        )
        db.commit()

    def handler(request):
        raise httpx.ConnectError("synthetic unavailable")

    with client_for(admin, setup, handler) as client:
        result = client.post("/api/vectors/defaults-initialize").json()
        assert result["manual_preserved"] == 1
        instrument = client.post(
            "/api/admin/instruments",
            json={
                "code": "111111",
                "name": "합성 등록 종목",
                "market": "KOSPI",
                "asset_type": "stock",
                "source_note": "테스트 확인",
            },
        )
        assert instrument.status_code == 201, instrument.text
        added = client.post("/api/watchlist", json={"instrument_id": instrument.json()["id"]})
        assert added.status_code == 201, added.text
        profile = client.get("/api/vectors/defaults/111111").json()["profile"]
        assert profile["status"] == "failed"
        assert profile["selected_dimensions"] == []
        assert client.get("/api/vectors/defaults/123456").json()["profile"]["origin"] == "manual"


def test_invalid_dimension_ids_are_not_saved(admin, setup):
    seed_vector_data(setup[1])

    def handler(request):
        report = {"items": [{"code": "123456", "dimension_ids": ["not-a-dimension"], "reason": "invalid"}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(report)}}]})

    with client_for(admin, setup, handler) as client:
        client.post("/api/vectors/defaults-initialize")
        profile = client.get("/api/vectors/defaults/123456").json()["profile"]
        assert profile["status"] == "failed"
        assert profile["error"] == "INVALID_DEFAULT_PROPOSAL"
        assert profile["selected_dimensions"] == []


def test_admin_connection_probe_is_explicit_and_does_not_initialize(admin, setup):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    with client_for(admin, setup, handler) as client:
        assert client.get("/api/admin/vector-defaults").status_code == 200
        assert not calls
        result = client.post("/api/admin/vector-defaults/verify", json={})
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "verified"
        assert len(calls) == 1
    with setup[1]() as db:
        assert db.scalar(select(StockFactorProfile)) is None


def test_market_only_is_review_not_ready(admin, setup):
    seed_vector_data(setup[1])

    def handler(request):
        report = {"items": [{"code": "123456", "dimension_ids": ["market-01"], "reason": "시장만 확인"}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(report)}}]})

    with client_for(admin, setup, handler) as client:
        client.post("/api/vectors/defaults-initialize")
        profile = client.get("/api/vectors/defaults/123456").json()["profile"]
        assert profile["status"] == "needs_review"


def test_no_business_evidence_does_not_call_model(admin, setup, monkeypatch):
    from nastack_trading.business_context import REVIEWED

    seed_vector_data(setup[1])
    monkeypatch.delitem(REVIEWED, "123456")

    def handler(request):
        raise AssertionError("No model call without business evidence")

    with client_for(admin, setup, handler) as client:
        client.post("/api/vectors/defaults-initialize")
        profile = client.get("/api/vectors/defaults/123456").json()["profile"]
        assert profile["status"] == "needs_review"
        assert profile["error"] == "BUSINESS_INFO_REQUIRED"
        assert profile["selected_dimensions"] == []


def test_cloud_blocked_uses_reviewed_business_rules_without_enabling_cloud(admin, setup):
    from nastack_trading.default_attributes import generate
    from nastack_trading.models import UserSettings

    config = setup[0].model_copy(
        update={"openai_api_key": SecretStr("test-key"), "cloud_model_allowlist": ["gpt-5.6-luna"]}
    )
    with setup[1]() as db:
        user = db.scalar(select(User))
        owner = user.id
        settings = db.get(UserSettings, owner)
        settings.value = {
            **settings.value,
            "analysis_model": {"provider": "openai"},
            "cloud_model": "gpt-5.6-luna",
            "cloud_enabled": False,
            "default_attributes_cloud_enabled": False,
        }
        db.add(
            StockFactorProfile(
                owner_id=owner,
                tenant_id=user.tenant_id,
                code="066570",
                value={
                    "origin": "llm_default",
                    "status": "pending",
                    "instrument": {"name": "LG전자", "market": "KOSPI", "asset_type": "stock"},
                },
            )
        )
        db.commit()

    def handler(request):
        raise AssertionError("Automatic cloud inference is disabled")

    generate(setup[1], config, owner, ["066570"], httpx.MockTransport(handler))
    with setup[1]() as db:
        profile = db.get(StockFactorProfile, (owner, "066570")).value
        assert profile["status"] == "ready"
        assert "sector-37" in profile["selected_dimensions"]
        assert profile["business_context"]["description"]
        assert profile["generation_method"] == "reviewed_business_rules"
        assert profile["llm_used"] is False and profile["model"] is None
        assert profile["error"] is None
        assert db.get(UserSettings, owner).value["cloud_enabled"] is False
