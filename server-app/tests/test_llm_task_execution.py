import json

import httpx
from test_research import fixtures, provider

from nastack_trading.news_translation import translate
from nastack_trading.research import ResearchPipeline


def test_research_uses_its_own_snapshot_prompt_and_retains_fixed_output_contract():
    config, run = fixtures()
    transport, calls = provider()
    prompt = {
        "text": "연구 보고서 전용 사용자 지침: 사업의 불확실성을 먼저 요약하세요.",
        "version": 7,
        "sha256": "saved-hash",
    }
    result = ResearchPipeline(config, transport, prompt=prompt)(run)
    messages = calls[-1][1]["messages"]
    assert prompt["text"] in messages[0]["content"]
    assert "UNTRUSTED" in messages[0]["content"]
    assert result["prompt"] == prompt and result["trading_enabled"] is False


def test_translation_uses_its_prompt_without_changing_stock_scores(setup):
    requests = []
    prompt = {
        "text": "번역 전용 사용자 지침: 간결한 문장으로 한국어 번역을 작성하세요.",
        "version": 3,
        "sha256": "translation-hash",
    }

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"translations": [{"id": "a", "summary_ko": "제공된 수치가 증가했다."}]}
                            )
                        }
                    }
                ]
            },
        )

    result = translate(
        setup[0].model_copy(update={"vllm_base_url": "http://model.test/v1"}),
        [{"id": "a", "title": "Data increased"}],
        {"provider": "vllm", "model": "test"},
        httpx.MockTransport(handle),
        prompt,
    )
    assert prompt["text"] in requests[0]["messages"][0]["content"]
    assert "JSON" in requests[0]["messages"][0]["content"]
    assert "score" not in result["a"]
