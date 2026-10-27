import json

import httpx
import pytest

from nastack_trading.config import Config
from nastack_trading.news import EvaluationError, evaluate


def assessment(article_id, **changes):
    return {
        "article_id": article_id,
        "dimension_id": "stock-123456",
        "score9": 1,
        "reason": "원문 사실에 근거한 참고 평가",
        "evidence": "원문 그대로 인용",
        "relevance": "direct",
        **changes,
    }


def run(items, *, cloud=False, response=None):
    articles = [
        {"id": key, "title": "원문 그대로 인용", "excerpt": "사업 자료\n실제 공급 사실"}
        for key in ["one", "two"]
    ]

    def handler(request):
        payload = json.loads(request.content)
        body = json.loads(payload["input"] if cloud else payload["messages"][1]["content"])
        for a in body["articles"]:
            assert a["evidence_candidates"]
            assert all(q in a["title"] + " " + a["excerpt"] for q in a["evidence_candidates"])
        if response is not None:
            return httpx.Response(200, json=response)
        text = json.dumps({"assessments": items}, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    return evaluate(
        Config(vllm_base_url="http://model.test/v1", vllm_model_id="test", _env_file=None),
        articles,
        [{"id": "stock-123456", "name": "시험기업", "family": "stock"}],
        httpx.MockTransport(handler),
        {"provider": "openai", "model": "test"} if cloud else None,
        {"code": "123456"},
    )


def test_bad_quote_does_not_discard_valid_article_and_diagnostics_have_no_raw_text():
    with pytest.raises(EvaluationError) as result:
        run([assessment("one"), assessment("two", evidence="provider-private-raw-text-not-in-source")])
    error = result.value
    assert error.diagnostics["reason"] == "EVIDENCE_NOT_IN_ORIGINAL"
    assert [a["article_id"] for a in error.validated] == ["one"]
    assert "provider-private" not in json.dumps(error.diagnostics)


def test_duplicate_article_is_not_partially_accepted():
    with pytest.raises(EvaluationError) as result:
        run([assessment("one"), assessment("one", score9=-1), assessment("two")])
    assert result.value.diagnostics["reason"] == "DUPLICATE_ASSESSMENT"
    assert [a["article_id"] for a in result.value.validated] == ["two"]


def test_invalid_schema_discards_only_the_affected_article():
    with pytest.raises(EvaluationError) as result:
        run([assessment("one"), assessment("two", score9="bad")])
    assert result.value.diagnostics["reason"] == "OUTPUT_SCHEMA"
    assert [a["article_id"] for a in result.value.validated] == ["one"]
    assert "bad" not in json.dumps(result.value.diagnostics)


@pytest.mark.parametrize("score", [True, 2.5, "1", 5])
def test_scores_remain_strict(score):
    with pytest.raises(EvaluationError) as result:
        run([assessment("one", score9=score), assessment("two")])
    assert result.value.diagnostics["reason"] == "OUTPUT_SCHEMA"


def test_uncertain_scored_and_missing_stock_are_separate_failures():
    with pytest.raises(EvaluationError) as result:
        run([assessment("one", relevance="uncertain")])
    assert {i["reason"] for i in result.value.diagnostics["items"]} == {
        "UNCERTAIN_WITH_SCORE",
        "MISSING_STOCK_ASSESSMENT",
    }
    assert not result.value.validated


def test_truncated_output_is_not_misreported_as_bad_evidence():
    with pytest.raises(EvaluationError) as result:
        run(
            [],
            cloud=True,
            response={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
            },
        )
    assert result.value.diagnostics["reason"] == "OUTPUT_INCOMPLETE"
    assert result.value.diagnostics["token_limit_reached"]
