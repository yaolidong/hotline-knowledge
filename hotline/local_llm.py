"""本地 OpenAI 兼容 LLM 探测与 .env 辅助；不加载模型权重。"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

LOCAL_PRESET_URLS = frozenset(
    {
        "http://127.0.0.1:11434/v1",
        "http://127.0.0.1:8000/v1",
    }
)


def normalize_base_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def is_local_preset_url(url: str) -> bool:
    return normalize_base_url(url) in LOCAL_PRESET_URLS


def list_model_ids(payload: dict | list) -> list[str]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            items = payload["data"]
        elif isinstance(payload.get("models"), list):
            items = payload["models"]
        else:
            items = []
    else:
        items = []
    ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("id") or item.get("name") or item.get("model")
        if isinstance(value, str) and value:
            ids.append(value)
    return ids


def model_present(model_ids: list[str], model: str) -> bool:
    return model in model_ids


def apply_env_updates(content: str, updates: dict[str, str]) -> str:
    text = content
    for name, value in updates.items():
        line = f"{name}={value}"
        if re.search(rf"^{re.escape(name)}=", text, flags=re.MULTILINE):
            text = re.sub(rf"^{re.escape(name)}=.*$", line, text, flags=re.MULTILINE)
        else:
            text = text.rstrip() + "\n" + line + "\n"
    return text


def should_update_timeout(current: str | None) -> bool:
    if current is None or current == "":
        return True
    return current.strip() == "90"


def probe_local_llm(
    *,
    base_url: str,
    model: str,
    api_key: str = "",
    timeout: float = 60.0,
    trust_env: bool = False,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    base = normalize_base_url(base_url)
    report: dict[str, Any] = {
        "status": "error",
        "reachable": False,
        "model_present": False,
        "json_object_ok": False,
        "latency_ms": None,
        "detail": "",
        "base_url": base,
        "model": model,
    }
    if not base or not model:
        report["status"] = "未配置"
        report["detail"] = "缺少 HOTLINE_LLM_BASE_URL 或 HOTLINE_LLM_MODEL"
        return report

    headers = {}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    owns_client = client is None
    http = client or httpx.Client(timeout=timeout, trust_env=trust_env, headers=headers)
    started = time.perf_counter()
    try:
        try:
            models_response = http.get(base + "/models")
        except httpx.TimeoutException:
            report["status"] = "unreachable"
            report["detail"] = "模型响应超时，请提高 HOTLINE_LLM_TIMEOUT 或等待模型加载"
            return report
        except httpx.HTTPError:
            report["status"] = "unreachable"
            report["detail"] = "无法连接本地模型服务，请先启动对应 compose profile"
            return report
        if models_response.status_code >= 400:
            report["status"] = "unreachable"
            report["detail"] = f"models 接口 HTTP {models_response.status_code}"
            return report
        report["reachable"] = True
        try:
            payload = models_response.json()
        except ValueError:
            report["status"] = "error"
            report["detail"] = "models 接口未返回 JSON"
            return report
        ids = list_model_ids(payload)
        if not model_present(ids, model):
            report["status"] = "model_missing"
            report["detail"] = "已连接但未找到配置的模型名"
            return report
        report["model_present"] = True

        try:
            chat = http.post(
                base + "/chat/completions",
                json={
                    "model": model,
                    "temperature": 0,
                    "max_tokens": 64,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": "只返回 JSON 对象。"},
                        {"role": "user", "content": '返回 {"ok":true}'},
                    ],
                },
            )
        except httpx.TimeoutException:
            report["status"] = "unreachable"
            report["detail"] = "模型响应超时，请提高 HOTLINE_LLM_TIMEOUT 或等待模型加载"
            return report
        except httpx.HTTPError:
            report["status"] = "unreachable"
            report["detail"] = "无法连接本地模型服务，请先启动对应 compose profile"
            return report
        if chat.status_code >= 400:
            report["status"] = "json_unsupported"
            report["detail"] = f"JSON mode 不可用：HTTP {chat.status_code}"
            return report
        try:
            body = chat.json()
            choice = body["choices"][0]
            if choice.get("finish_reason") not in (None, "stop"):
                report["status"] = "json_unsupported"
                report["detail"] = "模型输出被截断或不完整"
                return report
            content = choice["message"]["content"]
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("not object")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            report["status"] = "json_unsupported"
            report["detail"] = "未返回可解析的 JSON 对象"
            return report
        report["json_object_ok"] = True
        report["status"] = "ok"
        report["detail"] = "本地模型可达且支持 json_object"
        return report
    finally:
        report["latency_ms"] = int((time.perf_counter() - started) * 1000)
        if owns_client:
            http.close()
