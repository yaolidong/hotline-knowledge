"""真实 HTTP/浏览器交互；采用合成标注和内存图契约替身，不代表模型质量。"""

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from conftest import TOKENS
from test_fact_review import seed


def test_fact_review_browser_manual_return_correct_and_publish(application, tmp_path):
    from playwright.sync_api import sync_playwright

    executable = os.getenv(
        "HOTLINE_BROWSER_PATH", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
    )
    if not Path(executable).exists():
        pytest.skip("未发现本地浏览器")
    rid = seed(application, partial=True)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(application, log_level="error"))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=executable)
            page = browser.new_page(viewport={"width": 1440, "height": 1080})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{sock.getsockname()[1]}")
            page.locator("#token").fill(TOKENS["admin"])
            page.locator("#login-form button").click()
            page.locator("#workspace").wait_for(state="visible")
            page.evaluate("async rid => {await tab('review'); await loadRevision(rid,'review');}", rid)

            # 失败块可通过表单补录，原成功条目不丢失。
            page.get_by_text("待补录 · 1 块", exact=True).click()
            page.get_by_role("button", name="补录此块", exact=True).click()
            editor = page.locator(".fact-editor-host")
            editor.get_by_label("补录依据", exact=True).fill("对照合成第三条补录事项与材料")
            editor.get_by_role("button", name="新增实体", exact=True).click()
            editor.get_by_label("原文名称", exact=True).fill("丙登记")
            editor.get_by_role("button", name="新增实体", exact=True).click()
            editor.get_by_label("实体类型", exact=True).select_option("RequiredMaterial")
            editor.get_by_label("原文名称", exact=True).fill("丙申请表")
            editor.get_by_role("button", name="关系（0）", exact=True).click()
            editor.get_by_role("button", name="新增关系", exact=True).click()
            editor.get_by_label("关系类型", exact=True).select_option("REQUIRES_MATERIAL")
            editor.get_by_label("源实体", exact=True).select_option(label="丙登记")
            editor.get_by_label("目标实体", exact=True).select_option(label="丙申请表")
            page.wait_for_function("() => current.id && current.task_status === 'PARTIAL'")
            editor.get_by_role("button", name="保存表单修正并重新待审", exact=True).click()
            page.wait_for_function("() => current.task_status === 'READY'")
            assert len(application.state.store.get_revision(rid)["extraction"]["entities"]) == 6
            assert len(application.state.store.get_revision(rid)["extraction"]["relations"]) == 3

            group = page.locator(".fact-group").filter(has=page.locator("summary", has_text="甲登记")).first
            group.locator(":scope > summary").click()
            row = (
                group.locator(".fact-row")
                .filter(has=page.locator("summary", has_text="实体 · 同时满足"))
                .first
            )
            row.locator(":scope > summary").click()
            row.get_by_label("处理依据", exact=True).fill("需核对否定条件")
            row.get_by_role("button", name="退回修正", exact=True).click()
            page.get_by_text("已退回；相关事实组暂不能发布。", exact=True).wait_for()
            row.get_by_role("button", name="修正此条", exact=True).click()
            editor.get_by_label("修改依据", exact=True).fill("完整保留条件及排除原文")
            editor.get_by_label("内容", exact=True).fill("同时满足年满十八周岁和居住甲区，并且不属于失信情形")
            editor.get_by_role("button", name="保存表单修正并重新待审", exact=True).click()
            page.get_by_text("修正已保存，事实组需重新审核。", exact=True).wait_for()

            # 未完成覆盖核对不能批准；填写后发布按钮可操作。
            group.get_by_label("事实组审核依据", exact=True).fill("已核对合成甲登记完整条件与例外")
            group.get_by_role("button", name="审核通过此组", exact=True).click()
            page.get_by_text("请完成事实组完整性核对。", exact=True).wait_for()
            for checkbox in group.locator("[data-group-check]").all():
                checkbox.check()
            group.locator(".coverage-review > summary").click()
            for select in group.locator("select[data-coverage-chunk]").all():
                select.evaluate("node => node.closest('details').open = true")
                values = select.locator("option").evaluate_all("nodes => nodes.map(n=>n.value)")
                select.select_option(values[1])
                if values[1] == "unrelated":
                    cid = select.get_attribute("data-coverage-chunk")
                    group.get_by_label(f"排除依据 {cid}", exact=True).fill("属于另一个合成事项，无交叉要求")
            group.get_by_role("button", name="审核通过此组", exact=True).click()
            group.get_by_role("button", name="发布此组", exact=True).wait_for()
            group.get_by_role("button", name="发布此组", exact=True).click()
            page.wait_for_function("() => active === 'graph' && graphSnapshot?.nodes.length > 0")
            assert len(application.state.store.visible_ids()) == 1
            assert not errors, errors
            page.screenshot(path=str(tmp_path / "fact-group.png"), full_page=True)
            browser.close()
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        sock.close()
