"""Allowlisted trading journal payloads: never serialize broker credentials or account ciphertext."""

PLAN_FIELDS = {
    "name",
    "code",
    "broker",
    "account_last4",
    "mode",
    "fraction",
    "auto_max_quantity",
    "repeat_trading",
    "entry_policy",
    "buy_rule",
    "buy_price",
    "take_profit",
    "take_profit_enabled",
    "buy_enabled",
    "stop_loss_enabled",
    "stop_loss_rate",
    "stop_loss_price",
    "forecast_source",
    "trade_time",
    "active",
    "archived",
    "archived_at",
    "restored_at",
    "auto_control",
    "version",
}
ORDER_FIELDS = {
    "code",
    "name",
    "broker",
    "account_last4",
    "side",
    "quantity",
    "limit_price",
    "order_type",
    "reference_price",
    "reference_quote_id",
    "reference_quote_at",
    "transmission_reference_price",
    "transmission_quote_at",
    "transmission_quote_id",
    "purpose",
    "plan_version",
    "auto_generation",
    "auto_exit_reason",
    "filled_quantity",
    "fill_price",
    "remaining_quantity",
    "broker_order_no",
    "cancel_order_no",
    "note",
    "notification",
    "email_notification",
    "submit_started_at",
    "sale_cost_basis",
    "sale_cost_basis_at",
    "realized_profit",
    "realized_return_rate",
    "realized_cost_status",
}
EXTRA_FIELDS = {
    "plan_id",
    "order_id",
    "version",
    "time",
    "active",
    "before",
    "after",
    "status",
    "reason",
    "http_status",
    "state",
    "message",
    "field",
    "value",
    "action",
    "requested_at",
    "generation",
    "confirmation",
    "new_orders_locked",
    "event",
}


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if k in PLAN_FIELDS | ORDER_FIELDS | EXTRA_FIELDS}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def plan_detail(plan):
    return {
        "plan_id": plan.id,
        "code": plan.code,
        "broker": plan.broker,
        "version": plan.version,
        **{k: v for k, v in plan.value.items() if k in PLAN_FIELDS},
    }


def order_detail(order):
    return {
        "order_id": order.id,
        "plan_id": order.plan_id,
        "status": order.status,
        **{k: v for k, v in order.value.items() if k in ORDER_FIELDS},
    }
