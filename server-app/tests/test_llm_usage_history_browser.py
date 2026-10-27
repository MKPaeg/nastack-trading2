"""The private settings history is readable and stays in its own account view."""

from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def test_settings_history_period_model_category_cost_and_owner_guard():
    root = Path(__file__).resolve().parents[1]
    app = (root / "app.js").read_text()
    nodes = app[app.index("function node("):app.index("function showToast(")]
    widget = (root / "llm-settings-ui.js").read_text().split("async function renderLLMTasks", 1)[0]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 780})
        page.set_content('<section id="settings"><form id="settingsForm"></form></section>')
        for name in ("styles.css", "live.css", "mobile.css"):
            page.add_style_tag(path=str(root / name))
        page.add_script_tag(content=f"""
          let state={{user:{{id:'a',tenant_id:'one',role:'viewer'}},sessionEpoch:1}};
          {nodes}
          {widget}
          function seoulDate(){{return '2026-10-27';}}
          const pending=[],requests=[];
          async function api(path){{requests.push(path);return new Promise(resolve=>pending.push(resolve));}}
          const sample={{start:'2026-10-27',end:'2026-10-27',summary:{{calls:2,reported_calls:2,priced_calls:1,
            total_tokens:1250,estimated_usd:'0.0080000000',unknown_cost_calls:1}},
            by_day:[{{date:'2026-10-27',calls:2,total_tokens:1250,estimated_usd:'0.0080000000',unknown_cost_calls:1}}],
            by_model:[{{provider:'openai',model:'gpt-6.1-sol',calls:1,total_tokens:1200,estimated_usd:'0.0080000000',unknown_cost_calls:0}},
                      {{provider:'local_model',model:'owned-vllm',calls:1,total_tokens:50,estimated_usd:'0',unknown_cost_calls:1}}],
            by_category:[{{category:'collection',calls:1,total_tokens:1200,estimated_usd:'0.0080000000',unknown_cost_calls:0}},
                         {{category:'evaluation',calls:1,total_tokens:50,estimated_usd:'0',unknown_cost_calls:1}}],
            recent:[{{occurred_at:'2026-10-27T08:00:00+09:00',category:'collection',provider:'openai',
              model:'gpt-6.1-sol',input_tokens:1000,output_tokens:200,total_tokens:1200,estimated_usd:'0.0080000000'}}],
            note:'과거 호출은 소급 집계하지 않습니다.'}};
          renderLLMUsagePanel(document.querySelector('#settings'));
        """)
        assert page.get_by_label("LLM 사용량 기간").input_value() == "month"
        assert "period=month" in page.evaluate("requests[0]")
        page.evaluate("pending[0](sample)")
        history = page.locator(".usage-history")
        expect(history).to_contain_text("gpt-6.1-sol")
        expect(history).to_contain_text("owned-vllm")
        expect(history).to_contain_text("수집")
        expect(history).to_contain_text("평가")
        expect(history).to_contain_text("US$0.008000")
        expect(history).to_contain_text("비용 미확인")
        groups = history.locator(".usage-table-group")
        assert groups.count() == 4
        group_colors = [groups.nth(index).evaluate("el => getComputedStyle(el).backgroundColor") for index in range(4)]
        assert group_colors[0] == group_colors[2]
        assert group_colors[1] == group_colors[3]
        assert group_colors[0] != group_colors[1]
        for index in range(4):
            header_color = groups.nth(index).locator("th").first.evaluate("el => getComputedStyle(el).backgroundColor")
            row_color = groups.nth(index).locator("td").first.evaluate("el => getComputedStyle(el).backgroundColor")
            assert header_color != row_color
            assert row_color != group_colors[index]
        category_rows = history.locator(".usage-category-table tr:has(td)")
        assert category_rows.count() == 2
        colors = [category_rows.nth(index).locator("td").first.evaluate(
            "cell => getComputedStyle(cell).backgroundColor") for index in range(2)]
        assert colors[0] != colors[1]
        page.get_by_label("LLM 사용량 기간").select_option("year")
        assert "period=year" in page.evaluate("requests[1]")
        page.evaluate("state.user={id:'a',tenant_id:'two',role:'viewer'};pending[1](sample)")
        page.wait_for_timeout(100)
        expect(history).to_contain_text("2026-10-27 ~ 2026-10-27")
        assert history.evaluate("el=>el.scrollWidth<=el.clientWidth+1")
        page.screenshot(path="/private/tmp/nastack-llm-usage-history.png", full_page=True)
        browser.close()
