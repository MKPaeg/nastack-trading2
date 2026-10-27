import json

import httpx
import pytest

from nastack_trading.news_translation import translate
from nastack_trading.research import ResearchError


def test_translation_preserves_ids_and_requires_korean(setup):
    config = setup[0].model_copy(update={"vllm_base_url": "http://model.test/v1"})
    articles = [{"id": "a", "title": "Wind generation rose", "excerpt": "Up 68% in ISO-NE."}]

    def transport(summary):
        return httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps({"translations": [{"id": "a", "summary_ko": summary}]})
                            }
                        }
                    ]
                },
            )
        )

    model = {"provider": "vllm", "model": "test"}
    result = translate(config, articles, model, transport("ISO-NE 풍력 발전량이 68% 증가했다."))
    assert result["a"]["summary_ko"].endswith("증가했다.")
    assert result["a"]["translation_model"] == "test"
    with pytest.raises(ResearchError):
        translate(config, articles, model, transport("Still English"))
