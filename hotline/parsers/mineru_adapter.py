"""MinerU 可选解析适配器：未安装时明确失败，不静默回退伪造结果。"""

from __future__ import annotations

from pathlib import Path

from hotline.settings import ServiceError


class MinerUAdapter:
    """试点接口。默认不启用；对比脚本显式调用。"""

    name = "mineru"

    def __init__(self):
        try:
            import magic_pdf  # noqa: F401
        except ImportError as exc:
            raise ServiceError(
                "未安装 MinerU（magic_pdf）。复杂文档试点请先按官方文档安装后再对比，默认管道不受影响。",
                503,
            ) from exc

    def parse_pdf(self, path: Path) -> dict:
        raise ServiceError(
            "MinerU 适配器已检测到依赖，但本仓库尚未绑定具体版本化调用；请在对比实验中补齐版本锁定后再接入默认管道。",
            501,
        )


def available() -> bool:
    try:
        import magic_pdf  # noqa: F401

        return True
    except ImportError:
        return False
