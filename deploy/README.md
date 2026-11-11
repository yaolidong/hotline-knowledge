# 从零部署手册

本文是**唯一安装主线**。在任意克隆目录下，按所选拓扑搭起：Python 应用、OpenSPG、Neo4j、MySQL、MinIO、本地嵌入，并完成项目 / Schema / 健康检查。

相关：[项目 README](../README.md) · [架构](../docs/architecture.md) · [API](../docs/api.md) · [验证边界](../docs/verification.md)

**不要：** 多进程启动应用、清正式图库、把 `.env` / `deploy/images.env` 提交或贴进聊天、把合成资料当现行政策、把文档里的示例路径当成你的机器路径。

---

## 0. 选拓扑（先读）

| 拓扑 | 适用环境 | 应用怎么跑 | 依赖怎么跑 | 验证深度 |
| --- | --- | --- | --- | --- |
| **A（推荐，通用）** | Linux 或 macOS；Docker 在本机 PATH | 宿主机 Python 3.10 + `.venv` | 本机 `docker compose` | 与业务联调一致；本仓库曾在 Linux 容器依赖上完整验收（含 Windows 宿主机变体，见附录 C） |
| **B（尽量全容器）** | 同上，希望少装 Python 依赖 | Compose 服务 `hotline` | 同 A | 镜像与联通已提供；深度弱于 A，上线前需自测 |
| **C（Windows + WSL）** | 仅 Windows，Docker 在 WSL 内 | Windows Python | `wsl … docker compose` | 本仓库开发机主验证路径之一 |

**macOS 说明：** 使用 Docker Desktop（或兼容引擎）即可走拓扑 A；Compose 须 **≥ 2.24.4**（覆盖文件使用 `!override`）。Apple Silicon 上 OpenSPG 官方镜像多为 `amd64`，可能需模拟运行，性能与兼容性需自行确认。

**共同前提：** Git、可访问阿里云 OpenSPG 镜像仓 `spg-registry.us-west-1.cr.aliyuncs.com`、可用大模型（DeepSeek 或自建 OpenAI-compatible）。主机建议预留足够内存（OpenSPG JVM 覆盖为 `-Xms2048m -Xmx8192m`，另加 Neo4j/MySQL/嵌入/OCR）。

| 组件 | 默认宿主机地址 |
| --- | --- |
| 应用 | `http://127.0.0.1:8080` |
| OpenSPG | `http://127.0.0.1:8887` |
| Neo4j Browser / Bolt | `http://127.0.0.1:7474` / `bolt://127.0.0.1:7687` |
| 嵌入 | `http://127.0.0.1:8090` |
| MySQL / MinIO | 不映射宿主机端口 |

数据流：上传/OCR → 模型抽取 → 人工审核 → OpenSPG 写图 → Neo4j 回读 → 仅 `PUBLISHED` 进入客服查询。Compose 项目名固定 **`hotline-dev`**。

下文**主文按拓扑 A（bash）书写**。拓扑 B / C 见文末附录；命令差异主要是 Python 解释器路径与是否加 `wsl` 前缀。

---

## 1. 克隆仓库

```bash
git clone <repo-url> hotline-knowledge
cd hotline-knowledge
```

之后所有命令均在该仓库根目录（含 `pyproject.toml`、`deploy/`、`scripts/`）执行。

约定解释器变量（拓扑 A）：

```bash
# Linux / macOS
python3 --version    # 需要 3.10.x
uv --version
docker --version
docker compose version   # >= 2.24.4

export PY="$(pwd)/.venv/bin/python"
# 建好 venv 之前可先用系统 python3 跑 fetch；建好后一律用 $PY
```

---

## 2. 拉取上游依赖

```bash
test -f pyproject.toml && test -f deploy/README.md
python3 scripts/fetch_upstream.py
test -f vendor/openspg/dev/release/docker-compose.yml
test -d vendor/KAG
```

`vendor/` 由 Git 忽略，版本由 `upstream.lock.json` 锁定；不要改上游核心代码。

---

## 3. 创建 Python 环境与 `.env`

```bash
uv venv --python 3.10 .venv
export PY="$(pwd)/.venv/bin/python"
uv pip install --python "$PY" -r requirements.lock -e . -e vendor/KAG
"$PY" -m hotline.cli init
```

- `init` 从 `.env.example` 生成 `.env` 与四角色随机令牌（`HOTLINE_USERS`）
- **已有 `.env` 时拒绝覆盖**
- 令牌只在本机查看，勿写入共享日志

---

## 4. 配置大模型

编辑仓库根目录 `.env`（密钥只留本机）。

### 4.1 DeepSeek（已验证）

```dotenv
HOTLINE_LLM_BACKEND=openai
HOTLINE_LLM_BASE_URL=https://api.deepseek.com
HOTLINE_LLM_MODEL=deepseek-flash
HOTLINE_LLM_API_KEY=本地填写
HOTLINE_LLM_MAX_TOKENS=12000
HOTLINE_LLM_EXTRA_BODY={"thinking":{"type":"disabled"}}
HOTLINE_EXTRACTION_VALIDATION_RETRIES=1
HOTLINE_HTTP_TRUST_ENV=false
HOTLINE_QA_CONTEXT_CHARS=30000
HOTLINE_QA_VALIDATION_RETRIES=1
```

模型名以账号可用列表为准。参考：[接口](https://api-docs.deepseek.com/)、[JSON 输出](https://api-docs.deepseek.com/guides/json_mode/)、[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。

### 4.2 其它 OpenAI-compatible

```dotenv
HOTLINE_LLM_BACKEND=openai
HOTLINE_LLM_BASE_URL=http://127.0.0.1:8000/v1
HOTLINE_LLM_MODEL=你的模型名
HOTLINE_LLM_API_KEY=
```

须支持 `response_format=json_object`。本地栈用 `openai` 后端，不要用 `kag`。

### 4.3 可选：本地 Ollama / vLLM

与 OpenSPG 栈解耦，Compose 项目名 **`hotline-llm`**。

```bash
"$PY" scripts/prepare_local_llm.py --engine ollama --tier cpu
# 已有 deploy/llm.env 时拒绝覆盖

docker compose --project-name hotline-llm \
  --env-file deploy/llm.env \
  -f deploy/compose.llm.yml \
  --profile ollama up -d

"$PY" scripts/configure_local_llm.py
# 若 .env 仍指向云 API：加 --force
"$PY" scripts/check_local_llm.py
```

| Profile | 端口 | `HOTLINE_LLM_BASE_URL` |
| --- | --- | --- |
| `ollama`（CPU） | `127.0.0.1:11434` | `http://127.0.0.1:11434/v1` |
| `ollama-gpu` | 同上 | 同上（需 NVIDIA Container Toolkit） |
| `vllm` | `127.0.0.1:8000` | `http://127.0.0.1:8000/v1` |

健康检查只证明接口契约，不保证政务抽取质量。CPU Ollama 建议 `HOTLINE_LLM_TIMEOUT=300`。

---

## 5. 生成依赖镜像配置与嵌入 wheel

```bash
"$PY" scripts/prepare_local_deploy.py
```

行为：写入 `deploy/images.env` + `deploy/images.lock.json`；生成 `MYSQL_PASSWORD` / `GRAPH_PASSWORD` / `EMBEDDING_API_KEY`；把图密码与 `HOTLINE_NAMESPACE=HotlineTest` 写回 `.env`。**已有 `images.env` 时拒绝覆盖。**

```bash
"$PY" -m ensurepip
"$PY" scripts/download_embedding_wheels.py
```

wheel 在 `deploy/embedding-wheels/`（Git 忽略）。

---

## 6. 启动依赖容器（拓扑 A）

不要把 `compose config` 的展开结果打到共享日志（含密码）。

```bash
COMPOSE=(
  docker compose --project-name hotline-dev
  --env-file deploy/images.env
  -f vendor/openspg/dev/release/docker-compose.yml
  -f deploy/compose.override.yml
)

"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" up -d mysql neo4j minio server embedding
```

等待就绪：

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
# 应含 hotline-dev-mysql / neo4j / minio / openspg / embedding-*

for i in $(seq 1 36); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8887 || true)
  echo "openspg try $i -> $code"
  if [ "$code" != "000" ]; then break; fi
  sleep 5
done

curl -sS http://127.0.0.1:8090/health
```

管理入口：<http://127.0.0.1:8887>。嵌入健康：<http://127.0.0.1:8090/health>（无 `/v1/models`）。

缺 embedding 时重新执行上面的 `up -d … embedding`。

---

## 7. 创建测试项目

**先确认** `8887` 已可访问，再执行：

```bash
# .env 中应有：
# HOTLINE_NAMESPACE=HotlineTest
# HOTLINE_OPENSPG_URL=http://127.0.0.1:8887

"$PY" scripts/create_local_project.py
```

写回 `HOTLINE_OPENSPG_PROJECT_ID` 与 `HOTLINE_NEO4J_DATABASE`（以脚本输出为准，常为项目库名而非默认 `neo4j`）。

核对：

```dotenv
HOTLINE_NEO4J_URI=bolt://127.0.0.1:7687
HOTLINE_NEO4J_USER=neo4j
HOTLINE_NEO4J_PASSWORD=与 deploy/images.env 中 GRAPH_PASSWORD 一致
HOTLINE_OPENSPG_SCALAR_ENCODING=json
```

可选嵌入检索：

```bash
"$PY" scripts/configure_local_embedding.py
```

---

## 8. Schema 与 doctor

```bash
"$PY" -m hotline.cli schema --output data/Hotline.schema
# 专用项目尚无领域类型时显式应用（已有类型则拒绝）：
"$PY" -m hotline.cli schema --output data/Hotline.schema --apply
"$PY" -m hotline.cli doctor
```

| 字段 | 期望 |
| --- | --- |
| `users_configured` | `true` |
| `model_configured` | `true` |
| `project_id` | 正整数 |
| `namespace` | `HotlineTest…` |
| `kag_version` | 已安装版本号 |
| `neo4j` | `连接成功` |
| `openspg_http` | `200` 等成功码 |

---

## 9. 启动应用（拓扑 A）

只运行**一个**进程：

```bash
"$PY" -m hotline.cli serve --port 8080
```

- 工作台：<http://127.0.0.1:8080>
- OpenAPI：`/docs`
- 健康：`GET /health`

登录：取 `.env` 里 `HOTLINE_USERS` 的**键**作为 Bearer 令牌。

### 9.1 冒烟

```bash
unset HOTLINE_DATA_DIR
"$PY" scripts/deploy_smoke.py
```

报告：`data/validation/deploy-smoke.json`。合成资料不得办理业务。Web 审核时须确认语义质检提示后再通过。

```bash
"$PY" -m pytest -q
# 仅确认指向 HotlineTest 时：HOTLINE_RUN_LIVE=1 "$PY" -m pytest -m live -q
```

---

## 10. 日常运维

### 10.1 限流与监控

- `HOTLINE_RATE_LIMIT_*`
- `GET /health`、`GET /api/status`（维护角色）
- `GET /api/operations/summary`、`/api/operations/audit-export`

### 10.2 备份

须同时备份 `HOTLINE_DATA_DIR` 与 Neo4j/MySQL 卷：

```bash
"$PY" scripts/backup_hotline_data.py --output-root data/backups
"$PY" scripts/restore_hotline_data.py \
  --backup data/backups/hotline-data-<时间戳> \
  --target data/restore-drill
HOTLINE_DATA_DIR=data/restore-drill "$PY" -m hotline.cli doctor
```

卷快照示例（卷名以 `docker volume ls` 为准）：

```bash
docker run --rm -v hotline-dev_hotline_graph:/data -v "$PWD/data/backups:/backup" alpine \
  tar czf /backup/neo4j-graph.tgz -C /data .
docker run --rm -v hotline-dev_hotline_mysql:/var/lib/mysql -v "$PWD/data/backups:/backup" alpine \
  tar czf /backup/mysql-data.tgz -C /var/lib/mysql .
```

### 10.3 权限

客服仅 `PUBLISHED` + 当前 space；维护可见草稿；禁止向客服开放 Neo4j。

### 10.4 停止依赖

```bash
docker compose --project-name hotline-dev \
  --env-file deploy/images.env \
  -f vendor/openspg/dev/release/docker-compose.yml \
  -f deploy/compose.override.yml \
  stop
```

仓库不提供清库命令。

---

## 11. 环境变量速查

完整模板：[`.env.example`](../.env.example)。

| 变量 | 含义 |
| --- | --- |
| `HOTLINE_USERS` | 令牌 → 角色 |
| `HOTLINE_LLM_*` / `HOTLINE_QA_*` | 模型与问答 |
| `HOTLINE_EMBEDDING_*` | 可选语义召回 |
| `HOTLINE_OPENSPG_*` / `HOTLINE_NAMESPACE` / `HOTLINE_NEO4J_*` | 图平台 |
| `HOTLINE_OPENSPG_SCALAR_ENCODING` | 默认 `json` |
| `HOTLINE_DEFAULT_KNOWLEDGE_SPACE` | `production` / `test` |
| `HOTLINE_OCR_*` / `HOTLINE_WORKER_ENABLED` | OCR / worker |
| `HOTLINE_RATE_LIMIT_*` | POST 限流 |

`deploy/images.env`：`OPENSPG_*_IMAGE`、`MYSQL_PASSWORD`、`GRAPH_PASSWORD`、`EMBEDDING_API_KEY`。

---

## 12. 故障排查

| 现象 | 处理 |
| --- | --- |
| `openspg_http` 失败 | 容器是否 Up；等就绪；查 8887 |
| `neo4j` 失败 | 密码与 `.env` 是否一致；查 7687 |
| 无 embedding | 重跑 `up -d … embedding`；确认 wheel |
| `create_local_project` 失败 | 见 `data/validation/project-error.json`；命名空间须 `HotlineTest*` |
| `schema --apply` 拒绝 | 类型已存在 |
| 抽取大量失败 | 加大超时或换模型 |
| Apple Silicon 拉/跑镜像异常 | 确认 amd64 模拟或换可用引擎 |

---

## 附录 A. 拓扑 B：应用也容器化

在拓扑 A 依赖已就绪、`.env` 已配置后：

```bash
docker compose --project-name hotline-dev \
  --env-file deploy/images.env \
  -f vendor/openspg/dev/release/docker-compose.yml \
  -f deploy/compose.override.yml \
  up -d --build hotline
```

容器内由 override 注入 `HOTLINE_OPENSPG_URL=http://server:8887`、`HOTLINE_NEO4J_URI=bolt://neo4j:7687`。  
**模型地址不要写容器内的 `127.0.0.1`：** 云 API 用公网 URL；宿主机上的 Ollama/vLLM 用 `http://host.docker.internal:端口/v1`。  
嵌入在容器内应使用 `http://embedding:8090/v1`（可用 `configure_local_embedding` 的宿主机地址仅适用于拓扑 A）。

深度验证弱于拓扑 A；首次上线请自跑 `deploy_smoke` 或等价验收。

---

## 附录 B. 安装清单

- [ ] 已选拓扑 A / B / C  
- [ ] `git clone` 到自有目录并 `cd` 仓库根  
- [ ] Docker Compose ≥ 2.24.4；Python 3.10（A/C）或接受构建 `hotline` 镜像（B）  
- [ ] `fetch_upstream` → `vendor/`  
- [ ] `.venv` + `init` + 模型写入 `.env`（A/C）  
- [ ] `prepare_local_deploy` + embedding wheels  
- [ ] `up -d mysql neo4j minio server embedding`  
- [ ] `8887` / `8090/health` 可用  
- [ ] `create_local_project` + Schema apply  
- [ ] `doctor` 全绿  
- [ ] `serve` 或 `hotline` 容器 + `deploy_smoke.py`  

---

## 附录 C. 拓扑 C：Windows + WSL Docker

适用：应用在 **Windows** Python，依赖 Docker 仅在 **WSL** 内。

```powershell
git clone <repo-url> hotline-knowledge
cd hotline-knowledge

# 由「当前目录」推导 WSL 路径（勿抄他人机器路径）
$Repo = (Get-Location).Path
$drive = $Repo.Substring(0, 1).ToLower()
$LinuxRoot = '/mnt/' + $drive + ($Repo.Substring(2) -replace '\\', '/')

uv venv --python 3.10 .venv
$PY = Join-Path $Repo '.venv\Scripts\python.exe'
uv pip install --python $PY -r requirements.lock -e . -e vendor/KAG
& $PY -m hotline.cli init
# … 按主文编辑 .env、prepare_local_deploy、download_embedding_wheels …

# 可选：WSL 保活，避免空闲退出
Start-Process -FilePath wsl.exe -ArgumentList @('-u','root','-e','sleep','infinity') -WindowStyle Hidden

wsl -u root -e docker compose --project-name hotline-dev `
  --env-file "$LinuxRoot/deploy/images.env" `
  -f "$LinuxRoot/vendor/openspg/dev/release/docker-compose.yml" `
  -f "$LinuxRoot/deploy/compose.override.yml" `
  up -d mysql neo4j minio server embedding
```

其余步骤与主文相同，将 `"$PY"` 换成 `& $PY`，将 `docker compose` 换成上面的 `wsl -u root -e docker compose …` 并带 `$LinuxRoot` 路径。全新 WSL 装 Docker 可用 `scripts/install_docker_wsl.sh`（**须操作者明确授权**）。

若 Windows 已安装 Docker Desktop 且 `docker` 在 PATH 中，也可直接在 PowerShell 用主文 bash 等价的 `docker compose`（路径用仓库相对路径），无需 `$LinuxRoot`。
