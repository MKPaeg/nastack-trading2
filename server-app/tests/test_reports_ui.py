import shutil
import subprocess
from pathlib import Path

import pytest


def test_collection_navigation_precedes_analysis_and_report_label_is_consistent():
    root = Path(__file__).resolve().parents[1]
    html = (root / "index.html").read_text()
    assert html.index('data-view="data"') < html.index('data-view="analysis"')
    assert 'data-view="reports">분석 리포트</button>' in html
    assert 'reports: "분석 리포트"' in (root / "app.js").read_text()
    assert 'panel("분석 리포트")' in (root / "reports-ui.js").read_text()
    assert 'reports:["분석 리포트",' in (root / "screen-help.js").read_text()


def test_report_unknown_values_and_account_switch_fence():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required")
    source = (Path(__file__).resolve().parents[1] / "reports-ui.js").read_text()
    harness = """
const assert=require('node:assert/strict');
class Element {
  constructor(){this.children=[];this.isConnected=true;this.value='';}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  setAttribute(){}
  addEventListener(){}
}
const state={user:{id:'A'},view:'reports'};
const root=new Element(),node=()=>new Element(),panel=node,$=()=>root;
let resolve, saved=0;
const api=()=>new Promise(done=>{resolve=done;});
const persistScreenState=()=>saved++;
const formatTime=v=>v;
"""
    checks = """
assert.equal(reportNumber(null),'미확인');
assert.equal(reportNumber(undefined),'미확인');
assert.equal(reportNumber(0),'0');
assert.equal(reportForecast(null),'추정 불가');
assert.match(reportForecast({predicted_return:null,reason:'표본 부족'}),/표본 부족/);
assert.match(reportForecast({predicted_return:0,predicted_close:100}),/0%/);
(async()=>{
  const pending=renderReports();
  state.user={id:'B'};
  resolve({counts:{vector:1},items:[{id:'private-A'}],total:1});
  await pending;
  assert.equal(saved,0);
  assert.equal(state.reportFilters,undefined);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    subprocess.run([node, "-e", harness + source + checks], check=True, capture_output=True, text=True)
