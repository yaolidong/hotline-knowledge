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


def test_vllm_probe_retries_unreachable_then_ok_records_sleep():
    mod = load_configure()
    probe_results = iter(
        [
            {"status": "unreachable"},
            {"status": "unreachable"},
            {"status": "ok"},
        ]
    )
    sleeps: list[float] = []

    def probe(**k):
        return next(probe_results)

    def sleep_fn(interval: float) -> None:
        sleeps.append(interval)

    report = mod._probe_until_ok(
        engine="vllm",
        base_url="http://127.0.0.1:8000/v1",
        model="test-model",
        api_key="",
        skip_pull=True,
        probe=probe,
        pull=lambda m: None,
        sleep=sleep_fn,
        retry_count=2,
        retry_interval=10.0,
    )
    assert report["status"] == "ok"
    assert sleeps == [10.0, 10.0]


def test_windows_path_to_wsl():
    mod = load_configure()
    path = Path("C:/Users/test/datafusion/deploy/llm.env")
    assert mod.windows_path_to_wsl(path) == "/mnt/c/Users/test/datafusion/deploy/llm.env"


def test_build_ollama_pull_argv_uses_wsl_paths(tmp_path, monkeypatch):
    mod = load_configure()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "llm.env").write_text("x", encoding="utf-8")
    (tmp_path / "deploy" / "compose.llm.yml").write_text("x", encoding="utf-8")
    monkeypatch.setattr(mod, "resolve_docker_invocation", lambda: (["wsl", "-u", "root", "-e", "docker"], True))
    argv = mod.build_ollama_pull_argv(
        "qwen2.5:7b-instruct",
        use_wsl_paths=True,
        profile="ollama",
        service="ollama",
    )
    assert argv[0] == "wsl"
    env_idx = argv.index("--env-file")
    assert argv[env_idx + 1].startswith("/mnt/")
    f_idx = argv.index("-f")
    assert argv[f_idx + 1].startswith("/mnt/")
    prof_idx = argv.index("--profile")
    assert argv[prof_idx + 1] == "ollama"
    exec_idx = argv.index("exec")
    assert argv[exec_idx + 2] == "ollama"
    assert argv[-1] == "qwen2.5:7b-instruct"


def test_resolve_ollama_pull_target_gpu_profile():
    mod = load_configure()
    profile, service = mod.resolve_ollama_pull_target(
        {"LLM_COMPOSE_PROFILE": "ollama-gpu", "LLM_ENGINE": "ollama"}
    )
    assert profile == "ollama-gpu"
    assert service == "ollama-gpu"


def test_build_ollama_pull_argv_gpu_service():
    mod = load_configure()
    argv = mod.build_ollama_pull_argv(
        "qwen2.5:14b-instruct",
        use_wsl_paths=False,
        profile="ollama-gpu",
        service="ollama-gpu",
    )
    assert "--profile" in argv
    assert argv[argv.index("--profile") + 1] == "ollama-gpu"
    assert argv[argv.index("exec") + 2] == "ollama-gpu"


def test_default_pull_raises_when_docker_missing(monkeypatch):
    mod = load_configure()
    monkeypatch.setattr(mod.shutil, "which", lambda _: None)
    with pytest.raises(SystemExit, match="未找到 docker"):
        mod.default_pull("qwen2.5:7b-instruct")


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
