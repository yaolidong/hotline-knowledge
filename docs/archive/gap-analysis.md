# 源码审查与差距分析

审查日期：2026-11-03。初始工作区只有 `.git` 和未跟踪的 `.spec-workflow` 模板，没有业务代码、根目录 AGENTS.md 或部署工程。保留现有模板不动。

## 版本与环境

| 项目 | 实际检查结果 |
|---|---|
| KAG | `0.8.0`，提交 `fdab15b3929d2ee40dfcdd388f90233096a6afc9` |
| OpenSPG | 提交 `ceeb3ef549df79ca4c4878e7ff452c73584991f3` |
| 主机 | Windows、PowerShell；系统 Python 3.12.6；项目虚拟环境 Python 3.10.18；Node 24；Java 8 |
| 容器 | 初始未安装；获得用户明确授权后，在 WSL Ubuntu 24.04 安装 Docker Engine 29.9.2 / Compose 5.7.0 |
| 服务 | 当前已启动 OpenSPG、Neo4j 5.25.1、上游 MySQL 镜像（实际 MariaDB 10.5.8）、MinIO、本地 BGE 嵌入服务；DeepSeek 实际接入 |
| KAG 安装 | 安装到项目 `.venv`；按实际导入失败约束 Pydantic 2.10.x，保留上游 MCP 1.6.0 |

KAG 使用 Python/setuptools、`requirements.txt`、注册式 builder/solver 组件。OpenSPG 使用 Maven 多模块 Java/Scala，包含 builder、reasoner、server 和 cloudext。官方 compose 包含 OpenSPG、MySQL、Neo4j、MinIO；服务镜像提供产品入口，仓库中未发现可直接编辑的完整 Web 前端工程。

## 按源码确认的复用边界

| 能力 | 分类 | 源码依据与本项目处理 |
|---|---|---|
| LLM 调用 | 可直接复用/需配置 | `kag/common/llm/openai_client.py` 支持 openai/vllm 注册；本项目默认复用 `OpenAIClient`，另提供直接兼容客户端以支持不接受 KAG 额外请求字段的服务 |
| 子图与写入 | 可直接复用/需配置 | `kag/interface/common/model/sub_graph.py`、`knext/graph/client.py`、`GraphController.java`；复用 `SubGraph/Node/Edge` 和真实 `writerGraph` HTTP 协议 |
| SDK 鉴权 | 需要扩展 | 当前 `GraphClient.write_graph` 硬编码 token 且不显式返回结果；本项目薄 HTTP 适配器使用配置 token，检查 success/result，再核对节点与边 |
| Schema | 可复用/需配置 | `knext/schema/marklang/schema_ml.py`；通过真实 MarkLang 离线解析和 SchemaClient 创建类型；不运行可能删除旧类型的 `sync_schema` |
| Schema 约束抽取 | 需要扩展 | `kag/builder/component/extractor/schema_constraint_extractor.py` 有可替换提示词和规范化，但输出不满足每条事实的精确证据、条件树与审核要求；增加政务输出契约、证据校验，不另造通用 RAG 引擎 |
| 文档解析 | 需要扩展 | 上游 PDFReader/DocxReader/TXTReader 已存在，依赖 pdfminer/PyPDF2/python-docx；其输出不保证本任务所需的统一精确偏移/中文条款/扫描页完整性。复用文档解析库，新增小型带位置解析层，不修改上游 Reader |
| 融合 | 部分可复用、P1 待做 | 上游存在标准化、aligner/external graph。政务地区/时间/审核合并机制没有直接确认可用；当前仅同版本精确断言去重，跨文档保留独立来源和 canonicalKey |
| 图存储 | 可直接复用 | `Neo4jSinkWriter.java`、`Neo4jStoreClient.java` 按 namespace.label、业务 id 写节点与有属性的关系；不另建伪图表或文档向量替代品 |
| 图检索 | 需要适配 | KAG GraphClient 的 query_vertex/expend_one_hop 声明与本次 Java 源码端点不齐；Java CustomSearchRequest 只有 projectId/customQuery，未接受 SDK 所传 params。采用 Neo4j 驱动固定参数化读取，避免依赖猜测接口和字符串拼接用户输入 |
| 混合推理 | 暂不启用完整框架 | 已检查 SearchClient 的 text/vector/custom 接口与 solver/retriever 结构。通用检索没有本项目每版本发布白名单，P0 先使用领域实体/别名 + 图关系 + 原文证据查询；向量与完整 solver 接入延后 |
| Web 图谱/政务操作 | 需要新建 | 官方产品入口存在于部署说明/镜像；仓库未包含可修改的完整产品前端。新增同源轻量 Web 工作台，通过真实 API 读取实际图数据 |
| 审核、任务与来源 | 需要新建 | 新增 SQLite 文档/版本/审计，原文件本地持久化；正式图谱仍在 OpenSPG 对应 Neo4j 中 |
| OCR/MinerU | 已扩展基础 OCR，复杂版面暂缓 | 2026-11-04 接入 RapidOCR 3.10.0 / ONNX CPU，复用同一 Chunk 契约；PDFium 渲染支持原件审阅与扫描页 OCR。MinerU、复杂表格与多栏重建尚未接入 |

## 已确认的兼容性问题

1. MarkLang `parse_predicate` 正则不接受下划线。领域 API 保留 `REQUIRES_MATERIAL`，图物理谓词统一映射 `requiresMaterial`，边上另存 `relationType`。映射集中在 `schema.spg_name`。
2. KAG 0.8 固定的 MCP 1.6.0 导入 Pydantic 内部 `eval_type_backport`，当前新版 Pydantic 中该符号缺失；首次导入真实失败。约束 Pydantic 2.10.x、FastAPI 0.115.x，并用新 Python 进程验证 KAG 导入，避免模块缓存掩盖失败。
3. 官方 compose 使用 `latest`，不能证明镜像与当前仓库提交完全一致。容器覆盖配置要求部署者明确指定镜像 tag/digest，实际兼容性必须经过 live 测试确认。
4. 真实部署发现锁定版本 `Neo4jSinkWriter.writeNode/writeEdge` 对标量调用 `JSON.toJSONString`，数据库中的 recordId/name/关系属性带 JSON 引号；id 单独保留。图适配器按该存储格式编码查询参数并解码返回值，不修改上游代码或已写入图。`HOTLINE_OPENSPG_SCALAR_ENCODING` 支持升级后明确切换 plain，禁止未经回读验证直接切换。
5. OpenSPG 项目创建接口返回裸对象/列表，writerGraph 使用 success/result 包装，两者不同。项目创建还会实际调用 vectorizer；本次使用真实 FastEmbed/ONNX 中文 BGE 模型完成校验，未使用上游 mock vectorizer。
6. 官方美国西部镜像源可用，已锁定四个镜像摘要，见 `deploy/images.lock.json`。OpenSPG 镜像内 KAG 为 `0.8.0.20250703.2020`，本机扩展使用源码版 0.8.0；二者实际联调通过，但不能声称镜像与源码提交完全同构。

## 实际改动路径

所有二次开发在 `hotline/`、`tests/`、`scripts/`、`deploy/` 与 `docs/`。上游保持原样。核心路径：

`app → store/worker → parsing → extraction/schema → DRAFT → review → graph.publish → PUBLISHED → query/graph API → Web`。

本地源码证据均位于 `vendor/` 对应路径。上游固定版本链接：[KAG](https://github.com/OpenSPG/KAG/tree/fdab15b3929d2ee40dfcdd388f90233096a6afc9)、[OpenSPG](https://github.com/OpenSPG/openspg/tree/ceeb3ef549df79ca4c4878e7ff452c73584991f3)。
