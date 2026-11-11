# 政务热线知识构建与辅助查询

基于锁定版本 OpenSPG/KAG 的独立扩展：FastAPI 应用 + Web 工作台，完成政策资料抽取、人工审核、写图发布与客服辅助查询。没有模型或图服务时返回明确错误，不使用模拟答案。

**状态：** 推荐在 Linux/macOS 上按「宿主机 Python + 本机 Docker 依赖」部署；Windows+WSL 与应用容器为可选拓扑（见 deploy 手册）。合成资料可走通抽取→审核→发布→查询，不得用于实际政策办理。完整人工标注金标准仍有限，禁止无复核报准确率。

---

## 仓库目录

| 路径 | 用途 |
| --- | --- |
| `hotline/` | 应用源码（API、工作流、图、检索、静态页、领域 Schema） |
| `deploy/` | 部署专用：Compose 覆盖、Dockerfile、镜像/嵌入配置、**部署手册** |
| `docs/` | 架构、API、验证与评测等常驻说明（非逐步安装） |
| `scripts/` | 上游拉取、部署辅助、评测与验收脚本（分组见 [scripts/README.md](scripts/README.md)） |
| `examples/` | 合成样例与金标准包（`examples/gold/`） |
| `tests/` | 离线 pytest（真实服务测需显式环境变量） |
| `vendor/` | Git 忽略；由 `upstream.lock.json` 拉取的 KAG / OpenSPG |
| `data/` | Git 忽略；运行时数据、备份与本地配置产物 |
| `.env` / `deploy/images.env` | Git 忽略；密钥与令牌，禁止入库或截图 |

开发产物（可忽略）：`.venv/`、`.pytest_cache/`、`test-results/`、`.superpowers/`、`.spec-workflow/`。

---

## 从零搭建（入口）

完整命令与验收以 **[deploy/README.md](deploy/README.md)** 为准。先选拓扑：**A** Linux/macOS（推荐）· **B** 应用容器 · **C** Windows+WSL。在**自己克隆的目录**操作。

```bash
git clone <repo-url> hotline-knowledge
cd hotline-knowledge
# 拓扑 A 概要（依赖容器与模型须按 deploy 手册就绪）
python3 scripts/fetch_upstream.py
uv venv --python 3.10 .venv
export PY="$(pwd)/.venv/bin/python"
uv pip install --python "$PY" -r requirements.lock -e . -e vendor/KAG
"$PY" -m hotline.cli init
"$PY" -m hotline.cli doctor
"$PY" -m hotline.cli serve --port 8080
```

工作台：<http://127.0.0.1:8080>。登录令牌在本地 `.env` 的 `HOTLINE_USERS`（键 = Bearer token）。

---

## 能力摘要

- 资料：PDF（文字/扫描）、图片、DOCX、TXT；本地 RapidOCR；版本化任务与审计  
- 抽取：领域 Schema 驱动；逐字证据；失败块重试/人工补录；事实组审核后发布  
- 图：OpenSPG `writerGraph` + Neo4j 回读；仅 `PUBLISHED` 进入客服查询  
- 查询：结构化意图 + 可选本地嵌入混合召回；证据 QA；正式/测试空间隔离  
- 运维：角色令牌、限流、健康检查、数据目录备份/隔离恢复脚本  

明确不做：自动资格裁定、音视频建图、完整 SSO/组织目录、分布式多 worker。

---

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [deploy/README.md](deploy/README.md) | **从零部署与运维（唯一安装主线）** |
| [docs/architecture.md](docs/architecture.md) | 架构与数据流 |
| [docs/api.md](docs/api.md) | API 与权限 |
| [docs/verification.md](docs/verification.md) | 验证边界与质量门禁 |
| [docs/gold-evaluation.md](docs/gold-evaluation.md) | 金标准评测 |
| [docs/business-workbench.md](docs/business-workbench.md) | 事项工作台 / 反馈 / 诊断 |
| [docs/ocr-and-workbench.md](docs/ocr-and-workbench.md) | OCR 与原件审阅 |
| [docs/deployment.md](docs/deployment.md) | 部署入口（跳转 deploy 手册） |
| [docs/archive/](docs/archive/) | 历史验收笔记（不参与安装主线） |

离线测试：

```powershell
uv pip install --python ".venv/Scripts/python.exe" -e ".[dev]" -e "vendor/KAG"
& ".venv/Scripts/python.exe" -m pytest -q
```

真实服务 / 金标准 live 需显式环境变量（见 verification 与 deploy 验收节），默认不会调用外网模型或写正式图。
