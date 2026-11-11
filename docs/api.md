# API 与权限

所有 `/api/*` 使用 `Authorization: Bearer <token>`。`/health`、静态页面和 OpenAPI 描述可以匿名访问，业务数据必须鉴权。角色从服务端 `.env` 映射，客户端不能指定角色或自动获取管理员权限。

资料空间与业务运营新增接口见 [事项工作台接口](business-workbench.md)。查询默认正式资料，合成回归需显式传 `space: "test"`；图谱、来源与事项 GET 接口用 `?space=test`。返回的 trace_id 可用于本人答复反馈。

| 角色 | 权限 |
|---|---|
| admin | 全部业务操作 |
| maintainer | 导入、查看草稿/历史、修正实体关系、重新处理；不能审核发布 |
| reviewer | 查看草稿/历史、审核、拒绝、发布、停用；不能导入或修改内容 |
| agent | 客服查询、已发布图谱与已发布来源；不能读取草稿或修改知识 |

## 路径

| 方法与路径 | 功能 | 主要约束 |
|---|---|---|
| GET `/health` | 存活与依赖摘要 | `checks`：模型是否配置、图是否可达、项目是否配置、worker；未配模型时 `degraded` |
| GET `/api/me` | 当前身份 | 已鉴权 |
| GET `/api/status` | 配置/图连接/限流/本地 LLM 探测 | 维护类角色；不返回凭据；非本机预设模型地址不探测 |
| GET `/api/schema` | 领域定义 | 已鉴权 |
| POST `/api/import/text` | 直接文本导入 | admin/maintainer；返回 202 |
| POST `/api/import/files` | 多文件上传 | multipart：多个 `files`，`metadata` 为 JSON 字符串；最多 10 个，默认总计 20 MB |
| GET `/api/revisions?limit=50&offset=0` | 任务/版本列表 | 维护类角色；limit 1–100 |
| GET `/api/revisions/{rid}` | 完整提取/解析结果 | 维护类角色 |
| POST `/api/revisions/{rid}/reprocess` | 创建新处理版本 | admin/maintainer；同文档有运行任务时 409 |
| POST `/api/revisions/{rid}/retry-extraction` | 原版本重试失败块 | admin/maintainer；JSON `expected_version`；仅 DRAFT 且 PARTIAL/FAILED、有分块检查点；版本过期或有运行任务时 409 |
| PUT `/api/revisions/{rid}/extraction` | 修正实体/关系/证据 | admin/maintainer；必须重新通过 Schema/证据校验 |
| POST `/api/revisions/{rid}/review` | 审核状态流转 | admin/reviewer；expected_version 必须匹配 |
| GET `/api/revisions/{rid}/fact-review` | 逐项状态、事实组与发布快照 | 维护类角色 |
| POST `/api/revisions/{rid}/facts/{key}/decision` | 单条确认/退回/拒绝 | admin/reviewer；版本和处理依据必填 |
| PUT `/api/revisions/{rid}/chunks/{cid}/manual` | 失败块人工补录增量 | admin/maintainer；Schema 和本块证据校验 |
| POST `/api/revisions/{rid}/groups/{gid}/review` | 事实组审核/退回 | admin/reviewer；覆盖全文、完整性声明、逐项质检处置 |
| POST `/api/revisions/{rid}/groups/{gid}/publish` | 发布该组独立快照或重试 | admin/reviewer；只允许已审核且内容未变的组 |
| POST `/api/revisions/{rid}/review-copy` | 复制为修订草稿 | admin/maintainer；保留已发布快照，不调用模型 |
| GET `/api/revisions/{rid}/history` | 审计/历史快照 | 维护类角色 |
| GET `/api/evidence/{rid}/{chunk_id}` | 原文定位 | agent 仅可读取已发布版本 |
| GET `/api/evidence/{rid}/{chunk_id}/locate?start=0&end=10` | 精确范围对应的页框/单元格及源文件散列 | 同来源权限；返回 LOCATED 或 TEXT_ONLY；偏移必须有效 |
| GET `/api/revisions/{rid}/identity-candidates?entity_id=...` | 跨文档同地区同类型候选及审核记录 | 维护类角色；最多 20 候选，has_more 表示截断 |
| POST `/api/identity/decisions` | 确认/撤销实体身份关联 | admin/reviewer；双方版本、上一事件 ID 与必填依据；不合并图事实 |
| GET `/api/source/{rid}` | 原文件下载 | 同上；返回附件，不以内联 HTML 打开 |
| GET `/api/source/{rid}/text` | 解析全文及分块位置 | 同上；客服不能查看草稿或停用版本 |
| GET `/api/source/{rid}/info` | 原件类型、页数、版本、每页解析方式与 OCR 行框 | 同上；不依赖知识抽取成功，失败任务仍可审阅原件 |
| GET `/api/source/{rid}/pages/{page_number}?size=1800` | PDF 或扫描图片的实际页面 PNG | 同上；页码从 1 开始；size 600–3000；每次鉴权，不缓存到公共 URL |
| GET `/api/graph` | 图数据库查询 | keyword/entity_type/relation_type/region/service/revision_id/focus/offset/limit；limit 最大 150；revision_id 只允许当前角色与资料空间可见的版本 |
| POST `/api/query` | 客服自然语言问题 | 所有已鉴权角色；知识范围一律只含 PUBLISHED |
| POST `/api/qa` | 模型根据检索证据解释 | 请求结构同 query；先检索已发布图事实和原文，再调用配置模型 |

## 请求示例（仅合成测试）

文本导入：

```json
{
  "title": "合成测试办事指南",
  "region": "测试甲区",
  "document_number": "",
  "source": "软件回归测试样例",
  "document_type": "guide",
  "synthetic": true,
  "text": "合成测试，非真实政策。第一条 示例登记由测试服务局办理。"
}
```

响应包含 document_id、revision_id、duplicate。重复内容且元数据相同复用已有任务，显式重新处理另建版本。

文件导入支持 PDF/DOCX/TXT/PNG/JPEG/WebP，图片会核验实际格式、2400 万像素上限及是否单帧。元数据可传 `ocr_mode: "auto" | "force" | "off"`，默认自动；强制模式对 PDF 全部页面 OCR，关闭模式下扫描件明确失败。默认 OCR 策略不改变历史去重指纹。

任务列表另支持 `search`（最长 300 字）、`state`；返回 `{items,total}`。RUNNING 筛选包含排队任务；DRAFT 仅列出抽取完成的待审核版本。每项的 `stage` 为 `parse` 或 `extract`，运行时 `progress/total` 分别对应页面或文本块。字段只描述真实已完成工作，不生成假进度。

`state=PARTIAL` 表示存在通过校验的知识，但至少一个块失败。抽取阶段的 progress 计已尝试块数（含失败），不能作为知识完整率。详情 `parsed_metadata._extraction` 按 chunk ID 保存状态、错误、输入指纹、累计处理轮数和已校验结果；`extraction` 保留成功块的聚合结果。页面可查看失败块原文、重试或人工补录，也可修正已有条目。成功但没有政策事实的块允许返回空结果；整份没有实体仍标为 FAILED。PARTIAL 不能整篇审核发布；已核对完整且不依赖失败块的事实组可发布独立快照，其他块必须逐项说明与该组无关的依据。客服无权读取原待审版本，但可溯源已发布快照共享的原文件。

新页面对待审资料使用事实组流程；组审核字段、修订和兼容边界见 [事实组审核归档](archive/fact-group-review.md)。发布快照有独立版本 ID，原审核草稿继续保留 DRAFT，不能把快照状态视为整份原文已审核。

重试复用原解析正文；原文、上下文、Schema、提示或模型名改变时相应抽取缓存失效。需要重新解析、修改原件或处理旧版没有检查点的失败记录，应调用 reprocess 创建新版本。历史已发布资料不被覆盖。

OCR 位置信息存于版本 `parsed_metadata._source.pages`；每页包含 page、method、文档全文 start/end、lines。OCR 行含逐字 text、页内 start/end、confidence 和四点 box，坐标按该页宽高归一化；这些不是模型的正确率指标。原文块偏移仍以保存的规范正文为准。

审核/发布：

```json
{"action":"approve","expected_version":1,"note":"已对照原文核对完整条件和适用地区"}
```

新版提取 `schema_version=2` 的记录详情增加 `quality`。页面按当前 `quality.issues[].id` 提交 `issue_resolutions`，每项包含 `status`（`verified` 或 `false_positive`）及非空 `note`（最长 500 字）。必须覆盖所有当前问题；`unresolved`、纯空白理由或问题集合变化返回 409，空字符串理由返回 422。页面在请求前定位未处理/未填写的项目。旧客户端的 `acknowledged_issues` 仍按原兼容规则处理并标记审核模式；确认不免除逐字证据校验和原文审核。

action 可选 approve/reject/publish/retire。发布前必须 APPROVED；发布响应成功意味着写入和回读核验已通过。失败返回错误并保留 APPROVED，version 已增加，重试前需要刷新。

抽取完成、审核通过均不等于正式入图。页面审核通过后提供“发布到图谱”，发布成功自动打开该版本的真实图谱；已发布记录也可使用“查看此版本图谱”。进入图谱页面会重新查询数据库，版本筛选可通过“全部资料”解除。`revision_id` 不放宽角色、资料空间和发布状态限制。

身份审核请求包含 source_revision/source_entity/target_revision/target_entity、source_version/target_version、expected_event_id（首次为 0）、action（confirm/revoke）、note。事件保留完整历史；后续实体更正或来源拒绝、停用会使确认失效。

问答响应增加 `date_resolution`（QUESTION/EXPLICIT/TODAY/AMBIGUOUS/CONFLICT/INVALID）及 `retrieval.original_text`（GRAPH_KEYWORD/GRAPH_KEYWORD_VECTOR_RRF/GRAPH_KEYWORD_FALLBACK）。引文可含 locator；TEXT_ONLY 明确表示没有可验证的版面区域。

修正：

```json
{"expected_version":2,"note":"依据原文修正关系","extraction":{"entities":[],"relations":[]}}
```

上例仅说明请求结构；实体/关系的完整结构见 OpenAPI。已发布/停用/发布过的版本不允许覆盖。每个实体/关系必须有 evidence，实体类型和关系端点来自 Schema；属性和别名不能随意编造。修改后回到 DRAFT。

客服查询：

```json
{"question":"示例登记需要什么材料？","region":"测试甲区","service":"示例登记","as_of":"2026-11-03"}
```

响应包含 answer、facts、citations、services、warnings、latency_ms。状态可能为 ANSWERED、NO_EVIDENCE、NEEDS_CLARIFICATION、CONFLICT、VALIDITY_UNCONFIRMED。答案是图事实的结构化整理，非模型自由解释。未知信息不会生成占位政策事实。

`/api/qa` 另返回 `statements: [{text, citation_ids}]`、`missing_information` 和 `generation: {used, model, status, attempts}`。每段模型解释关联真实 `citations`，包含文档/块 ID、连续原文、字符位置、页码（输入可提供时）及来源 URL。伪造引用、无对应引文的数字、模型自行生成的 URL、非法 JSON 均拒绝接受。默认允许一次带校验反馈的修正，仍失败则保留结构化检索结果并显示警告，不能将 fallback 当作模型成功。

`missing_information` 只能从有限核实项中选择，不能在附加说明中绕过引文校验生成政策事实。生成完成后再次检查发布状态，期间停用则返回 409。多地区、范围超限要求澄清，政策有效性沿用保守判断。无可用证据时不调用模型。

引用逐字匹配不能证明句子在语义上完全正确，因此回答标明“模型基于检索证据的解释”，仍须核对原文，不构成行政决定。

重要错误：401 无身份，403 无权限，409 状态/版本冲突，413 大小超限，422 结构不合法，502 上游写入/模型响应异常，503 外部服务未配置或不可用。

不开放任意 Cypher、SQL、shell 或自定义模型 URL 接口。生成的 `/openapi.json` 为运行时接口定义的权威来源。

新版对话页仍逐轮调用上述接口，不新增 history 或 conversation_id 参数。历史记录仅存在于当前页面，追问沿用可见的 region/service/as_of 范围，每轮重新检索。停止等待只取消前端请求，不保证停止后台模型调用。详见 [对话与图谱说明归档](archive/chat-and-graph.md)。

自然语言召回修复新增响应字段 `scope: {region,service}`、`retrieval: {method}`，`services` 增加来源 `title`。method 可能为 EXACT、CANDIDATES、NO_REGION_MATCH、NO_SERVICE_MATCH、SCOPE_CONFLICT、MULTIPLE_REGIONS、LIMIT。未发布库为空时可不返回这些附加字段。近似名称、宽泛地区候选返回 NEEDS_CLARIFICATION，不生成政策答案；确认后仍用原接口提交正式名称和具体地区。详见 [检索修复与输入方式归档](archive/query-recall.md)。
# 2026-11-04 业务扩展接口

结构化 CSV、版本差异、逐项质检处置、事项编码、受限依据路径、条件核对与运营概览的历史契约说明见 [业务演进归档](archive/business-evolution.md#接口)。空间隔离、检索诊断和反馈接口见 [事项工作台说明](business-workbench.md#接口与配置)。

