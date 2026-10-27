import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from nastack_trading.app import create_app
from nastack_trading.model_catalog import CACHE, cloud_catalog, cloud_model_allowed, gpt_at_least_56


@pytest.mark.parametrize(
    "model,allowed",
    [
        ("gpt-5.6-luna", True),
        ("gpt-5.6-sol-2026-01-01", True),
        ("gpt-5.10", True),
        ("gpt-6-astra", True),
        ("gpt-6.1-sol", True),
        ("gpt-5.5", False),
        ("gpt-5-mini", False),
        ("gpt-4.1", False),
        ("o3", False),
        ("gpt-image-2", False),
        ("gpt-56evil", False),
    ],
)
def test_version_filter(model, allowed):
    assert gpt_at_least_56(model) is allowed


def test_account_discovery_read_only_and_cached(setup):
    CACHE.clear()
    config = setup[0].model_copy(
        update={
            "openai_api_key": SecretStr("synthetic-catalog-test"),
            "cloud_model_allowlist": ["gpt-5.6-luna"],
        }
    )
    calls = []

    def handler(request):
        assert request.method == "GET"
        assert str(request.url) == "https://api.openai.com/v1/models"
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": model}
                    for model in ["gpt-5.6-luna", "gpt-6-astra", "gpt-6.1-sol", "gpt-5.5", "gpt-image-2"]
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    result = cloud_catalog(config, transport, discover=True)
    assert result["ids"] == ["gpt-6.1-sol", "gpt-6-astra", "gpt-5.6-luna"]
    assert cloud_model_allowed(config, "gpt-6-astra", transport)
    assert not cloud_model_allowed(config, "gpt-999-nonexistent", transport)
    assert len(calls) == 1
    assert "synthetic-catalog-test" not in repr(CACHE)
    failed = cloud_catalog(config, httpx.MockTransport(lambda _: httpx.Response(401)), refresh=True)
    assert failed["ids"] == result["ids"] and failed["error"]


def test_shared_catalog_endpoint_does_not_change_model_or_cloud_policy(admin, setup):
    CACHE.clear()
    config = setup[0].model_copy(
        update={
            "openai_api_key": SecretStr("synthetic-catalog-endpoint"),
            "cloud_model_allowlist": ["gpt-5.6-luna"],
        }
    )
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"data": [{"id": "gpt-6-astra"}, {"id": "gpt-5.6-sol"}]})
    )
    with TestClient(
        create_app(config, news_transport=transport),
        headers={"origin": "http://testserver"},
        cookies=admin.cookies,
    ) as client:
        before = client.get("/api/settings").json()
        result = client.get("/api/models").json()
        assert {item["model"] for item in result["items"] if item["provider"] == "openai"} == {
            "gpt-6-astra",
            "gpt-5.6-sol",
            "gpt-5.6-luna",
        }
        assert client.get("/api/settings").json() == before
        assert client.get("/api/admin/vector-defaults").json()["allow_cloud"] is False
        choice = {
            **before["value"],
            "cloud_model": "gpt-6-astra",
            "analysis_model": {"provider": "openai", "model_id": "gpt-6-astra", "base_url": ""},
        }
        saved = client.put("/api/settings", json={"version": before["version"], "value": choice})
        assert saved.status_code == 200, saved.text
        assert client.get("/api/settings").json()["value"]["cloud_model"] == "gpt-6-astra"
        assert client.get("/api/news/models").json()["default"]["model"] == "gpt-6-astra"
        assert client.get("/api/settings").json()["value"]["cloud_enabled"] is False
