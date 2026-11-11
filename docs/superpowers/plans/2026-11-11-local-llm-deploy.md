# 本地大模型推理服务接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为政务热线知识图谱提供可切换的本地 LLM（Ollama + vLLM）独立推理部署、`.env` 配置脚本，以及可达/模型/JSON 健康检查，使构建不局限于第三方云 API。

**Architecture:** 新增独立 Compose 项目 `hotline-llm`（`deploy/compose.llm.yml` + profiles），与 OpenSPG 栈解耦。探测逻辑集中在 `hotline/local_llm.py`，由 `check`/`configure`/`doctor` 共用。应用继续只走现有 `ModelClient` + `HOTLINE_LLM_*`（本地强制 `BACKEND=openai`）。

**Tech Stack:** Docker Compose profiles、httpx、python-dotenv、现有 pydantic-settings / pytest；Ollama 与 vLLM OpenAI 兼容 HTTP API。

**Spec:** `docs/superpowers/specs/2026-11-11-local-llm-deploy-design.md`

## Global Constraints

- 不在 `hotline` 进程内加载模型权重；不改 KAG/OpenSPG 上游。
- 不把 LLM 服务写入 `deploy/compose.override.yml`；Compose 项目名固定 `hotline-llm`。
- 本地写入 `.env` 时 `HOTLINE_LLM_BACKEND=openai`；JSON 探测必须带 `response_format={"type":"json_object"}`，失败不得静默降级。
- 本机预设 URL 仅：`http://127.0.0.1:11434/v1`（Ollama）、`http://127.0.0.1:8000/v1`（vLLM）。
- 非本机预设 `HOTLINE_LLM_BASE_URL` 默认拒绝覆盖，除非 `--force`。
- 不打印 API Key；备份 `.env` 到 `data/config-backups/`。
- 默认 CI 不下载真实大模型、不要求 GPU；真实抽取闭环非本轮必验。
- 提交 git **仅当用户明确要求**；计划中的 Commit 步骤在未授权时跳过并记录「待用户授权提交」。
- 交流与用户可见文档保持中文；代码标识符用英文。

## File Structure

| 文件 | 职责 |
| --- | --- |
| `hotline/local_llm.py` | 预设 URL、模型列表解析、JSON 探测、报告结构、`.env` 键更新辅助 |
| `tests/test_local_llm.py` | mock httpx 覆盖探测、覆盖保护、超时写入规则 |
| `scripts/prepare_local_llm.py` | 生成 `deploy/llm.env` |
| `scripts/check_local_llm.py` | CLI 健康检查 |
| `scripts/configure_local_llm.py` | 探测通过后写 `.env`；Ollama 可 docker exec pull |
| `deploy/compose.llm.yml` | ollama / vllm profiles |
| `deploy/llm.env.example` | 模板 |
| `.gitignore` | 忽略 `deploy/llm.env` |
| `hotline/cli.py` | `doctor` 增加 `local_llm` |
| `deploy/README.md`、`.env.example`、`docs/deployment.md` | 文档 |

---

### Task 1: `hotline/local_llm.py` 核心探测与 URL/环境辅助

**Files:**
- Create: `hotline/local_llm.py`
- Test: `tests/test_local_llm.py`
- Modify: none yet

**Interfaces:**
- Consumes: `httpx`, `hotline.settings.Settings`（可选）
- Produces:
  - `LOCAL_PRESET_URLS: frozenset[str]`
  - `is_local_preset_url(url: str) -> bool`
  - `normalize_base_url(url: str) -> str`（去尾斜杠）
  - `list_model_ids(payload: dict | list) -> list[str]`
  - `model_present(model_ids: list[str], model: str) -> bool`
  - `probe_local_llm(*, base_url: str, model: str, api_key: str = "", timeout: float = 60.0, trust_env: bool = False, client: httpx.Client | None = None) -> dict`
  - `apply_env_updates(content: str, updates: dict[str, str]) -> str`
  - `should_update_timeout(current: str | None) -> bool`（`None` 或 `"90"` → True）
  - 报告 dict 键：`status`（`ok`|`未配置`|`unreachable`|`model_missing`|`json_unsupported`|`error`）、`reachable`、`model_present`、`json_object_ok`、`latency_ms`、`detail`（无密钥）

- [ ] **Step 1: Write the failing test**

创建 `tests/test_local_llm.py`：

```python
import json

import httpx
import pytest

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
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
& ".venv\Scripts\python.exe" -m pytest tests/test_local_llm.py -v
```

Expected: FAIL（`ModuleNotFoundError: hotline.local_llm` 或 import 错误）

- [ ] **Step 3: Write implementation**

创建 `hotline/local_llm.py`（完整实现，无省略）：

```python
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
        except httpx.HTTPError:
            report["status"] = "json_unsupported"
            report["detail"] = "JSON chat 请求失败"
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
```

- [ ] **Step 4: Run test to verify it passes**

```powershell
& ".venv\Scripts\python.exe" -m pytest tests/test_local_llm.py -v
```

Expected: 全部 PASS

- [ ] **Step 5: Commit（仅用户明确要求时）**

```powershell
git add hotline/local_llm.py tests/test_local_llm.py
git commit -m "feat: add local LLM probe helpers"
```

未授权则跳过。

---

### Task 2: Compose、`llm.env` 模板与 `prepare_local_llm.py`

**Files:**
- Create: `deploy/compose.llm.yml`
- Create: `deploy/llm.env.example`
- Create: `scripts/prepare_local_llm.py`
- Modify: `.gitignore`
- Test: `tests/test_prepare_local_llm.py`

**Interfaces:**
- Consumes: 无运行时 hotline 依赖（可只读常量）
- Produces: `deploy/llm.env` 内容；CLI：
  - `python scripts/prepare_local_llm.py --engine ollama|vllm [--tier cpu|gpu-entry|gpu-strong]`
  - 已存在 `deploy/llm.env` → exit 1，不覆盖

**默认映射（写入 llm.env）：**

| `--engine` | `--tier`（默认） | `LLM_MODEL` | `LLM_HOST_PORT` |
| --- | --- | --- | --- |
| `ollama` | `cpu`（默认） | `qwen2.5:7b-instruct` | `11434` |
| `ollama` | `gpu-entry` | `qwen2.5:14b-instruct` | `11434` |
| `vllm` | `gpu-strong`（vllm 默认） | `Qwen/Qwen2.5-14B-Instruct` | `8000` |

钉镜像（写入 example 与生成文件；若拉取失败由操作者改 tag，不在脚本内静默换源）：

- `OLLAMA_IMAGE=ollama/ollama:0.6.8`
- `VLLM_IMAGE=vllm/vllm-openai:v0.8.5`

- [ ] **Step 1: Write failing tests for prepare**

`tests/test_prepare_local_llm.py`：

```python
from pathlib import Path

import pytest

from scripts.prepare_local_llm import build_llm_env, main


def test_build_llm_env_ollama_cpu():
    text = build_llm_env(engine="ollama", tier="cpu")
    assert "LLM_ENGINE=ollama" in text
    assert "LLM_MODEL=qwen2.5:7b-instruct" in text
    assert "LLM_HOST_PORT=11434" in text
    assert "OLLAMA_IMAGE=ollama/ollama:0.6.8" in text


def test_build_llm_env_vllm():
    text = build_llm_env(engine="vllm", tier="gpu-strong")
    assert "LLM_ENGINE=vllm" in text
    assert "LLM_MODEL=Qwen/Qwen2.5-14B-Instruct" in text
    assert "LLM_HOST_PORT=8000" in text
    assert "VLLM_IMAGE=vllm/vllm-openai:v0.8.5" in text


def test_main_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "llm.env").write_text("LLM_ENGINE=ollama\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["--engine", "ollama"])
```

注意：若 `scripts` 不是包，将 `build_llm_env`/`main` 放在 `scripts/prepare_local_llm.py`，测试用 importlib 加载：

```python
import importlib.util
from pathlib import Path

def load():
    path = Path("scripts/prepare_local_llm.py")
    spec = importlib.util.spec_from_file_location("prepare_local_llm", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
```

计划要求实现时采用 **importlib 加载脚本** 的测试方式，避免改 `sys.path` 包结构。

- [ ] **Step 2: Run tests — expect fail**

```powershell
& ".venv\Scripts\python.exe" -m pytest tests/test_prepare_local_llm.py -v
```

- [ ] **Step 3: Implement compose + example + prepare + gitignore**

`.gitignore` 追加一行：`deploy/llm.env`

`deploy/llm.env.example`：

```dotenv
# 复制或由 scripts/prepare_local_llm.py 生成；勿提交真实密钥。
LLM_ENGINE=ollama
OLLAMA_IMAGE=ollama/ollama:0.6.8
VLLM_IMAGE=vllm/vllm-openai:v0.8.5
LLM_MODEL=qwen2.5:7b-instruct
LLM_HOST_PORT=11434
LLM_API_KEY=
VLLM_GPU_MEMORY_UTILIZATION=0.9
VLLM_MAX_MODEL_LEN=16384
OLLAMA_KEEP_ALIVE=24h
```

`deploy/compose.llm.yml`：

```yaml
# 独立项目：docker compose --project-name hotline-llm --env-file deploy/llm.env -f deploy/compose.llm.yml --profile ollama|vllm up -d
services:
  ollama:
    profiles: ["ollama"]
    image: ${OLLAMA_IMAGE:?请设置 OLLAMA_IMAGE}
    container_name: hotline-llm-ollama
    ports:
      - "127.0.0.1:${LLM_HOST_PORT:-11434}:11434"
    environment:
      OLLAMA_KEEP_ALIVE: ${OLLAMA_KEEP_ALIVE:-24h}
    volumes:
      - hotline_ollama:/root/.ollama
    restart: unless-stopped

  vllm:
    profiles: ["vllm"]
    image: ${VLLM_IMAGE:?请设置 VLLM_IMAGE}
    container_name: hotline-llm-vllm
    ports:
      - "127.0.0.1:${LLM_HOST_PORT:-8000}:8000"
    environment:
      HUGGING_FACE_HUB_TOKEN: ${HUGGING_FACE_HUB_TOKEN:-}
    ipc: host
    gpus: all
    command:
      - --model
      - ${LLM_MODEL:?请设置 LLM_MODEL}
      - --host
      - "0.0.0.0"
      - --port
      - "8000"
      - --max-model-len
      - ${VLLM_MAX_MODEL_LEN:-16384}
      - --gpu-memory-utilization
      - ${VLLM_GPU_MEMORY_UTILIZATION:-0.9}
    volumes:
      - hotline_vllm_cache:/root/.cache/huggingface
    restart: unless-stopped

volumes:
  hotline_ollama:
  hotline_vllm_cache:
```

`scripts/prepare_local_llm.py`：实现 `build_llm_env`、`main(argv: list[str] | None = None)`；`argparse` 校验 `vllm` 不得与 `cpu` tier 组合（若组合则 exit 1 并中文说明）；已存在文件则 `SystemExit("已有 deploy/llm.env，未覆盖。")`。

- [ ] **Step 4: Run tests — expect pass**

```powershell
& ".venv\Scripts\python.exe" -m pytest tests/test_prepare_local_llm.py tests/test_local_llm.py -v
```

- [ ] **Step 5: Commit（仅用户授权时）**

```powershell
git add .gitignore deploy/compose.llm.yml deploy/llm.env.example scripts/prepare_local_llm.py tests/test_prepare_local_llm.py
git commit -m "feat: add local LLM compose profiles and prepare script"
```

---

### Task 3: `check_local_llm.py` 与 `doctor` 汇总

**Files:**
- Create: `scripts/check_local_llm.py`
- Modify: `hotline/cli.py`（`doctor` 分支）
- Test: `tests/test_local_llm_check_doctor.py`

**Interfaces:**
- Consumes: `hotline.local_llm.probe_local_llm`、`Settings`
- Produces:
  - `check_local_llm.main` → stdout JSON 报告；`status!="ok"` 时 exit code 1；`未配置` 时 exit code 2
  - `doctor` 报告增加 `"local_llm": { ...摘要... }`（可调用 `probe_local_llm`；未配置时 status=`未配置`）

- [ ] **Step 1: Write failing tests**

```python
import json
from unittest.mock import patch

import httpx

from hotline.local_llm import probe_local_llm


def test_doctor_includes_local_llm_when_unconfigured(settings, capsys):
    settings.llm_base_url = ""
    settings.llm_model = ""
    from hotline import cli

    # 通过抽取 doctor 逻辑为可测函数更干净：实现时在 cli.py 增加
    # build_doctor_report(settings) -> dict
    from hotline.cli import build_doctor_report

    report = build_doctor_report(settings)
    assert report["local_llm"]["status"] == "未配置"


def test_check_script_exit_codes(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    path = Path("scripts/check_local_llm.py")
    spec = importlib.util.spec_from_file_location("check_local_llm", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    # 使用 monkeypatch 让 probe 走 mock：更直接测 probe 已在 Task1；此处测 main 对 status 映射
    monkeypatch.setattr(
        mod,
        "probe_local_llm",
        lambda **kwargs: {
            "status": "ok",
            "reachable": True,
            "model_present": True,
            "json_object_ok": True,
            "latency_ms": 1,
            "detail": "ok",
        },
    )
    assert mod.main_from_values("http://127.0.0.1:11434/v1", "m", "") == 0
    monkeypatch.setattr(mod, "probe_local_llm", lambda **kwargs: {"status": "未配置", "detail": "x"})
    assert mod.main_from_values("", "", "") == 2
    monkeypatch.setattr(mod, "probe_local_llm", lambda **kwargs: {"status": "unreachable", "detail": "x"})
    assert mod.main_from_values("http://127.0.0.1:11434/v1", "m", "") == 1
```

实现时 **`cli.py` 必须抽出 `build_doctor_report(settings) -> dict`**，`doctor` 子命令只负责打印；原有 neo4j/openspg 字段行为保持不变。

- [ ] **Step 2: Run tests — expect fail**

```powershell
& ".venv\Scripts\python.exe" -m pytest tests/test_local_llm_check_doctor.py -v
```

- [ ] **Step 3: Implement check script + refactor doctor**

`scripts/check_local_llm.py`：从 `.env`/`Settings` 读配置；暴露 `main_from_values(base_url, model, api_key) -> int`；打印 `json.dumps(report, ensure_ascii=False, indent=2)`。

`hotline/cli.py`：将现有 doctor 体迁入 `build_doctor_report`；在返回前：

```python
from hotline.local_llm import probe_local_llm

if not settings.llm_base_url or not settings.llm_model:
    report["local_llm"] = {"status": "未配置", "detail": "缺少模型 URL 或名称"}
else:
    report["local_llm"] = probe_local_llm(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key.get_secret_value(),
        timeout=min(settings.llm_timeout, 60.0),
        trust_env=settings.http_trust_env,
    )
```

- [ ] **Step 4: Run tests — expect pass**

```powershell
& ".venv\Scripts\python.exe" -m pytest tests/test_local_llm.py tests/test_prepare_local_llm.py tests/test_local_llm_check_doctor.py -v
```

另跑一次 doctor 冒烟（允许 neo4j 失败）：

```powershell
& ".venv\Scripts\python.exe" -m hotline.cli doctor
```

Expected: JSON 含 `local_llm` 键，无密钥字段。

- [ ] **Step 5: Commit（仅用户授权时）**

```powershell
git add scripts/check_local_llm.py hotline/cli.py tests/test_local_llm_check_doctor.py
git commit -m "feat: add local LLM check and doctor summary"
```

---

### Task 4: `configure_local_llm.py`（探测、可选 pull、写 `.env`）

**Files:**
- Create: `scripts/configure_local_llm.py`
- Test: `tests/test_configure_local_llm.py`

**Interfaces:**
- Consumes: `probe_local_llm`、`is_local_preset_url`、`apply_env_updates`、`should_update_timeout`、`dotenv_values`
- Produces: CLI
  - `python scripts/configure_local_llm.py [--force] [--skip-pull]`
  - 读取 `deploy/llm.env` 得 `LLM_ENGINE`、`LLM_MODEL`、`LLM_HOST_PORT`、`LLM_API_KEY`
  - 目标 URL：`http://127.0.0.1:{port}/v1`
  - Ollama 且 `model_missing` 且未 `--skip-pull`：执行  
    `docker compose --project-name hotline-llm --env-file deploy/llm.env -f deploy/compose.llm.yml exec -T ollama ollama pull {model}`  
    然后再 probe；仍失败则不改 `.env`
  - vLLM：不 pull，只 probe；可对 `unreachable`/`model_missing` 有限重试 3 次、间隔 10s（实现为函数参数便于测试加速）
  - 成功：备份 `.env`，写入 backend/url/model/api_key/`EXTRA_BODY={}`；按规则更新 timeout→300

- [ ] **Step 1: Write failing tests**

```python
from pathlib import Path

import pytest


def load_configure():
    import importlib.util
    path = Path("scripts/configure_local_llm.py")
    spec = importlib.util.spec_from_file_location("configure_local_llm", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_refuses_non_preset_without_force(tmp_path, monkeypatch):
    mod = load_configure()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "llm.env").write_text(
        "LLM_ENGINE=ollama\nLLM_MODEL=qwen2.5:7b-instruct\nLLM_HOST_PORT=11434\nLLM_API_KEY=\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "HOTLINE_LLM_BASE_URL=https://api.deepseek.com\nHOTLINE_LLM_MODEL=deepseek-flash\nHOTLINE_LLM_TIMEOUT=90\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="未覆盖"):
        mod.configure(force=False, skip_pull=True, probe=lambda **k: {"status": "ok"}, pull=lambda m: None)


def test_writes_env_after_ok(tmp_path, monkeypatch):
    mod = load_configure()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "deploy").mkdir()
    (tmp_path / "data" / "config-backups").mkdir(parents=True)
    (tmp_path / "deploy" / "llm.env").write_text(
        "LLM_ENGINE=ollama\nLLM_MODEL=qwen2.5:7b-instruct\nLLM_HOST_PORT=11434\nLLM_API_KEY=\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "HOTLINE_LLM_BASE_URL=http://127.0.0.1:11434/v1\nHOTLINE_LLM_MODEL=old\nHOTLINE_LLM_TIMEOUT=90\n",
        encoding="utf-8",
    )
    mod.configure(
        force=False,
        skip_pull=True,
        probe=lambda **k: {
            "status": "ok",
            "reachable": True,
            "model_present": True,
            "json_object_ok": True,
            "detail": "ok",
        },
        pull=lambda m: None,
    )
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "HOTLINE_LLM_BACKEND=openai" in text
    assert "HOTLINE_LLM_MODEL=qwen2.5:7b-instruct" in text
    assert "HOTLINE_LLM_TIMEOUT=300" in text
    assert "HOTLINE_LLM_EXTRA_BODY={}" in text


def test_probe_failure_does_not_write(tmp_path, monkeypatch):
    mod = load_configure()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "llm.env").write_text(
        "LLM_ENGINE=ollama\nLLM_MODEL=qwen2.5:7b-instruct\nLLM_HOST_PORT=11434\nLLM_API_KEY=\n",
        encoding="utf-8",
    )
    original = "HOTLINE_LLM_BASE_URL=http://127.0.0.1:11434/v1\nHOTLINE_LLM_MODEL=keep\n"
    (tmp_path / ".env").write_text(original, encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.configure(
            force=False,
            skip_pull=True,
            probe=lambda **k: {"status": "json_unsupported", "detail": "no"},
            pull=lambda m: None,
        )
    assert (tmp_path / ".env").read_text(encoding="utf-8") == original
```

将核心逻辑做成可注入 `probe`/`pull` 的 `configure(...)` 函数，便于单测。

- [ ] **Step 2: Run tests — expect fail**

```powershell
& ".venv\Scripts\python.exe" -m pytest tests/test_configure_local_llm.py -v
```

- [ ] **Step 3: Implement configure script**

要求：

1. 缺少 `deploy/llm.env` 或 `.env` → 中文 `SystemExit`。
2. 覆盖保护用 `is_local_preset_url`。
3. `docker compose … exec` 失败时不写 `.env`，detail 不含令牌。
4. 成功打印简短 JSON：`{"configured": true, "base_url": "...", "model": "..."}`，无 key。

- [ ] **Step 4: Run all related tests**

```powershell
& ".venv\Scripts\python.exe" -m pytest tests/test_local_llm.py tests/test_prepare_local_llm.py tests/test_local_llm_check_doctor.py tests/test_configure_local_llm.py -v
```

Expected: 全部 PASS

- [ ] **Step 5: Commit（仅用户授权时）**

```powershell
git add scripts/configure_local_llm.py tests/test_configure_local_llm.py
git commit -m "feat: configure app env for local LLM after health probe"
```

---

### Task 5: 文档与示例环境注释

**Files:**
- Modify: `deploy/README.md`
- Modify: `.env.example`
- Modify: `docs/deployment.md`
- Modify: `docs/superpowers/specs/2026-11-11-local-llm-deploy-design.md`（状态改为「已批准，实现中/已实现」在本任务结束时改为「已实现」）
- Test: 无自动化；人工核对链接与命令可复制

- [ ] **Step 1: Update `deploy/README.md`**

在「模型服务」相关位置新增整节 **「本地大模型（Ollama / vLLM）」**，必须包含：

1. 生成配置：`python scripts/prepare_local_llm.py --engine ollama --tier cpu`
2. 启动 Ollama：

```powershell
docker compose --project-name hotline-llm --env-file "deploy/llm.env" -f "deploy/compose.llm.yml" --profile ollama up -d
```

3. 启动 vLLM（需 NVIDIA + 驱动）：

```powershell
docker compose --project-name hotline-llm --env-file "deploy/llm.env" -f "deploy/compose.llm.yml" --profile vllm up -d
```

4. `python scripts/configure_local_llm.py`（从 DeepSeek 切换加 `--force`）
5. `python scripts/check_local_llm.py` 与 `python -m hotline.cli doctor`
6. 能力边界：健康检查 ≠ 抽取质量；CPU 建议 `HOTLINE_LLM_TIMEOUT=300`
7. 与云 API 切换说明

- [ ] **Step 2: Update `.env.example`**

在 LLM 段增加注释示例（非默认启用）：

```dotenv
# 本地 Ollama 示例：HOTLINE_LLM_BACKEND=openai
# HOTLINE_LLM_BASE_URL=http://127.0.0.1:11434/v1
# HOTLINE_LLM_MODEL=qwen2.5:7b-instruct
# 本地 vLLM 示例：HOTLINE_LLM_BASE_URL=http://127.0.0.1:8000/v1
```

- [ ] **Step 3: Update `docs/deployment.md`**

文首或模型节增加指向 `deploy/README.md` 本地 LLM 节；写明「提供健康检查；真实本地抽取闭环非本轮必验」。

- [ ] **Step 4: Manual doc checklist**

对照 spec 第 7、9 节：命令、端口、`--force`、非目标均已写明。

- [ ] **Step 5: Commit（仅用户授权时）**

```powershell
git add deploy/README.md .env.example docs/deployment.md docs/superpowers/specs/2026-11-11-local-llm-deploy-design.md
git commit -m "docs: document local Ollama and vLLM deployment"
```

---

### Task 6: 回归与收尾验证

**Files:** none new

- [ ] **Step 1: Full unit suite for touched area**

```powershell
& ".venv\Scripts\python.exe" -m pytest tests/test_local_llm.py tests/test_prepare_local_llm.py tests/test_local_llm_check_doctor.py tests/test_configure_local_llm.py tests/test_model_contract.py -v
```

Expected: PASS

- [ ] **Step 2: Ruff on new/changed Python**

```powershell
& ".venv\Scripts\python.exe" -m ruff check hotline/local_llm.py hotline/cli.py scripts/prepare_local_llm.py scripts/check_local_llm.py scripts/configure_local_llm.py tests/test_local_llm.py tests/test_prepare_local_llm.py tests/test_local_llm_check_doctor.py tests/test_configure_local_llm.py
```

Expected: 无错误

- [ ] **Step 3: Spec coverage checklist（执行者勾选）**

| Spec 要求 | Task |
| --- | --- |
| compose.llm.yml + profiles | 2 |
| llm.env.example / prepare | 2 |
| check + shared probe | 1, 3 |
| configure + --force + backup | 4 |
| doctor local_llm | 3 |
| Ollama docker exec pull | 4 |
| 文档 | 5 |
| 不改抽取主链路 | 全程未改 extraction 协议 |
| 无真实模型 CI | 测试均为 mock |

- [ ] **Step 4: 向用户汇报**

列出新增文件、如何启动 Ollama/vLLM、如何从 DeepSeek `--force` 切换；说明未跑真实 GPU/模型下载除非用户授权。

---

## Self-Review (plan author)

1. **Spec coverage:** 独立 compose、双引擎、CPU/GPU 默认模型、健康检查四步、configure 覆盖保护与 timeout 规则、doctor、文档、非目标 —— 均有对应 Task；真实闭环 C 明确排除。
2. **Placeholders:** 无 TBD；镜像 tag 已钉具体版本（操作者可改 `llm.env`）。
3. **Type consistency:** `probe_local_llm` 报告键与 doctor/check/configure 共用；`build_doctor_report` / `configure(...)` / `main_from_values` 在各 Task Interfaces 对齐。
