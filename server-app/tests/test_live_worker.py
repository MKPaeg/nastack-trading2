from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from nastack_trading.live_worker import LiveWorker, entry_window, leader
from nastack_trading.models import KiwoomConnection, TradeOrder, TradePlan, User, UserSettings
from nastack_trading.order_boundary import before_transmission
from nastack_trading.trading import PlanControl, TradingService, control_plan
from nastack_trading.trade_safety import RuntimeUpdate, save_automatic_runtime

AT = datetime.fromisoformat("2026-10-22T10:05:00+09:00")


class Gateway:
    def __init__(self):
        self.held = 0
        self.price = 100
        self.calls = []
        self.wire = []
        self.change = None
        self.timeout = False

    def position(self, db, user, plan, account):
        self.calls.append(("position", user.id, plan.code))
        return {"quantity": self.held, "purchase_price": 100, "environment": "live"}

    def quote(self, db, user, plan):
        return self.price

    def capacity(self, db, user, plan, account, side, *args):
        return (100 if side == "buy" else self.held), 10000, "live"

    def submit(self, db, user, plan, account, side, quantity, **kwargs):
        if self.change:
            self.change(db, user, plan)
        before_transmission()
        self.wire.append((user.id, plan.code, side, quantity, kwargs.get("limit_price")))
        if self.timeout:
            raise HTTPException(502, "ambiguous")
        self.held += quantity if side == "buy" else -quantity
        return str(len(self.wire))

    def fills(self, db, user, order):
        return {
            "filled_quantity": order.value["quantity"],
            "fill_price": order.value["reference_price"],
            "remaining_quantity": 0,
        }


@pytest.fixture
def runner(setup, monkeypatch):
    import nastack_trading.live_worker as live
    import nastack_trading.trading as trading

    monkeypatch.setattr(live, "now", lambda: AT)
    monkeypatch.setattr(trading, "now", lambda: AT)
    monkeypatch.setattr(live, "market_session", lambda: {"is_open": True})
    monkeypatch.setattr(trading, "market_session", lambda: {"is_open": True})
    monkeypatch.setattr(TradingService, "notify", lambda *args: None)
    monkeypatch.setattr(trading, "notify_automatic_control", lambda *args, **kwargs: None)
    config, factory = setup
    config = config.model_copy(
        update={"live_orders_enabled": True, "automatic_orders_enabled": True, "live_order_scope": {}}
    )
    with factory() as db:
        user = db.scalar(select(User))
        uid, tid = user.id, user.tenant_id
        row = db.get(UserSettings, uid)
        if not row:
            row = UserSettings(user_id=uid, tenant_id=tid, value={})
            db.add(row)
        row.value = {"trade_safety": {"new_orders_locked": False}}
        db.add(
            KiwoomConnection(
                owner_id=uid,
                tenant_id=tid,
                enabled=True,
                environment="live",
                version=1,
                token_scope="synthetic",
            )
        )
        plan = TradePlan(
            owner_id=uid,
            tenant_id=tid,
            broker="kiwoom",
            code="034220",
            version=1,
            value={
                "mode": "automatic",
                "name": "Synthetic",
                "active": True,
                "fraction": 100,
                "buy_rule": "price",
                "buy_price": 100,
                "take_profit": 5,
                "auto_max_quantity": 1,
                "auto_authorization": {
                    "key_version": 0,
                    "connection_version": 1,
                    "environment": "live",
                    "token_scope": "synthetic",
                },
                "auto_control": {"action": "start", "generation": "test-run", "requested_at": AT.isoformat()},
            },
        )
        db.add(plan)
        db.commit()
        pid = plan.id
        engine = db.get_bind()
    gateway = Gateway()
    worker = LiveWorker(config, engine, factory, gateway)
    return worker, gateway, factory, uid, tid, pid


def test_read_throttle_defers_decision_without_creating_or_sending_orders(runner, monkeypatch):
    from nastack_trading.namuh_reads import NamuhReadDeferred

    worker, gateway, factory, uid, tid, pid = runner

    def limited(*args):
        raise NamuhReadDeferred()

    monkeypatch.setattr(gateway, "position", limited)
    worker.tick()
    with factory() as db:
        status = db.get(TradePlan, pid).value["worker_status"]
        assert status["state"] == "waiting"
        assert "조회 호출 제한" in status["message"]
        assert not db.scalars(select(TradeOrder).where(TradeOrder.owner_id == uid)).all()
    assert gateway.wire == []


def test_inline_start_updates_running_plan_without_resetting_order_identity(runner):
    from nastack_trading.trading import PlanInput

    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        plan = db.get(TradePlan, pid)
        user = db.get(User, uid)
        result = control_plan(
            db,
            user,
            pid,
            PlanControl(
                version=plan.version,
                action="start",
                confirmation="실전 자동매매 시작 확인",
                settings=PlanInput(
                    code=plan.code,
                    broker=plan.broker,
                    version=plan.version,
                    buy_price=100,
                    fraction=100,
                    auto_max_quantity=3,
                ),
            ),
            worker.config,
        )
        assert result["active"]
        assert db.get(TradePlan, pid).value["auto_control"]["generation"] == "test-run"
    worker.tick()
    assert gateway.wire[0][3] == 3
    with factory() as db:
        plan = db.get(TradePlan, pid)
        control_plan(
            db,
            db.get(User, uid),
            pid,
            PlanControl(
                version=plan.version,
                action="start",
                confirmation="실전 자동매매 시작 확인",
                settings=PlanInput(
                    code=plan.code,
                    broker=plan.broker,
                    version=plan.version,
                    buy_price=101,
                    fraction=100,
                    auto_max_quantity=5,
                ),
            ),
            worker.config,
        )
    worker.tick()
    worker.tick()
    assert len(gateway.wire) == 1


def test_no_one_share_default_cap(runner):
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        plan = db.get(TradePlan, pid)
        plan.value = {**plan.value, "auto_max_quantity": None, "fraction": 10}
        db.commit()
    worker.tick()
    assert gateway.wire[0][3] == 10


@pytest.mark.parametrize("stop_price,price", [(None, 95), (97, 97)])
def test_stop_loss_sells_all_existing_holdings_not_quantity_cap(runner, stop_price, price):
    worker, gateway, factory, uid, tid, pid = runner
    gateway.held = 7
    gateway.price = price
    with factory() as db:
        plan = db.get(TradePlan, pid)
        plan.value = {
            **plan.value,
            "stop_loss_enabled": True,
            "stop_loss_rate": 5,
            "stop_loss_price": stop_price,
            "fraction": 10,
            "auto_max_quantity": 1,
        }
        db.commit()
    worker.tick()
    assert gateway.wire == [(uid, "034220", "sell", 7, None)]
    worker.tick()
    with factory() as db:
        sale = db.scalar(select(TradeOrder).where(TradeOrder.plan_id == pid))
        assert sale.status == "filled"
        assert sale.value["auto_exit_reason"] == "stop_loss"


def test_disabled_stop_loss_does_not_sell(runner):
    worker, gateway, factory, uid, tid, pid = runner
    gateway.held = 7
    gateway.price = 80
    worker.tick()
    assert gateway.wire == []


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_manual_entered_quantity_is_independent_of_automatic_policy(runner, side):
    from nastack_trading.trading import OrderPreview

    worker, gateway, factory, uid, tid, pid = runner
    gateway.held = 10
    with factory() as db:
        plan = db.get(TradePlan, pid)
        plan.value = {
            **plan.value,
            "mode": "manual",
            "active": False,
            "fraction": 10,
            "entry_policy": "buy_then_sell_all",
            "auto_max_quantity": 1,
        }
        db.commit()
        user = db.get(User, uid)
        intent = worker.service.preview(
            db,
            user,
            OrderPreview(plan_id=pid, side=side, quantity=3, limit_price=100 if side == "buy" else None),
        )
        assert intent.value["quantity"] == 3
        result = worker.service.execute(db, user, intent.id)
        assert result.status == "accepted"
    assert gateway.wire == [(uid, "034220", side, 3, 100 if side == "buy" else None)]
    assert gateway.held == (13 if side == "buy" else 7)


def test_manual_sell_rechecks_available_quantity_before_transmission(runner):
    from nastack_trading.trading import OrderPreview

    worker, gateway, factory, uid, tid, pid = runner
    gateway.held = 3
    with factory() as db:
        plan = db.get(TradePlan, pid)
        plan.value = {**plan.value, "mode": "manual", "active": False}
        db.commit()
        user = db.get(User, uid)
        with pytest.raises(HTTPException) as error:
            worker.service.preview(db, user, OrderPreview(plan_id=pid, side="sell", quantity=4))
        assert error.value.status_code == 422
        intent = worker.service.preview(db, user, OrderPreview(plan_id=pid, side="sell", quantity=3))
        gateway.held = 2  # Another fill changed the broker's available shares after preview.
        with pytest.raises(HTTPException) as error:
            worker.service.execute(db, user, intent.id)
        assert error.value.status_code == 409
    assert gateway.wire == []


def test_new_start_watches_existing_holding_without_additional_buy_and_sells_all(runner):
    from nastack_trading.trading import PlanInput

    worker, gateway, factory, uid, tid, pid = runner
    gateway.held = 2
    with factory() as db:
        plan = db.get(TradePlan, pid)
        control_plan(
            db,
            db.get(User, uid),
            pid,
            PlanControl(
                version=plan.version,
                action="start",
                confirmation="실전 자동매매 시작 확인",
                settings=PlanInput(
                    code=plan.code,
                    broker=plan.broker,
                    version=plan.version,
                    buy_price=100,
                    fraction=10,
                    auto_max_quantity=5,
                ),
            ),
            worker.config,
        )
    worker.tick()
    assert gateway.wire == []
    worker.tick()
    assert len(gateway.wire) == 0
    gateway.price = 105
    worker.tick()
    assert gateway.wire[-1] == (uid, "034220", "sell", 2, None)
    with factory() as db:
        sale = db.scalar(select(TradeOrder).where(TradeOrder.request_key.like("%:sell")))
        assert sale.value["sale_cost_basis"] == 100


def explicitly_start_stopped_plan(worker, factory, uid, pid, *, buy_enabled=True):
    with factory() as db:
        plan = db.get(TradePlan, pid)
        plan.value = {**plan.value, "active": False, "buy_enabled": buy_enabled}
        db.commit()
        control_plan(
            db,
            db.get(User, uid),
            pid,
            PlanControl(version=plan.version, action="start", confirmation="실전 자동매매 시작 확인"),
            worker.config,
        )


def test_explicit_start_buys_once_with_existing_holding_and_survives_restart(runner):
    worker, gateway, factory, uid, tid, pid = runner
    gateway.held = 2
    explicitly_start_stopped_plan(worker, factory, uid, pid)
    worker.tick()
    assert gateway.wire == [(uid, "034220", "buy", 1, 100)]
    assert gateway.held == 3
    restarted = LiveWorker(worker.config, worker.engine, factory, gateway)
    restarted.tick()
    restarted.tick()
    assert len(gateway.wire) == 1


def test_global_pause_resume_revokes_held_entry_but_keeps_exit_watching(runner):
    worker, gateway, factory, uid, tid, pid = runner
    gateway.held = 2
    explicitly_start_stopped_plan(worker, factory, uid, pid)
    with factory() as db:
        user = db.get(User, uid)
        save_automatic_runtime(db, user, RuntimeUpdate(version=0, paused=True))
        save_automatic_runtime(db, user, RuntimeUpdate(version=1, paused=False))
    worker.tick()
    assert gateway.wire == []
    gateway.price = 105
    worker.tick()
    assert gateway.wire == [(uid, "034220", "sell", 2, None)]


def test_pause_between_preview_and_execution_blocks_held_buy(runner):
    from nastack_trading.trading import OrderPreview

    worker, gateway, factory, uid, tid, pid = runner
    gateway.held = 2
    explicitly_start_stopped_plan(worker, factory, uid, pid)
    with factory() as db:
        user = db.get(User, uid)
        plan = db.get(TradePlan, pid)
        intent = worker.service.preview(
            db,
            user,
            OrderPreview(
                plan_id=pid,
                side="buy",
                request_key=f"auto:{pid}:{plan.value['auto_control']['generation']}:0:buy",
                limit_price=100,
            ),
            automatic=True,
        )
        save_automatic_runtime(db, user, RuntimeUpdate(version=0, paused=True))
        save_automatic_runtime(db, user, RuntimeUpdate(version=1, paused=False))
        with pytest.raises(HTTPException) as error:
            worker.service.execute(db, user, intent.id)
        assert error.value.status_code == 409
    assert gateway.wire == []


def test_explicit_start_does_not_buy_when_disabled_or_exit_condition_met(runner):
    worker, gateway, factory, uid, tid, pid = runner
    gateway.held = 2
    explicitly_start_stopped_plan(worker, factory, uid, pid, buy_enabled=False)
    worker.tick()
    assert gateway.wire == []
    explicitly_start_stopped_plan(worker, factory, uid, pid)
    gateway.price = 105
    worker.tick()
    assert gateway.wire == [(uid, "034220", "sell", 2, None)]


def test_inline_settings_cannot_switch_instrument(runner):
    from nastack_trading.trading import PlanInput

    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        plan = db.get(TradePlan, pid)
        with pytest.raises(HTTPException) as error:
            control_plan(
                db,
                db.get(User, uid),
                pid,
                PlanControl(
                    version=plan.version,
                    action="start",
                    confirmation="실전 자동매매 시작 확인",
                    settings=PlanInput(code="005930", broker="kiwoom", version=plan.version, buy_price=100),
                ),
                worker.config,
            )
        assert error.value.status_code == 422
    assert not gateway.wire


def test_buy_fills_goal_sell_and_restart_no_duplicates(runner):
    worker, gateway, factory, uid, tid, pid = runner
    worker.tick()
    assert gateway.wire == [(uid, "034220", "buy", 1, 100)]
    worker.tick()  # Reconcile the buy, no repeated buy.
    assert len(gateway.wire) == 1
    gateway.price = 105
    worker.tick()
    assert gateway.wire[-1] == (uid, "034220", "sell", 1, None)
    assert len(gateway.wire) == 2
    restarted = LiveWorker(worker.config, worker.engine, factory, gateway)
    restarted.tick()
    restarted.tick()
    assert len(gateway.wire) == 2
    with factory() as db:
        assert [
            o.status
            for o in db.scalars(select(TradeOrder).order_by(TradeOrder.created_at, TradeOrder.request_key))
        ].count("filled") == 2


def test_one_round_stops_after_sell_even_next_day(runner, monkeypatch):
    import nastack_trading.live_worker as live
    import nastack_trading.trading as trading

    worker, gateway, factory, uid, tid, pid = runner
    worker.tick()
    worker.tick()
    gateway.price = 105
    worker.tick()
    worker.tick()
    gateway.price = 99
    worker.tick()
    assert len(gateway.wire) == 2
    tomorrow = AT.replace(day=23)
    monkeypatch.setattr(live, "now", lambda: tomorrow)
    monkeypatch.setattr(trading, "now", lambda: tomorrow)
    worker.tick()
    assert len(gateway.wire) == 2
    with factory() as db:
        assert db.get(TradePlan, pid).value["active"] is False
        assert db.get(TradePlan, pid).value["worker_status"]["state"] == "completed"


def test_repeat_rounds_same_day_restart_day_rollover_and_block(runner, monkeypatch):
    import nastack_trading.live_worker as live
    import nastack_trading.trading as trading

    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        plan = db.get(TradePlan, pid)
        plan.value = {**plan.value, "repeat_trading": True}
        db.commit()
    worker.tick()
    worker.tick()
    gateway.price = 105
    worker.tick()
    worker.tick()  # filled sell, no buy above threshold
    gateway.price = 99
    restarted = LiveWorker(worker.config, worker.engine, factory, gateway)
    restarted.tick()
    restarted.tick()
    assert [r[2] for r in gateway.wire] == ["buy", "sell"]
    tomorrow = AT.replace(day=23)
    monkeypatch.setattr(live, "now", lambda: tomorrow)
    monkeypatch.setattr(trading, "now", lambda: tomorrow)
    gateway.price = 105
    restarted.tick()
    restarted.tick()
    assert [r[2] for r in gateway.wire] == ["buy", "sell"]
    gateway.price = 99
    with factory() as db:
        settings = db.get(UserSettings, uid)
        settings.value = {"trade_safety": {"new_orders_locked": True}}
        db.commit()
    restarted.tick()
    assert len(gateway.wire) == 2
    with factory() as db:
        assert db.get(TradePlan, pid).value["active"] is False


def test_repeat_waits_for_flat_position_after_sell(runner):
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        plan = db.get(TradePlan, pid)
        plan.value = {**plan.value, "repeat_trading": True}
        db.commit()
    worker.tick()
    worker.tick()
    gateway.price = 105
    worker.tick()
    gateway.held = 1  # Broker position is not yet flat, despite a filled sell ledger.
    worker.tick()
    worker.tick()
    assert len(gateway.wire) == 2
    with factory() as db:
        assert not db.get(TradePlan, pid).value["active"]  # Legacy repeat flag cannot restart.


def test_repeat_waits_for_sell_completion(runner, monkeypatch):
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        plan = db.get(TradePlan, pid)
        plan.value = {**plan.value, "repeat_trading": True}
        db.commit()
    worker.tick()
    worker.tick()
    gateway.price = 105
    worker.tick()
    monkeypatch.setattr(
        gateway, "fills", lambda *args: {"filled_quantity": 0, "remaining_quantity": 1, "fill_price": None}
    )
    gateway.price = 99
    worker.tick()
    worker.tick()
    assert len(gateway.wire) == 2  # Flat quote/position alone never authorizes a new round.


def test_control_transition_notifications_not_heartbeat_or_repeated_stop(runner, monkeypatch):
    import nastack_trading.trading as trading

    worker, gateway, factory, uid, tid, pid = runner
    events = []
    monkeypatch.setattr(
        trading,
        "notify_automatic_control",
        lambda db, u, cfg, p, event, *args: events.append((u.id, p.id, event)),
    )
    with factory() as db:
        user = db.get(User, uid)
        plan = db.get(TradePlan, pid)
        control_plan(db, user, pid, PlanControl(version=plan.version, action="stop"), worker.config)
        control_plan(db, user, pid, PlanControl(version=plan.version, action="stop"), worker.config)
        control_plan(
            db,
            user,
            pid,
            PlanControl(version=plan.version, action="start", confirmation="실전 자동매매 시작 확인"),
            worker.config,
        )
    assert events == [(uid, pid, "stop"), (uid, pid, "start")]


@pytest.mark.parametrize(
    "mutation", ["lock", "stop", "manual", "deactivate", "version", "connection", "shutdown"]
)
def test_final_wire_guard_rechecks_latest_settings(runner, mutation):
    worker, gateway, factory, uid, tid, pid = runner

    def change(db, user, plan):
        if mutation == "lock":
            settings = db.get(UserSettings, user.id)
            settings.value = {"trade_safety": {"new_orders_locked": True}}
        elif mutation == "deactivate":
            user.active = False
        elif mutation == "version":
            plan.version += 1
        elif mutation == "connection":
            db.get(KiwoomConnection, user.id).version += 1
        elif mutation == "shutdown":
            worker.stop_event.set()
        else:
            plan.value = {**plan.value, **({"active": False} if mutation == "stop" else {"mode": "manual"})}
        db.commit()

    gateway.change = change
    worker.tick()
    assert not gateway.wire
    with factory() as db:
        order = db.scalar(select(TradeOrder))
        assert order.status == "blocked"


def test_unknown_is_never_retransmitted(runner):
    worker, gateway, *_ = runner
    gateway.timeout = True
    worker.tick()
    worker.tick()
    worker.tick()
    assert len(gateway.wire) == 1
    with worker.factory() as db:
        assert db.scalar(select(TradeOrder)).status == "unknown"


def test_crash_after_broker_acceptance_before_local_ack_never_resubmits(runner, monkeypatch):
    from datetime import timedelta

    import nastack_trading.live_worker as live
    import nastack_trading.trading as trading

    worker, gateway, factory, uid, tid, pid = runner
    submit = gateway.submit

    def crash(*args, **kwargs):
        submit(*args, **kwargs)  # Broker accepted, but process dies before saving its order number.
        raise SystemExit("synthetic process termination")

    gateway.submit = crash
    with pytest.raises(SystemExit):
        worker.tick()
    with factory() as db:
        assert db.scalar(select(TradeOrder)).status == "submitting"
    gateway.submit = submit
    later = AT + timedelta(minutes=3)
    monkeypatch.setattr(live, "now", lambda: later)
    monkeypatch.setattr(trading, "now", lambda: later)
    restarted = LiveWorker(worker.config, worker.engine, factory, gateway)
    restarted.tick()
    restarted.tick()
    assert len(gateway.wire) == 1
    with factory() as db:
        assert db.scalar(select(TradeOrder)).status == "unknown"
        assert db.get(TradePlan, pid).value["active"] is True


def test_server_stop_keeps_plan_and_restart_reconciles_accepted_buy(runner):
    worker, gateway, factory, uid, tid, pid = runner
    worker.tick()
    worker.stop()
    with factory() as db:
        assert db.get(TradePlan, pid).value["active"] is True
        assert db.scalar(select(TradeOrder)).status == "accepted"
    restarted = LiveWorker(worker.config, worker.engine, factory, gateway)
    restarted.tick()
    assert len(gateway.wire) == 1
    with factory() as db:
        assert db.scalar(select(TradeOrder)).status == "filled"
    gateway.price = 105
    restarted.tick()
    assert [row[2] for row in gateway.wire] == ["buy", "sell"]


@pytest.mark.parametrize("phase", ["before_wire", "after_wire", "after_ack"])
def test_real_sigkill_recovery_with_isolated_database(runner, monkeypatch, phase):
    """Kill only our forked mock worker: no HTTP, real account, or runtime server."""
    import os
    import select as io_select
    import signal
    from datetime import timedelta

    import nastack_trading.live_worker as live
    import nastack_trading.trading as trading

    if not hasattr(os, "fork"):
        pytest.skip("SIGKILL worker test requires POSIX fork")
    worker, gateway, factory, uid, tid, pid = runner
    assert worker.engine.dialect.name == "sqlite" or "nastack_test" in str(worker.engine.url)
    worker.engine.dispose()  # Do not share pooled connections across fork.
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            submit = gateway.submit

            def pause_for_kill():
                os.write(write_fd, b"ready")
                while True:
                    signal.pause()

            def interrupted_submit(*args, **kwargs):
                if phase == "before_wire":
                    pause_for_kill()
                result = submit(*args, **kwargs)
                if phase == "after_wire":
                    pause_for_kill()
                return result

            gateway.submit = interrupted_submit
            worker.tick()
            pause_for_kill()  # accepted order already committed for after_ack.
        finally:
            os._exit(1)
    os.close(write_fd)
    try:
        ready, _, _ = io_select.select([read_fd], [], [], 15)
        assert ready and os.read(read_fd, 5) == b"ready", "mock worker did not reach crash boundary"
    finally:
        os.close(read_fd)
        os.kill(child, signal.SIGKILL)
        _, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    worker.engine.dispose()
    with factory() as db:
        plan = db.get(TradePlan, pid)
        assert plan.value["active"] is True
        assert plan.value["auto_control"]["generation"] == "test-run"
        order = db.scalar(select(TradeOrder))
        assert order.status == ("accepted" if phase == "after_ack" else "submitting")
    if phase != "before_wire":
        gateway.held = 1  # The independent mock broker retains the accepted position.
    later = AT + timedelta(minutes=3)
    monkeypatch.setattr(live, "now", lambda: later)
    monkeypatch.setattr(trading, "now", lambda: later)
    restarted = LiveWorker(worker.config, worker.engine, factory, gateway)
    restarted.tick()
    restarted.tick()
    assert gateway.wire == []  # Restart must not send a replacement buy.
    with factory() as db:
        assert db.scalar(select(TradeOrder)).status == ("filled" if phase == "after_ack" else "unknown")
    if phase == "after_ack":
        gateway.price = 105
        restarted.tick()
        assert [row[2] for row in gateway.wire] == ["sell"]


def test_another_session_can_block_after_credential_commit(runner):
    worker, gateway, factory, uid, tid, pid = runner

    def switch(db, user, plan):
        db.commit()  # A broker token refresh may release the preparation transaction.
        with factory.begin() as other:
            settings = other.get(UserSettings, uid)
            settings.value = {"trade_safety": {"version": 1, "new_orders_locked": True}}

    gateway.change = switch
    worker.tick()
    assert not gateway.wire
    with factory() as db:
        assert db.scalar(select(TradeOrder)).status == "blocked"


def test_actual_http_adapter_checks_after_credentials_before_wire(runner, monkeypatch):
    import httpx

    from nastack_trading.trade_gateway import TradeGateway

    worker, fake, factory, uid, tid, pid = runner
    requests, prepared = [], []

    def transport(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"return_code": 0, "ord_no": "synthetic"})

    gateway = TradeGateway(worker.config, kiwoom_transport=httpx.MockTransport(transport))
    for method in ["position", "capacity", "quote"]:
        monkeypatch.setattr(gateway, method, getattr(fake, method))

    def credentials(db, user, broker):
        prepared.append(broker)
        db.commit()
        with factory.begin() as other:
            settings = other.get(UserSettings, uid)
            settings.value = {"trade_safety": {"new_orders_locked": True}}
        return "live", {"authorization": "Bearer synthetic"}

    monkeypatch.setattr(gateway, "credentials", credentials)
    worker.service.gateway = gateway
    worker.tick()
    assert prepared == ["kiwoom"] and not requests
    with factory() as db:
        assert db.scalar(select(TradeOrder)).status == "blocked"


def test_safety_change_waits_for_already_entered_transmission_gate(runner):
    import threading

    from nastack_trading.trade_safety import SafetyUpdate, save_safety

    worker, gateway, factory, uid, tid, pid = runner
    started, finished = threading.Event(), threading.Event()
    failures = []

    def toggle():
        try:
            with factory() as db:
                user = db.get(User, uid)
                started.set()
                save_safety(db, user, worker.config, SafetyUpdate(version=0, new_orders_locked=True))
        except Exception as error:
            failures.append(type(error).__name__)
        finally:
            finished.set()

    threads = []

    def send(db, user, plan, account, side, quantity, **kwargs):
        before_transmission()
        thread = threading.Thread(target=toggle, daemon=True)
        threads.append(thread)
        thread.start()
        assert started.wait(5)
        assert not finished.wait(0.1)  # The competing safety write cannot pass the DB gate.
        gateway.wire.append((user.id, plan.code, side, quantity, kwargs.get("limit_price")))
        return "1"

    gateway.submit = send
    worker.tick()
    for thread in threads:
        thread.join(10)
    assert finished.is_set() and not failures
    assert len(gateway.wire) == 1
    worker.tick()
    assert len(gateway.wire) == 1


@pytest.mark.parametrize(
    "setting", ["live_orders_enabled", "automatic_orders_enabled", "live_worker_enabled"]
)
def test_server_gates_prevent_requests(runner, setting):
    worker, gateway, *_ = runner
    worker.config = worker.config.model_copy(update={setting: False})
    worker.service.config = worker.config
    worker.tick()
    assert not gateway.wire and not gateway.calls


def test_zero_targets_and_viewer_inactive_are_idle(runner):
    worker, gateway, factory, uid, tid, pid = runner
    with factory.begin() as db:
        db.get(TradePlan, pid).value = {"mode": "manual", "active": False}
    worker.tick()
    assert not gateway.calls
    for field, value in [("role", "viewer"), ("active", False)]:
        with factory.begin() as db:
            db.get(TradePlan, pid).value = {"mode": "automatic", "active": True}
            setattr(db.get(User, uid), field, value)
        worker.tick()
        assert not gateway.calls


def test_forecast_window_schedule_holiday_and_leader(runner, monkeypatch):
    worker, gateway, *_ = runner
    assert entry_window(
        {"buy_rule": "price", "auto_control": {"action": "schedule"}, "trade_time": "10:00"}, AT
    )
    assert not entry_window(
        {"buy_rule": "forecast", "auto_control": {"action": "schedule"}, "trade_time": "10:00"}, AT
    )
    assert not entry_window(
        {"buy_rule": "price", "auto_control": {"action": "schedule"}, "trade_time": "11:00"}, AT
    )
    monkeypatch.setattr("nastack_trading.live_worker.market_session", lambda: {"is_open": False})
    worker.tick()
    assert not gateway.calls
    with leader(worker.engine) as a:
        assert a
        with leader(worker.engine) as b:
            assert not b


def test_start_requires_explicit_approval_and_scoped_owner(runner):
    worker, gateway, factory, uid, tid, pid = runner
    with factory() as db:
        user = db.get(User, uid)
        with pytest.raises(HTTPException):
            control_plan(db, user, pid, PlanControl(version=1, action="start"), worker.config)
        db.rollback()
        stranger = SimpleNamespace(id="other", tenant_id=tid, active=True, role="trader")
        with pytest.raises(HTTPException) as e:
            control_plan(
                db,
                stranger,
                pid,
                PlanControl(version=1, action="start", confirmation="실전 자동매매 시작 확인"),
                worker.config,
            )
        assert e.value.status_code == 404
        db.rollback()
        saved = control_plan(
            db,
            user,
            pid,
            PlanControl(version=1, action="start", confirmation="실전 자동매매 시작 확인"),
            worker.config,
        )
        assert saved["active"] and saved["auto_control"]["state"] == "running"
    assert not gateway.calls


@pytest.mark.parametrize("prediction", [0.02, 0, -0.02, None])
def test_forecast_entry_requires_fresh_positive_result(runner, prediction):
    from nastack_trading.models import VectorResult

    worker, gateway, factory, uid, tid, pid = runner
    with factory.begin() as db:
        plan = db.get(TradePlan, pid)
        plan.value = {**plan.value, "buy_rule": "forecast", "forecast_source": "integrated"}
        db.add(
            VectorResult(
                owner_id=uid,
                tenant_id=tid,
                fingerprint="synthetic-vector",
                value={
                    "request": {"code": "034220"},
                    "today": "2026-10-22",
                    "data_cutoff": "2026-10-21",
                    "comparison": [{"method": "pearson", "forecast": {"predicted_return": prediction}}],
                },
            )
        )
    worker.tick()
    assert len(gateway.wire) == 0  # Even positive stored forecasts no longer authorize buys.


def test_stale_forecast_is_not_reused(runner):
    from nastack_trading.models import VectorResult

    worker, gateway, factory, uid, tid, pid = runner
    with factory.begin() as db:
        plan = db.get(TradePlan, pid)
        plan.value = {**plan.value, "buy_rule": "forecast", "forecast_source": "integrated"}
        db.add(
            VectorResult(
                owner_id=uid,
                tenant_id=tid,
                fingerprint="stale-vector",
                value={
                    "request": {"code": "034220"},
                    "today": "2026-10-22",
                    "data_cutoff": "2026-10-20",
                    "comparison": [{"method": "pearson", "forecast": {"predicted_return": 0.02}}],
                },
            )
        )
    worker.tick()
    assert not gateway.wire


def test_stop_preserves_reconciliation_and_no_new_order(runner):
    worker, gateway, factory, uid, tid, pid = runner
    worker.tick()
    with factory.begin() as db:
        plan = db.get(TradePlan, pid)
        plan.value = {**plan.value, "mode": "manual", "active": False}
        plan.version += 1
    gateway.price = 105
    worker.tick()
    assert len(gateway.wire) == 1
    with factory() as db:
        assert db.scalar(select(TradeOrder)).status == "filled"


def test_multi_user_tenant_and_multiple_symbols_are_isolated(runner):
    from nastack_trading.models import Tenant

    worker, gateway, factory, uid, tid, pid = runner
    positions = {}

    def position(db, user, plan, account):
        gateway.held = positions.get((user.id, plan.code), 0)
        return {"quantity": gateway.held, "purchase_price": 100, "environment": "live"}

    submit = gateway.submit

    def send(db, user, plan, account, side, quantity, **kwargs):
        result = submit(db, user, plan, account, side, quantity, **kwargs)
        positions[(user.id, plan.code)] = gateway.held
        return result

    gateway.position = position
    gateway.submit = send
    with factory.begin() as db:
        template = dict(db.get(TradePlan, pid).value)
        other_tenant = Tenant(name="isolated", timezone="Asia/Seoul")
        db.add(other_tenant)
        db.flush()
        ids = [uid]
        for index, tenant in enumerate([tid, other_tenant.id]):
            user = User(
                tenant_id=tenant,
                email=f"worker{index}@example.com",
                role="trader",
                active=True,
                code_hash="not-a-login",
            )
            db.add(user)
            db.flush()
            db.add(
                KiwoomConnection(
                    owner_id=user.id,
                    tenant_id=tenant,
                    enabled=True,
                    environment="live",
                    version=1,
                    token_scope="synthetic",
                )
            )
            ids.append(user.id)
            db.add(
                UserSettings(
                    user_id=user.id, tenant_id=tenant, value={"trade_safety": {"new_orders_locked": False}}
                )
            )
            db.add(
                TradePlan(owner_id=user.id, tenant_id=tenant, broker="kiwoom", code="034220", value=template)
            )
        db.add(TradePlan(owner_id=uid, tenant_id=tid, broker="kiwoom", code="046120", value=template))
    worker.tick()
    assert set(w[0] for w in gateway.wire) == set(ids)
    assert len(gateway.wire) == 3  # Only one unresolved order for the original user's broker.
    worker.tick()
    assert len(gateway.wire) == 4
    assert sum(w[0] == uid for w in gateway.wire) == 2
    with factory() as db:
        for order in db.scalars(select(TradeOrder)):
            plan = db.get(TradePlan, order.plan_id)
            assert (order.owner_id, order.tenant_id) == (plan.owner_id, plan.tenant_id)


def test_take_profit_toggle_keeps_round_and_applies_next_tick(runner):
    from nastack_trading.trading import PlanSettingUpdate, update_plan_setting

    worker, gateway, factory, uid, tid, pid = runner
    worker.tick()
    worker.tick()  # Buy filled.
    with factory() as db:
        plan = db.get(TradePlan, pid)
        user = db.get(User, uid)
        generation = plan.value.get("auto_control", {}).get("generation")
        update_plan_setting(
            db, user, pid, PlanSettingUpdate(version=plan.version, field="take_profit_enabled", value=0)
        )
    gateway.price = 110
    worker.tick()
    assert len(gateway.wire) == 1  # Above target, but disabled.
    with factory() as db:
        plan = db.get(TradePlan, pid)
        user = db.get(User, uid)
        assert plan.value.get("auto_control", {}).get("generation") == generation
        update_plan_setting(
            db, user, pid, PlanSettingUpdate(version=plan.version, field="take_profit_enabled", value=1)
        )
    worker.tick()
    assert len(gateway.wire) == 2  # Same round now exits.


def test_every_automatic_order_has_linked_durable_journal(runner):
    from nastack_trading.models import AuditEvent

    worker, gateway, factory, uid, tid, pid = runner
    worker.tick()
    worker.tick()
    gateway.price = 105
    worker.tick()
    worker.tick()
    with factory() as db:
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.actor_id == uid, AuditEvent.tenant_id == tid)
        ).all()
        for action in [
            "order_preview",
            "order_execution_requested",
            "order_submitting",
            "order_accepted",
            "fill_checked",
        ]:
            matching = [e for e in events if e.action == "trading." + action]
            assert len(matching) == 2
            for event in matching:
                assert event.detail["order_id"] == event.target_id
                assert event.detail["plan_id"] == pid
                assert event.detail["code"] == "034220" and event.detail["broker"] == "kiwoom"
                assert event.detail["quantity"] == 1
                assert "credential_scope" not in event.detail
