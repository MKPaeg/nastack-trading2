import argparse

import pytest

from nastack_trading.server import lan_address, private_same_origin


@pytest.mark.parametrize("address", ["192.168.45.223", "192.168.45.224", "10.0.0.5"])
def test_dynamic_private_same_origin(address):
    assert private_same_origin(f"http://{address}:8080", f"http://{address}:8080/api/auth/request-code")


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example:8080",
        "http://8.8.8.8:8080",
        "null",
        None,
        "http://192.168.45.224:8080",
        "http://192.168.45.223:8081",
        "https://192.168.45.223:8080",
        "http://user@192.168.45.223:8080",
    ],
)
def test_cross_origin_public_or_malformed_rejected(origin):
    assert not private_same_origin(origin, "http://192.168.45.223:8080/api/auth/request-code")


def test_lan_origin_permission_requires_explicit_development_setting(setup):
    from fastapi.testclient import TestClient
    from nastack_trading.app import create_app
    for enabled in (False, True):
        config = setup[0].model_copy(update={"development_lan_same_origin": enabled})
        with TestClient(create_app(config), base_url="http://192.168.45.224:8080") as client:
            result = client.post("/api/nonexistent", headers={"origin":"http://192.168.45.224:8080"})
            assert result.status_code == (404 if enabled else 403)
            assert client.post("/api/nonexistent", headers={"origin":"http://evil.example"}).status_code == 403


@pytest.mark.parametrize("address", ["192.168.45.122", "10.0.0.2", "172.16.0.2"])
def test_lan_binding_explicit_private_ipv4(address):
    assert lan_address(address) == address


@pytest.mark.parametrize("address", ["0.0.0.0", "127.0.0.1", "8.8.8.8", "::1", "localhost", "169.254.1.2"])
def test_public_and_wildcard_bind_rejected(address):
    with pytest.raises(argparse.ArgumentTypeError):
        lan_address(address)
