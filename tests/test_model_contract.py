"""实际 SDK + HTTP 传输替身，只验证模型协议，不声称执行了真实推理。"""

import json

import httpx
import pytest

from hotline.extraction import ModelClient
from hotline.settings import ServiceError


@pytest.mark.parametrize("backend", ["kag", "openai"])
def test_model_messages_and_json_response(settings, backend):
    settings.llm_backend = backend
    settings.llm_base_url = "http://model.test/v1"
    settings.llm_model = "synthetic-contract-model"
    settings.llm_retries = 0
    model = ModelClient(settings)
    model._initialize()
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 0,
                "model": settings.llm_model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"entities":[],"relations":[]}'},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    transport = httpx.Client(transport=httpx.MockTransport(respond))
    if backend == "kag":
        model.client.client = model.client.client.with_options(http_client=transport)
    else:
        model.client = model.client.with_options(http_client=transport)
    messages = [{"role": "system", "content": "仅抽取原文"}, {"role": "user", "content": "合成测试"}]
    assert json.loads(model.complete(messages)) == {"entities": [], "relations": []}
    assert calls[0]["messages"] == messages
    assert calls[0]["model"] == settings.llm_model
    assert calls[0]["temperature"] == 0


def test_provider_errors_do_not_leak_secrets(settings):
    settings.llm_backend = "openai"
    settings.llm_base_url = "http://model.test/v1"
    settings.llm_model = "test"
    settings.llm_retries = 0
    model = ModelClient(settings)
    model._initialize()
    model.client = model.client.with_options(
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    401, json={"error": {"message": "secret-key-and-private-document"}}
                )
            )
        )
    )
    with pytest.raises(ServiceError) as error:
        model.complete([{"role": "user", "content": "测试"}])
    assert "secret-key" not in str(error.value)
    assert "模型调用失败" in str(error.value)
