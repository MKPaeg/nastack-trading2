import shutil
import subprocess
from pathlib import Path

import pytest


def test_snap_is_nearby_only_and_keeps_manual_controls():
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required")
    source = (Path(__file__).resolve().parents[1] / "trade-ui.js").read_text()
    helper = source[
        source.index("function tradeSnapTarget") : source.index("function installTradeScrollSnap")
    ]
    subprocess.run(
        [
            executable,
            "-e",
            helper
            + """
const assert=require('node:assert/strict');
const r=top=>({getBoundingClientRect:()=>({top})});
const a=r(100),b=r(700);
assert.equal(tradeSnapTarget([a,b],84).row,a);
assert.equal(tradeSnapTarget([a,b],300),null);
assert.equal(tradeSnapTarget([a,b],690).row,b);
assert.equal(tradeSnapTarget([],84),null);
""",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "event.altKey" in source
    assert "input:focus,select:focus,textarea:focus" in source
    assert "target.row.dataset.tradeRow===last" in source
    assert "abort.abort()" in source
