# 本地大模型推理服务接入设计

日期：2026-11-11  
状态：已实现  
范围：为政务热线知识图谱构建提供可切换的本地 LLM 部署与健康检查，不局限于第三方云 API（如 DeepSeek）。

## 1. 背景与目标

### 1.1 现状

- 应用已通过 `hotline.extraction.ModelClient` 调用 OpenAI 兼容接口。
- `HOTLINE_LLM_BACKEND` 支持 `kag` 与 `openai`；`HOTLINE_LLM_BASE_URL` / `HOTLINE_LLM_MODEL` / `HOTLINE_LLM_API_KEY` 已可指向任意兼容服务。
- 文档提及可用 vLLM 等本地服务，但仓库内**没有**与嵌入服务同级的本地 LLM Compose、拉模型脚本与连通性/JSON 能力检查。
- 当前联调验证主要使用 DeepSeek 云 API。

### 1.2 目标（本轮交付 = 原选项 B）

1. 以**独立推理微服务**方式支持本地大模型（不在 `hotline` 进程内加载权重）。
2. 同时支持 **Ollama**（CPU / 入门 GPU）与 **vLLM**（更强 GPU）。
3. 提供 Compose 叠加层、环境模板、准备/配置脚本，以及「可达 + 模型存在 + JSON 对象输出」健康检查。
4. 应用继续只使用现有 `HOTLINE_LLM_*`；DeepSeek 与本地通过配置切换。

### 1.3 非目标

- 进程内 transformers / llama-cpp 推理。
- 修改 KAG / OpenSPG 上游核心代码。
- 把 LLM 服务塞进现有 `deploy/compose.override.yml`。
- 本轮强制真实合成资料本地模型全链路验收（原选项 C）。
- 自动选模、多模型负载均衡、为迁就弱模型改抽取提示词或 Schema。

## 2. 已确认决策

| 决策点 | 选择 |
| --- | --- |
| 部署形态 | A：独立推理服务 |
| 引擎 | 3：Ollama + vLLM 都支持 |
| 算力覆盖 | C：CPU 与 GPU 双路径 |
| 交付深度 | B：配置 + 文档 + 脚本 + 健康检查 |
| 落地结构 | 方案 1：独立 `deploy/compose.llm.yml` + profiles，与 OpenSPG 栈解耦 |

## 3. 架构

```text
[文档上传 / OCR] → [hotline ModelClient]
                        │  HOTLINE_LLM_BASE_URL / MODEL
                        │  HOTLINE_LLM_BACKEND=openai（本地推荐）
            ┌───────────┴───────────┐
            ▼                       ▼
     云 API（如 DeepSeek）     本机 LLM 服务（二选一写入 .env）
                       ┌────────────┴────────────┐
                       ▼                         ▼
                Ollama profile              vLLM profile
                CPU / 入门 GPU              更强 GPU
```

原则：

- 不改变抽取 → 审核 → OpenSPG 写图 → Neo4j 回读主链路。
- OpenSPG / Neo4j / MySQL / MinIO / embedding 仍用既有 compose；LLM 额外使用独立项目名与文件，例如：
  `docker compose --project-name hotline-llm --env-file deploy/llm.env -f deploy/compose.llm.yml --profile ollama up -d`
  （vLLM 将 `--profile` 换为 `vllm`）。不把 LLM 服务并入 `hotline-dev` 项目，避免与 OpenSPG 栈生命周期耦合。
- 同一时刻应用只绑定一套 `HOTLINE_LLM_*`；两个 profile 不应同时作为「当前写入 .env 的配置源」。

## 4. 文件边界

| 路径 | 职责 |
| --- | --- |
| `deploy/compose.llm.yml` | 仅定义 `ollama`、`vllm` 两个 profile 服务；端口绑定 `127.0.0.1` |
| `deploy/llm.env.example` | 镜像、模型、端口、GPU/显存相关、可选 API Key 模板 |
| `deploy/llm.env` | 本机生成（Git 忽略） |
| `scripts/prepare_local_llm.py` | 生成 `llm.env`，已存在则拒绝覆盖；写入推荐默认值 |
| `scripts/configure_local_llm.py` | 探测 → 校验模型与 JSON → 备份并写回 `.env` 的 `HOTLINE_LLM_*` |
| `scripts/check_local_llm.py` | 独立健康检查 CLI |
| `hotline/local_llm.py` | 共享探测实现（models 列表、JSON 探测、报告结构），供脚本与 `doctor` 导入 |
| `hotline/cli.py`（`doctor`） | 增加 `local_llm` 摘要字段，不打印密钥 |
| `deploy/README.md`、`.env.example`、`docs/deployment.md` | 使用说明与交叉引用 |
| `.gitignore` | 增加 `deploy/llm.env` |

实现时镜像 tag 尽量钉死（或记录 digest），避免无记录的浮动 `latest` 作为唯一依据；`llm.env.example` 中写明可替换来源。

## 5. 服务参数与默认模型

### 5.1 端口与 API 根路径

| Profile | 服务名 | 宿主机监听 | 应用 `HOTLINE_LLM_BASE_URL` |
| --- | --- | --- | --- |
| `ollama` | `ollama` | `127.0.0.1:11434` | `http://127.0.0.1:11434/v1` |
| `vllm` | `vllm` | `127.0.0.1:8000` | `http://127.0.0.1:8000/v1` |

### 5.2 默认模型

| 场景 | 引擎 | 默认 `LLM_MODEL` | 说明 |
| --- | --- | --- | --- |
| CPU / 入门 | Ollama | `qwen2.5:7b-instruct` | 中文指令较好；权重由 Ollama pull |
| GPU 入门（约 8–12GB） | Ollama | `qwen2.5:14b-instruct` | 仍走 Ollama 便于先跑通 |
| 更强 GPU | vLLM | `Qwen/Qwen2.5-14B-Instruct` | HuggingFace 模型 ID；可在 `llm.env` 换更大模型 |

文档必须声明：结构化政务抽取对模型能力敏感；小模型可能导致 Schema/证据校验频繁失败，属能力问题而非接口配置问题。不把量化权重文件纳入本仓库。

### 5.3 `deploy/llm.env` 关键项

```dotenv
LLM_ENGINE=ollama
OLLAMA_IMAGE=
VLLM_IMAGE=
LLM_MODEL=
LLM_HOST_PORT=11434
LLM_API_KEY=
VLLM_GPU_MEMORY_UTILIZATION=0.9
VLLM_MAX_MODEL_LEN=16384
OLLAMA_KEEP_ALIVE=24h
```

`prepare_local_llm.py` 按用户选择的引擎（参数或交互约定在实现计划中定为 CLI 参数，例如 `--engine ollama|vllm` 与可选 `--tier cpu|gpu-entry|gpu-strong`）填充默认模型与端口。

### 5.4 写入应用 `.env` 的规则

`configure_local_llm.py` 在检查通过后写入（先备份到 `data/config-backups/`，模式对齐 `configure_local_embedding.py`）：

```dotenv
HOTLINE_LLM_BACKEND=openai
HOTLINE_LLM_BASE_URL=http://127.0.0.1:<port>/v1
HOTLINE_LLM_MODEL=<llm.env 中 LLM_MODEL>
HOTLINE_LLM_API_KEY=<有则写，无则空>
HOTLINE_LLM_EXTRA_BODY={}
```

约束：

1. **覆盖保护**：若现有 `HOTLINE_LLM_BASE_URL` 已指向非本机预设地址（不是 `http://127.0.0.1:11434/v1` 且不是 `http://127.0.0.1:8000/v1`），默认拒绝覆盖；仅当显式 `--force` 时允许从云 API 切到本地。
2. 本地统一 `BACKEND=openai`，并要求 `response_format=json_object`，避免 KAG 客户端附加字段导致本地服务拒请求。
3. 不打印 API Key；不修改系统环境变量；不改动无关配置键。
4. Ollama 在 configure 阶段若模型未就绪：通过 **容器内** `docker exec` 对 `hotline-llm` 项目中的 `ollama` 服务执行 `ollama pull <model>`（不依赖宿主机是否安装 Ollama CLI）；失败则中止且不修改 `.env`。vLLM 权重由容器启动时按 `LLM_MODEL` 拉取/加载，configure 不执行额外 pull 命令，只做可达与 JSON 探测；若模型仍在加载则重试有限次数后失败。
5. 超时：configure 成功后，若 `.env` 中不存在 `HOTLINE_LLM_TIMEOUT`，或取值等于应用默认 `90`，则写入 `300`；若用户已设为其他值（含小于 90 的显式值），一律保留不改。

## 6. 健康检查与错误处理

### 6.1 `scripts/check_local_llm.py` 步骤

任一步失败即停止，给出可操作原因；不回传上游密钥或过长响应正文：

1. 配置完备：`HOTLINE_LLM_BASE_URL`、`HOTLINE_LLM_MODEL` 非空。
2. HTTP 可达：请求 `{base}/models`（必要时兼容引擎差异），校验超时与可选 `Authorization`。
3. 模型存在：列表中包含配置模型名（适配 Ollama / vLLM 字段差异）。
4. JSON 对象能力：极短 chat 请求，带 `response_format={"type":"json_object"}`，响应体可 `json.loads` 为 object；拒绝截断输出。
5. 输出结构化报告：`reachable`、`model_present`、`json_object_ok`、`latency_ms` 等。

`configure_local_llm.py` 改 `.env` 前必须通过步骤 2–4。检查逻辑提取为可导入的共享函数（例如 `hotline/local_llm.py` 或 `scripts` 下可被 import 的模块），`check_local_llm.py` 与 `doctor` **调用同一实现**，避免 subprocess 互调。`doctor` 增加 `local_llm` 摘要；未配置模型时报告 `未配置`，不把缺失配置显示为连接成功。

### 6.2 错误表

| 情况 | 行为 |
| --- | --- |
| 容器未启动 / 连接拒绝 | 非零退出 + 提示按 deploy README 启动对应 profile |
| 模型未拉取 | check 只报告；configure 可 pull，pull 失败不改 `.env` |
| 不支持 `json_object` | 失败并说明需换支持 OpenAI JSON mode 的模型/引擎；**不**静默降级为纯文本再解析 |
| 已配置云 API 且无 `--force` | configure 拒绝覆盖 |
| 应用运行时调用失败 | 保持现有 `ModelClient` → `ServiceError`；不改抽取语义 |

## 7. 文档与测试

### 7.1 文档

- `deploy/README.md`：Ollama（CPU/入门）与 vLLM（GPU）启动命令、拉模型、configure/check、与 DeepSeek 切换、超时与显存注意。
- `.env.example`：增加本地 URL 注释示例，不含真实密钥。
- `docs/deployment.md`：指向 deploy README，并注明本轮提供健康检查，真实本地抽取闭环不作为必验项。

### 7.2 测试

- 对 check/configure 的 URL 判定、覆盖保护、JSON 失败不写 env 做单元或脚本级测试（httpx mock）。
- 默认 CI **不**下载真实大模型、不要求 GPU。

## 8. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 小模型 JSON/政务抽取质量差 | 文档明确能力边界；健康检查只证接口契约，不证抽取质量 |
| Ollama/vLLM JSON mode 行为差异 | 统一走 `openai` 后端 + 硬性 `json_object` 探测 |
| Windows + GPU 驱动复杂 | 文档以 WSL2/Linux Docker 为主路径；与现有 OpenSPG WSL 部署一致 |
| 误覆盖 DeepSeek 配置 | 非本机预设 URL 默认拒绝覆盖，需 `--force` |
| 镜像浮动 | example/脚本钉 tag 或记录 digest |

## 9. 成功标准

1. 可用文档中的 Compose profile 分别拉起 Ollama 或 vLLM（在具备相应算力的机器上）。
2. `prepare_local_llm.py` / `configure_local_llm.py` / `check_local_llm.py` 行为符合本文约束。
3. `doctor` 能汇总本地 LLM 检查摘要且不泄露密钥。
4. 将 `.env` 指到本地服务后，现有 `ModelClient` 无需改抽取协议即可发起调用（质量取决于所选模型）。
5. 从 DeepSeek 切到本地、再切回，覆盖保护与 `--force` 行为符合预期。

## 10. 实现顺序（预览，正式计划另文）

1. `.gitignore` + `llm.env.example` + `compose.llm.yml`
2. `prepare_local_llm.py` / `check_local_llm.py` / `configure_local_llm.py`
3. `doctor` 汇总
4. 单元测试（mock）
5. 更新 `deploy/README.md`、`.env.example`、`docs/deployment.md`
