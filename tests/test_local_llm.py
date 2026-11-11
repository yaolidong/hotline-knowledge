import json

import httpx

from hotline.local_llm import (
    LOCAL_PRESET_URLS,
    apply_env_updates,
    is_local_preset_url,
    list_model_ids,
    model_present,
    probe_local_llm,
    should_update_timeout,
)


def test_preset_urls():
    assert is_local_preset_url("http://127.0.0.1:11434/v1")
    assert is_local_preset_url("http://127.0.0.1:8000/v1/")
    assert not is_local_preset_url("https://api.deepseek.com")
    assert "http://127.0.0.1:11434/v1" in LOCAL_PRESET_URLS


def test_list_model_ids_openai_and_ollama_shapes():
    assert list_model_ids({"data": [{"id": "qwen2.5:7b-instruct"}]}) == ["qwen2.5:7b-instruct"]
    assert list_model_ids({"models": [{"name": "qwen2.5:7b-instruct"}]}) == ["qwen2.5:7b-instruct"]
    assert model_present(["Qwen/Qwen2.5-14B-Instruct"], "Qwen/Qwen2.5-14B-Instruct")


def test_probe_success_with_json_object(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.read()))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen2.5:7b-instruct"}]})
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["model"] == "qwen2.5:7b-instruct"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"ok":true}'},
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:11434/v1")
    report = probe_local_llm(
        base_url="http://127.0.0.1:11434/v1",
        model="qwen2.5:7b-instruct",
        client=client,
    )
    assert report["status"] == "ok"
    assert report["reachable"] is True
    assert report["model_present"] is True
    assert report["json_object_ok"] is True
    assert isinstance(report["latency_ms"], int)


def test_probe_rejects_missing_model():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "other"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = probe_local_llm(base_url="http://127.0.0.1:11434/v1", model="missing", client=client)
    assert report["status"] == "model_missing"
    assert report["json_object_ok"] is False


def test_probe_rejects_non_json_mode_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        return httpx.Response(400, json={"error": "response_format not supported"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = probe_local_llm(base_url="http://127.0.0.1:8000/v1", model="m", client=client)
    assert report["status"] == "json_unsupported"


def test_probe_chat_timeout_is_unreachable_not_json_unsupported():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        raise httpx.TimeoutException("read timeout")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = probe_local_llm(base_url="http://127.0.0.1:11434/v1", model="m", client=client)
    assert report["status"] == "unreachable"
    assert "HOTLINE_LLM_TIMEOUT" in report["detail"]


def test_probe_models_timeout_is_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("connect timeout")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = probe_local_llm(base_url="http://127.0.0.1:11434/v1", model="m", client=client)
    assert report["status"] == "unreachable"
    assert "超时" in report["detail"]


def test_apply_env_updates_and_timeout_rule():
    text = "HOTLINE_LLM_BASE_URL=https://api.deepseek.com\nHOTLINE_LLM_TIMEOUT=90\n"
    out = apply_env_updates(
        text,
        {
            "HOTLINE_LLM_BACKEND": "openai",
            "HOTLINE_LLM_BASE_URL": "http://127.0.0.1:11434/v1",
            "HOTLINE_LLM_MODEL": "qwen2.5:7b-instruct",
        },
    )
    assert "HOTLINE_LLM_BASE_URL=http://127.0.0.1:11434/v1" in out
    assert "HOTLINE_LLM_BACKEND=openai" in out
    assert should_update_timeout(None) is True
    assert should_update_timeout("90") is True
    assert should_update_timeout("300") is False
    assert should_update_timeout("60") is False
