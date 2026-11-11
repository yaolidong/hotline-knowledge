import json
import subprocess
import sys
from pathlib import Path


def test_run_gold_smoke_script():
    result = subprocess.run(
        [sys.executable, "scripts/run_gold_smoke.py"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(result.stdout)
    assert payload["packs"] == 8
    assert payload["questions"] == 40
