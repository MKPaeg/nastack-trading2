import shutil
import subprocess
from pathlib import Path

import pytest


def test_detail_and_tab_share_default_vector_request():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    source = (Path(__file__).resolve().parents[1] / "app.js").read_text()
    helper = (
        "function vectorAnalysisRequest"
        + source.split("function vectorAnalysisRequest", 1)[1].split("function renderDetailAnalysis", 1)[0]
    )
    checks = """
const assert=require('node:assert/strict');
const range={start:'2026-01-01',end:'2026-10-23'},dims=['b','a'];
const detail=vectorAnalysisRequest('034220',range,dims);
const tab=vectorAnalysisRequest('034220',range,dims,{horizons:[1,5,21],window:60,levels:5,method:'pearson',end_mode:'latest_completed'});
assert.deepEqual(detail,tab);assert.equal(detail.input_policy,'lagged_available');
assert.deepEqual(detail.dimensions,['a','b']);
assert.notDeepEqual(detail,vectorAnalysisRequest('034220',range,dims,{window:120}));
"""
    subprocess.run([node, "-e", helper + checks], check=True, capture_output=True, text=True)
    assert "const request=vectorAnalysisRequest(item.code,range,dimensions)" in source
    assert "const request=vectorAnalysisRequest(item.code,{start:vectorDraft.start" in source
