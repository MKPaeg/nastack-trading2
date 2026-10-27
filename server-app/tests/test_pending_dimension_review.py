"""Research evidence contracts only; no network, credentials or production DB."""
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def probe_module():
    spec = importlib.util.spec_from_file_location("pending_probe", ROOT / "scripts/probe_pending_dimensions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plan_covers_all_current_pending_items_once():
    from nastack_trading.dimension_catalog import DIMENSIONS
    text = (ROOT / "docs/research/2026-10-27-pending-dimensions-plan.md").read_text()
    ids = re.findall(r"^\| ((?:sector|commodity|market|control)-\d+) ", text, re.M)
    assert len(ids) == len(set(ids)) == 25
    from nastack_trading.dimension_worldbank import SERIES
    assert set(ids) == {d["id"] for d in DIMENSIONS if not d["can_collect"]} | set(SERIES) | {"commodity-11", "control-11"}


def test_worldbank_repeat_and_period_counts():
    base = ROOT / "docs/research/external-data/probes"
    one = json.loads((base / "2026-10-27-pending-public-01.json").read_text())["results"][0]
    four = json.loads((base / "2026-10-27-pending-public-04.json").read_text())["results"][0]
    assert one["sha256"] == four["sha256"]
    names = {"Zinc", "Aluminum", "Lead", "Tin", "Iron ore, cfr spot", "Crude oil, Dubai",
             "Natural gas, Europe", "Soybean oil", "Palm oil", "Coffee, Arabica", "Coffee, Robusta",
             "Cotton, A Index", "Rubber, RSS3", "Rubber, TSR20 **"}
    series = [s for s in four["series"] if s["name"] in names]
    assert len(series) == 14  # 12 dimensions: coffee and rubber each have two alternatives.
    for row in series:
        assert row["count_since_2020"] == 81
        assert row["missing_since_2020"] == row["duplicate_periods"] == 0
        assert row["positive"] and row["last"][0] == "2026M09"


def test_price_summary_rejects_missing_and_invalid_not_zero_fill():
    summarize = probe_module().price_summary
    output = summarize({"data": {"defaultMnrl": [
        {"crtrYmd": "20261023", "cmercPrc": "47.51"},
        {"crtrYmd": "20261024", "cmercPrc": "47.51"},
        {"crtrYmd": "bad", "cmercPrc": "4"},
        {"crtrYmd": "20261022", "cmercPrc": "-"},
        {"crtrYmd": "20261021", "cmercPrc": "0"}]}})
    assert output["valid_positive_count"] == 2 and output["invalid_count"] == 3
    assert output["weekend_dates"] == 1 and output["unchanged_adjacent_values"] == 1


def test_probe_urls_fixed_public_and_no_key_or_db_import():
    module = probe_module()
    assert all(url.startswith("https://") for url in module.SOURCES.values())
    source = (ROOT / "scripts/probe_pending_dimensions.py").read_text()
    assert not any(x in source for x in ("sqlalchemy", "os.environ", "AUTH_KEY", "load_dotenv"))
    for form in module.FORMS.values():
        assert not any("key" in key.lower() for key in form)


def test_generated_inventory_contains_the_same_review():
    data = json.loads((ROOT / "docs/research/external-data/inventory.json").read_text())
    review = data["pending_dimension_review"]
    assert len(review["rows"]) == 25
    assert review["plan_markdown"] == (ROOT / "docs/research/2026-10-27-pending-dimensions-plan.md").read_text()
    from nastack_trading.dimension_worldbank import SERIES
    completed = {r["id"] for r in review["rows"] if r["implementation_complete"]}
    assert completed == set(SERIES) | {"commodity-11", "control-11"} and len(completed) == 13
    assert sum(not r["implementation_complete"] for r in review["rows"]) == 12
    assert all(r["implementation_scope"] == ("conditional_daily_collection_view" if r["id"] == "control-11" else "monthly_collection_view") for r in review["rows"] if r["implementation_complete"])
