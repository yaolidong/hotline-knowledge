"""下载哈希锁定的 Linux amd64/Python 3.10 嵌入服务依赖，供离线 Docker 构建。"""

import os
import subprocess
import sys
from pathlib import Path

target = Path("deploy/embedding-wheels")
target.mkdir(exist_ok=True)
environment = {
    key: value
    for key, value in os.environ.items()
    if key.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
}
subprocess.run(
    [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "pip",
        "download",
        "--timeout",
        "120",
        "--retries",
        "4",
        "--no-deps",
        "--require-hashes",
        "--only-binary=:all:",
        "--platform",
        "manylinux_2_28_x86_64",
        "--platform",
        "manylinux2014_x86_64",
        "--platform",
        "manylinux_2_17_x86_64",
        "--python-version",
        "310",
        "--implementation",
        "cp",
        "--abi",
        "cp310",
        "--dest",
        str(target),
        "-r",
        "deploy/embedding.lock",
    ],
    env=environment,
    check=True,
)
