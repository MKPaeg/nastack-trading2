from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_partial_failure_message_retry_and_no_implicit_paid_calls(role):
    root=Path(__file__).resolve().parents[1]
    app=(root/"app.js").read_text()
    nodes=app[app.index("function node("):app.index("function showToast(")]
    ui=(root/"collection-ui.js").read_text().split("function collectionWorkflowView",1)[1]
    with sync_playwright() as p:
        browser=p.chromium.launch();page=browser.new_page();page.set_content('<main id="main"></main>')
        page.add_style_tag(path=str(root/"live.css"))
        page.add_script_tag(content=nodes+f"""
          const state={{user:{{id:'a',role:'{role}'}}}};
          let calls=0;
          async function refreshBusinessCollection(){{calls++;return {{status:'completed'}};}}
          function collectionWorkflowView{ui}
          const data={{status:'partial',error:'MODEL_INVALID_EVIDENCE_OR_SCORE',stage:'완료',pending:1,
            evaluation_diagnostics:{{reason:'EVIDENCE_NOT_IN_ORIGINAL',items:[{{article_id:'synthetic-article',reason:'EVIDENCE_NOT_IN_ORIGINAL'}}],remaining_batches_continued:true}}}};
          document.querySelector('#main').append(collectionWorkflowView(data,'123456'));
        """)
        expect(page.get_by_text('실패 기사는 미평가로 분리하고 나머지 묶음은 계속 평가했습니다.',exact=False)).to_be_visible()
        retry=page.get_by_role('button',name='실패 단계만 다시 실행')
        if role=='viewer':
            expect(retry).to_have_count(0)
        else:
            expect(retry).to_be_visible()
            # Cancellation never sends a request. Merely viewing is read-only.
            page.on('dialog',lambda dialog:dialog.dismiss())
            retry.click()
        assert page.evaluate('calls')==0
        page.screenshot(path=f'/private/tmp/nastack-news-partial-{role}.png')
        browser.close()
