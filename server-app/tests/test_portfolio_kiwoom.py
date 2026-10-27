import httpx
from conftest import CODE, EMAIL, ORIGIN
from fastapi.testclient import TestClient
from test_kiwoom import configuration, provider

from nastack_trading.app import create_app


def test_kiwoom_holdings_and_period_returns(setup):
    calls = []
    auth = provider(calls)

    def respond(request):
        api = request.headers.get("api-id")
        if api == "kt00018":
            return httpx.Response(
                200,
                json={
                    "return_code": 0,
                    "tot_evlt_amt": "10000",
                    "tot_evlt_pl": "1000",
                    "tot_prft_rt": "11.11",
                    "acnt_evlt_remn_indv_tot": [
                        {
                            "stk_cd": "A005930",
                            "stk_nm": "삼성전자",
                            "rmnd_qty": "10",
                            "pur_pric": "900",
                            "cur_prc": "1000",
                            "evlt_amt": "10000",
                            "evltv_prft": "1000",
                        }
                    ],
                },
            )
        if api == "kt00016":
            return httpx.Response(
                200,
                json={
                    "return_code": 0,
                    "prft_rt": "-2.5",
                    "tot_amt_fr": "10000",
                    "tot_amt_to": "9750",
                    "evltv_prft": "-250",
                },
            )
        return auth.handle_request(request)

    with TestClient(
        create_app(configuration(setup[0]), kiwoom_transport=httpx.MockTransport(respond)),
        headers={"origin": ORIGIN},
    ) as client:
        assert client.post("/api/auth/login", json={"email": EMAIL, "access_code": CODE}).status_code == 200
        saved = client.put(
            "/api/admin/brokers/kiwoom", json={"version": 0, "enabled": True, "environment": "live"}
        )
        assert saved.status_code == 200
        result = client.post("/api/portfolio/kiwoom/holdings/refresh")
        assert result.status_code == 200, result.text
        snapshot = result.json()["snapshot"]
        assert snapshot["items"][0]["code"] == "005930"
        assert snapshot["items"][0]["quantity"] == 10
        assert snapshot["items"][0]["purchase_price"] == 900
        assert client.get("/api/portfolio/holdings").json()["snapshot"] is None
        returns = client.post(
            "/api/portfolio/kiwoom/returns/refresh", json={"start": "2026-01-01", "end": "2026-10-21"}
        )
        assert returns.status_code == 200, returns.text
        assert returns.json()["snapshot"]["prft_rt"] == -2.5
        assert (
            client.post(
                "/api/portfolio/kiwoom/returns/refresh", json={"start": "2026-10-21", "end": "2026-01-01"}
            ).status_code
            == 422
        )
