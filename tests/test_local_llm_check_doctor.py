import importlib.util
from pathlib import Path

import httpx


def test_doctor_includes_local_llm_when_unconfigured(settings, capsys):
    settings.llm_base_url = ""
    settings.llm_model = ""
    from hotline.cli import build_doctor_report

    report = build_doctor_report(settings)
    assert report["local_llm"]["status"] == "未配置"


def test_check_script_exit_codes(tmp_path, monkeypatch, capsys):
    path = Path("scripts/check_local_llm.py")
    spec = importlib.util.spec_from_file_location("check_local_llm", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "m"}]})

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
