"""只读验证真实导入结果、表格阅读和失败块界面，不注入图谱或模型响应。"""

import argparse
import json
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

from hotline.settings import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("revision_ids", nargs="+")
    args = parser.parse_args()
    settings = Settings()
    token = next(k for k, user in settings.users.items() if user.role == "admin")
    output = Path("test-results/extraction-recovery")
    output.mkdir(exist_ok=True)
    report, errors = [], []
    with (
        httpx.Client(
            base_url="http://127.0.0.1:8080", trust_env=False, headers={"Authorization": "Bearer " + token}
        ) as client,
        sync_playwright() as pw,
    ):
        browser = pw.chromium.launch(
            headless=True, executable_path="C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:8080")
        page.locator("#token").fill(token)
        page.locator("#login-form button").click()
        page.locator("#workspace").wait_for(state="visible")
        for rid in args.revision_ids:
            response = client.get(f"/api/revisions/{rid}")
            response.raise_for_status()
            record = response.json()
            await_load = "async rid => {await tab('results'); await loadRevision(rid, 'results');}"
            page.evaluate(await_load, rid)
            page.locator("#extraction-detail .record-stats").get_by_text(
                f"{len((record['extraction'] or {}).get('entities', []))} 个实体", exact=True
            ).wait_for()
            page.get_by_role("button", name="识别 / 解析文字", exact=True).click()
            rendered_cells = page.locator("#extraction-detail .source-chunk td").count()
            expected_cells = sum(
                len(p.get("cells", []))
                for p in (record["parsed_metadata"] or {}).get("_source", {}).get("pages", [])
            )
            assert rendered_cells == expected_cells, (rid, rendered_cells, expected_cells)
            outcomes = (record["parsed_metadata"] or {}).get("_extraction", {})
            if record["task_status"] in ("PARTIAL", "FAILED") and outcomes:
                page.get_by_role("button", name="重试失败块", exact=True).wait_for()
                page.locator("#extraction-detail summary").filter(has_text="分块处理").click()
                assert page.locator("#extraction-detail p.error.small").count() == sum(
                    p["status"] == "FAILED" for p in outcomes.values()
                )
                page.evaluate("async rid => {await tab('review'); await loadRevision(rid, 'review');}", rid)
                assert page.get_by_role("button", name="审核通过", exact=True).count() == 0
            page.screenshot(path=str(output / f"{rid}.png"), full_page=True)
            report.append(
                {"revision_id": rid, "status": record["task_status"], "rendered_cells": rendered_cells}
            )
        browser.close()
    assert not errors, errors
    (output / "browser-report.json").write_text(
        json.dumps({"items": report, "page_errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report))


if __name__ == "__main__":
    main()
