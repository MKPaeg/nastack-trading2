import shutil
import subprocess
from pathlib import Path

import pytest


def test_automatic_activity_truthful_status_without_orders():
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required")
    root = Path(__file__).resolve().parents[1]
    source = (root / "auto-trade-ui.js").read_text()
    helper = source.split("function paintAutomaticActivity", 1)[0]
    script = (
        helper
        + """
const assert=require('node:assert/strict');
const at=Date.parse('2026-10-22T14:00:00+09:00');
const w={running:true,last_tick:new Date(at-5000).toISOString(),poll_seconds:5};
const p={active:true,status:{state:'holding',message:'목표 수익률 감시'}};
assert.equal(automaticActivity(p,w,{},at).animate,true);
assert.equal(automaticActivity(p,w,{},at).label,'자동매매 감시 중');
assert.equal(automaticActivity(p,w,{new_orders_locked:true},at).animate,false);
assert.equal(automaticActivity(p,w,{new_orders_locked:true},at).kind,'blocked');
assert.equal(automaticActivity(p,w,{server_locked:true},at).kind,'blocked');
assert.equal(automaticActivity(p,w,{automatic_locked:true},at).kind,'blocked');
assert.equal(automaticActivity(p,w,{automatic_paused:true},at).label,'자동매매 일시정지');
assert.equal(automaticActivity(p,w,{automatic_paused:true},at).animate,false);
assert.equal(automaticActivity({...p,active:false},w,{},at).kind,'stopped');
assert.equal(automaticActivity(p,{...w,running:false},{},at).animate,false);
assert.equal(automaticActivity(p,w,{},at+60000).kind,'unknown');
assert.equal(automaticActivity({...p,status:{state:'waiting',message:'예약 시간 대기'}},w,{},at).kind,'waiting');
assert.equal(automaticActivity({...p,status:{state:'error'}},w,{},at).animate,false);
assert.equal(automaticActivity({...p,status:{state:'waiting',message:'나무증권 조회 호출 제한 · 이번 주기 매매 판단 보류'}},w,{},at).label,'자동매매 조회 대기');
assert.equal(automaticActivity({...p,status:{state:'waiting',message:'나무증권 조회 호출 제한'}},w,{},at).animate,false);
assert.equal(automaticActivity({...p,status:{state:'accepted'}},w,{},at).label,'자동 주문 체결 확인 중');
"""
    )
    subprocess.run([executable, "-e", script], check=True, capture_output=True, text=True)
    assert "appendAutomaticActivity(name,plan,metadata)" in (root / "trade-ui.js").read_text()
    assert "owner!==state.user?.id" in source
    assert 'api("/trading/safety")' in source
    assert ".auto-activity .spinner" in (root / "live.css").read_text()
    css = (root / "live.css").read_text()
    assert "@keyframes auto-activity-background" in css
    assert ".auto-activity-running { animation:none; }" in css


def test_activity_refresh_preserves_rotating_node():
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required")
    source = (Path(__file__).resolve().parents[1] / "auto-trade-ui.js").read_text()
    script = source.split("function appendAutomaticActivity", 1)[0] + """
const assert=require('node:assert/strict');
const node=(tag,text,cls)=>({textContent:text,className:cls,setAttribute(){}});
const badge={dataset:{},children:[],setAttribute(){},replaceChildren(...nodes){this.children=nodes;}};
const worker={running:true,last_tick:new Date().toISOString()};
const plan={active:true,status:{state:'holding'}};
paintAutomaticActivity(badge,plan,worker,{});
const icon=badge.children[0];
paintAutomaticActivity(badge,plan,worker,{});
assert.equal(badge.children[0],icon);
paintAutomaticActivity(badge,plan,worker,{new_orders_locked:true});
assert.notEqual(badge.children[0],icon);
assert.equal(badge.dataset.animated,'false');
"""
    subprocess.run([executable, "-e", script], check=True, capture_output=True, text=True)
