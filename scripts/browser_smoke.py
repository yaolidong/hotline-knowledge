"""真实本地 HTTP/浏览器冒烟：无模型时必须失败并保留原文，不注入模拟图谱。"""

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
output = ROOT / "test-results" / "browser"
output.mkdir(parents=True, exist_ok=True)
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
token = secrets.token_urlsafe(32)
agent_token = secrets.token_urlsafe(32)
environment = {
    **os.environ,
    "HOTLINE_DATA_DIR": str(output / ("data-" + secrets.token_hex(4))),
    "HOTLINE_USERS": json.dumps(
        {
            token: {"name": "浏览器测试管理员", "role": "admin"},
            agent_token: {"name": "浏览器测试客服", "role": "agent"},
        }
    ),
    "HOTLINE_LLM_BASE_URL": "",
    "HOTLINE_LLM_MODEL": "",
    "HOTLINE_NEO4J_PASSWORD": "",
    "HOTLINE_WORKER_ENABLED": "true",
}
log = (output / "server.log").open("w", encoding="utf-8")
process = subprocess.Popen(
    [sys.executable, "-m", "hotline.cli", "serve", "--port", str(port)],
    cwd=ROOT,
    env=environment,
    stdout=log,
    stderr=log,
    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
)
base = f"http://127.0.0.1:{port}"
try:
    for _ in range(100):
        try:
            if httpx.get(base + "/health", timeout=1, trust_env=False).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    else:
        raise RuntimeError("测试服务未能启动")
    with sync_playwright() as playwright:
        executable = os.getenv(
            "HOTLINE_BROWSER_PATH", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
        )
        launch_options = {"executable_path": executable} if Path(executable).exists() else {}
        browser = playwright.chromium.launch(headless=True, **launch_options)
        page = browser.new_page(viewport={"width": 1440, "height": 1080})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base)
        page.locator("#token").fill(token)
        page.locator("#login-form button").click()
        page.locator("#workspace").wait_for(state="visible")
        page.locator("#knowledge-space").select_option("test")
        page.get_by_role("button", name="数据导入", exact=True).click()
        page.locator('#import-form [name="title"]').fill("浏览器合成测试指南")
        page.locator('#import-form [name="region"]').fill("测试甲区")
        page.locator('#import-form [name="synthetic"]').check()
        page.locator("#import-text-mode").click()
        page.locator('#import-form [name="text"]').fill(
            "合成测试资料，非真实政策。\n第一条 示例登记由测试服务局办理。"
        )
        page.locator('#import-form button[type="submit"]').click()
        page.locator("#import-result").get_by_text("已提交", exact=False).wait_for()
        page.get_by_role("button", name="抽取结果", exact=True).click()
        page.locator("#results-list").get_by_text("失败", exact=False).wait_for(timeout=15000)
        page.locator("#results-list").get_by_role("button", name="查看").first.click()
        page.locator("#extraction-detail .record-card > p.error").filter(has_text="模型服务尚未配置").wait_for()
        page.get_by_role("button", name="重试失败块", exact=True).wait_for()
        page.screenshot(path=str(output / "import-status.png"), full_page=True)
        page.get_by_role("button", name="知识图谱", exact=True).click()
        page.get_by_role("button", name="检索图谱", exact=True).click()
        page.locator("#notification").get_by_text("图数据库尚未配置", exact=False).wait_for()
        assert page.locator(".graph-node").count() == 0
        page.get_by_role("button", name="审核与维护", exact=True).click()
        page.locator('#review-filter [name="state"]').select_option("")
        page.locator("#review-list").get_by_role("button", name="对照审核").first.click()
        assert page.locator("#review-detail").get_by_role("button", name="发布到图谱").count() == 0
        page.locator("#logout").click()
        page.locator("#token").fill(agent_token)
        page.locator("#login-form button").click()
        page.locator("#workspace").wait_for(state="visible")
        page.locator("#knowledge-space").select_option("test")
        assert not page.get_by_role("button", name="数据导入", exact=True).is_visible()
        page.locator('#query-form [name="question"]').fill("示例登记需要什么材料？")
        page.locator('#query-form button[type="submit"]').click()
        page.locator("#answer").get_by_text("没有检索到可用于回答的已发布知识。", exact=True).wait_for()
        page.screenshot(path=str(output / "query-empty.png"), full_page=True)
        assert not errors, errors
        browser.close()
        print(
            "浏览器冒烟通过：角色隔离、文本导入、任务失败原文保留、图服务缺失提示、未发布知识拒答；无页面脚本异常。"
        )
finally:
    process.terminate()
    process.wait(timeout=10)
    log.close()
