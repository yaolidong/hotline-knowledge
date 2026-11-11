# 事项工作台与知识运营第一批实施

2026-11-04。本次延用 OpenSPG/KAG、Neo4j、现有 SQLite audit 表和同源前端；未执行数据库结构迁移，未覆盖既有政策图。

## 已实现

- 正式/合成测试资料空间：默认 `production`，`test` 必须明确切换。问答、语义召回、图浏览、事项列表和客服来源访问均校验空间；不提供混合空间参数。维护人员可通过版本 ID 审阅跨空间资料，不能借此向正式客服查询混入测试事实。身份确认也不能跨空间。
- 事项工作台：读取真实图的已发布事项，按同地区同名聚合展示来源，支持关键词、地区、分页和办理信息详情。同名分组不是身份融合。事实、规则、来源属性和原文均可查看，原件和对话入口复用现有组件。
- 表单审核：支持实体名称/类型/属性/别名/展示名、关系端点、AND/OR/NOT 树、逐字引文和偏移编辑；新增/移除只在保存后生效。服务端继续校验 Schema、原文证据与 expected_version；保存后回到 DRAFT。高级 JSON 保留。不能直接覆盖已发布版本。
- 检索诊断：执行真实图查询与同一原文召回函数，展示范围、图候选、关键词/向量名次、图事实必留块、上下文选择与来源过滤原因。维护角色可用，不调用答案生成模型；开启嵌入服务时仍调用嵌入模型。最多展示 200 个候选块、检查 100 个相关目录版本，并明确标记截断。
- 查询追踪与反馈：使用 audit 追加 QUERY_TRACE、QUERY_FEEDBACK、FEEDBACK_REVIEW。默认只记问题散列、范围、证据版本、结果散列、模型/检索状态和耗时，不保存来电问题正文。提交者可显式附上脱敏问题。用户只能反馈自己的 trace；重复同类反馈幂等；维护人员处理需说明与事件版本，记录可重新打开，不删除历史。
- 接线话术/工单复制：复用已有回答与引用，保留必要核实提醒；工单另包含查询范围、版本标识和 trace。反馈提交与关闭不会自动改动或发布知识。

## 接口与配置

`Question` 增加可选 `space: production | test`。GET 图、事项、资料列表和客服来源接口使用同名查询参数。未传时采用 `HOTLINE_DEFAULT_KNOWLEDGE_SPACE`，默认 production。该配置只控制默认选择，不是文档权限机制；所有已登录角色均可显式进入测试空间，正式部署如需禁止测试入口应另按组织策略配置。

| 接口 | 权限和用途 |
|---|---|
| GET `/api/services?space=production&keyword=...&region=...&limit=30&offset=0` | 已登录用户；只读真实图，返回事项分组与来源；truncated 表示图候选上限 |
| POST `/api/retrieval/diagnose` | admin/maintainer/reviewer；Question 请求；返回实际检索与过滤信息 |
| POST `/api/feedback` | 已登录用户；trace_id、category、note、可选脱敏 question；只能反馈本人查询 |
| GET `/api/feedback?space=production&limit=30&offset=0` | 维护角色；分页反馈及处理状态 |
| POST `/api/feedback/{id}/review` | admin/maintainer；expected_event_id、status、note；乐观并发 |
| GET `/api/operations/summary?days=7&space=production` | 维护角色；版本存量与查询耗时摘要 |
| GET `/api/operations/audit-export?limit=200&action=&since=` | 维护角色；审计索引导出（无完整 snapshot 正文） |

category：wrong_evidence、irrelevant、missing_requirement、outdated、helpful。处理状态 OPEN、RESOLVED、DISMISSED。`/api/query` 与 `/api/qa` 现在返回 trace_id、space；旧调用未填 space 将使用正式空间，不再隐式检索合成样本。历史验收脚本查询合成语料时须显式使用 test。

## 验证与限制

`tests/test_business_operations.py` 使用明确的图替身验证空间隔离、嵌入输入过滤、反馈归属、并发/重复提交、维护权限与停用竞态；不能视为图数据库验收。

`scripts/validate_business_workbench.py` 已完成真实 HTTP/Edge、DeepSeek、OpenSPG 和 Neo4j 的 8 项检查。只新建合成验收资料；真实指南用于只读事项展示。报告和截图在 `test-results/business-workbench/`，无业务接口 Mock。首次运行会追加合成资料、审核发布及反馈处理记录，重复运行复用已记录版本。

后续目录连接器、身份参与检索、质检处置与有界推理已落地；历史接续笔记见 [archive/business-evolution.md](archive/business-evolution.md)。表格/VLM 解析对比、组织 SSO、持久索引和并发任务仍待实施。当前“正式资料”仅指非合成标记，不代表政策现行有效。

## 界面文案与布局约定

- 每页保留主标题与操作，去掉重复路径、副标题、装饰编号和功能宣传；用导航分隔、筛选栏、区块间距表达结构。
- 问答的查询范围默认收起，摘要展示选定范围；未填写时自动识别，日期默认今天。展开后可修改，新对话清空范围。
- 事项卡片并列显示地区与来源数量，分组规则按需展开。导入页保留文件限制、OCR 纠错提示与服务状态，移除重复流程介绍。
- 原文来源、审核状态、合成资料标识、错误、条件冲突与时效风险不作为冗余文案移除。辅助答复仍明确不作资格裁定。
- 复用现有原生组件、色板和同源接口，不引入前端依赖；浏览器交互脚本需先展开查询范围再填写字段。
