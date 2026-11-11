"""独立本地 LLM 健康检查 CLI；不打印 API 密钥。"""

from __future__ import annotations

import json

from hotline.local_llm import probe_local_llm
from hotline.settings import Settings


def exit_code_for_status(status: str) -> int:
    if status == "ok":
        return 0
    if status == "未配置":
        return 2
    return 1


def main_from_values(
    base_url: str,
    model: str,
    api_key: str,
    *,
    timeout: float = 60.0,
    trust_env: bool = False,
) -> int:
    report = probe_local_llm(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout=timeout,
        trust_env=trust_env,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code_for_status(report["status"])


def main() -> None:
    settings = Settings()
    code = main_from_values(
        settings.llm_base_url,
        settings.llm_model,
        settings.llm_api_key.get_secret_value(),
        timeout=settings.llm_timeout,
        trust_env=settings.http_trust_env,
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
