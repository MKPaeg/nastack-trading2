"""Exercise the production chart in a real DOM, without paid/broker calls."""
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_chart_rolling_today_custom_end_and_live_labels(role):
    source = (Path(__file__).resolve().parents[1] / "app.js").read_text()
    chart_code = source[source.index("function priceText("):source.index("// Dimension synchronization")]
    node_code = source[source.index("function node("):source.index("function showToast(")]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content('<main id="chart"></main>')
        page.evaluate("""() => {
          window.testNow = '2026-10-26T10:00:00+09:00';
          const RealDate = Date;
          window.Date = class extends RealDate {
            constructor(...args) { super(...(args.length ? args : [window.testNow])); }
            static now() { return new RealDate(window.testNow).getTime(); }
          };
        }""")
        page.add_script_tag(content=f"""
          const state = {{user:{{id:'a',tenant_id:'one',role:'{role}',timezone:'Asia/Seoul'}},
            chartStartByCode:new Map(),chartEndByCode:new Map()}};
          const calls=[],saved=[];
          let bars=[{{date:'2026-10-23',close:5910,volume:100}},
                    {{date:'2026-10-26',close:7680,volume:200,provisional:true}}];
          async function api(path,options) {{calls.push([path,JSON.parse(options.body)]);return {{chart:bars,remaining:[]}};}}
          function showToast() {{}}
          function persistScreenState() {{saved.push(Object.fromEntries(state.chartEndByCode));}}
          {node_code}
          {chart_code}
          function draw() {{document.querySelector('#chart').replaceChildren(chart(bars,'338220'));}}
          draw();
        """)
        end = page.get_by_label("차트 종료일", exact=True)
        expect(end).to_have_value("2026-10-26")
        expect(page.get_by_text("2026-10-26 · 현재가 (장중 잠정)", exact=False)).to_be_visible()
        assert page.evaluate("calls.length") == 0
        # Source daily close replaces the provisional price after market close.
        page.evaluate("bars[1].provisional=false;draw()")
        expect(page.get_by_text("2026-10-26 · 종가 7,680 원", exact=False)).to_be_visible()
        # Explicit end is kept across redraws and can be serialized/restored.
        end.fill("2026-10-23")
        end.dispatch_event("change")
        expect(page.get_by_text("2026-10-23 · 종가 5,910 원", exact=False)).to_be_visible()
        page.evaluate("state.chartEndByCode=new Map(Object.entries(saved.at(-1)));draw()")
        expect(end).to_have_value("2026-10-23")
        page.get_by_role("button", name="오늘까지", exact=True).click()
        expect(end).to_have_value("2026-10-26")
        assert page.evaluate("Object.keys(saved.at(-1)).length") == 0
        # Rolling mode follows tomorrow, using the previous trading day's close pre-open.
        page.evaluate("window.testNow='2026-10-27T08:00:00+09:00';draw()")
        expect(end).to_have_value("2026-10-27")
        expect(page.get_by_text("2026-10-26 · 종가 7,680 원", exact=False)).to_be_visible()
        # Editing only the start must not freeze today's end date.
        page.get_by_label("차트 시작일", exact=True).fill("2026-02-01")
        page.get_by_label("차트 시작일", exact=True).dispatch_event("change")
        assert page.evaluate("state.chartEndByCode.size") == 0
        # Another symbol has no access to this symbol's manual end.
        page.evaluate("state.chartEndByCode.set('338220','2026-10-23');document.querySelector('#chart').replaceChildren(chart(bars,'328130'))")
        expect(end).to_have_value("2026-10-27")
        if role == "viewer":
            assert page.evaluate("calls.length") == 0
        else:
            assert page.evaluate("calls.every(([path])=>path==='/vectors/stock-history')")
        page.screenshot(path=f"/private/tmp/nastack-chart-today-{role}.png")
        browser.close()
