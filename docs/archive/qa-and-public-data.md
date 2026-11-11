# 图谱交互、真实资料与证据问答验证

日期：2026-11-04（Asia/Shanghai）。本次复用现有服务、真实 OpenSPG/Neo4j 与 DeepSeek，未增加运行时依赖或修改上游源码。

## 页面操作

打开 http://127.0.0.1:8080，刷新页面后登录。

- 图谱：滚轮和按钮缩放、拖动画布平移、拖动节点调整布局、适应画布、全屏。关系文字悬停显示，点击查看属性和证据。拖动不会写库，重新检索后重置布局。
- 来源：点击节点/关系的“定位原文”或回答后的引用编号，查看高亮证据、版本、页码/章节、相邻段落、解析全文；可下载实际导入的源文件并打开来源网页。原始 PDF 输入保留解析页码，TXT 不虚构 PDF 页码。
- 客服：回答方式选择“AI 证据问答”，也可切回原有结构化查询。模型先获得已发布图事实与原文，逐句给出引用；失败、缺失、地区歧义、未确认的时效均有明确状态。

真实指南体验：地区填“辽宁省阜新市太平区”，事项填“食品经营许可证”，例如问“需要什么材料？”或“办理期限是多少，能否延长？”。这是历史资料的工程验证，不代表已核实现行政策。

## 真实资料来源与获取边界

固定来源清单在 `examples/public_policy_sources.json`，实际文件、获取记录、哈希、提取快照和结果在 `data/public-policies/`（被 Git 忽略）。

| 资料 | 官方来源 | 本次实际获取方式 |
|---|---|---|
| 食品经营许可证（餐饮）服务指南 | [阜新市太平区政府 PDF](https://www.fxtp.gov.cn/tpq/file/2022-11-25/16693641368954028e49284871fcf7400184addb3bbf6f19.pdf) | 公开 Reader 提取 PDF 正文，保存为 TXT，906 字符；不是 PDF 原件 |
| 2025 年企业牵头承担国家科技项目配套补贴通知 | [武汉市科技创新局发布页](https://kjj.wuhan.gov.cn/wmfw/tzgg/tzgg_18371/202504/t20250402_2561761.html)及[六页扫描附件](https://kjj.wuhan.gov.cn/wmfw/tzgg/tzgg_18371/202504/P020250402563361944943.pdf) | 公开 Reader 获取官网 HTML 正文，保存为 TXT，2338 字符；不是扫描件 OCR 结果 |

本机直连这些政府站点的 PDF 时发生 TLS 连接重置，原始 PDF **没有成功下载**。`--reader-text` 是显式启用的正文获取替代路径；获取报告保留原始 URL、Reader URL、文本 SHA256、下载时间和失败原因。只向 Reader 发送清单中的公开政策 URL，不发送本地上传资料、模型密钥或访问令牌。

当时扫描附件的远端文本提取结果为空，两份实际导入均为 TXT，不计入原始 PDF/OCR 验收。后续已接入本地 OCR，并使用合成扫描 PDF 完成独立验收，见 [OCR 记录](../ocr-and-workbench.md)；本节这两份政府资料的输入仍是 TXT，不能追溯成官方扫描原件。下载按钮得到该 TXT，官网 PDF 通过来源链接访问。

## 抽取效果与复核

| 输入与版本 | 实际结果 | 处理 |
|---|---|---|
| 太平区指南 v1 | 自动抽取 22 实体、21 业务关系，通过逐字证据校验 | 复核后纠正三类语义问题，保留 20 实体、19 业务关系，在 HotlineTest 项目发布用于 QA 验证 |
| 太平区指南 v2 | 实体名称不符合原文证据校验 | 保留 FAILED/DRAFT，不发布，也不覆盖 v1 |
| 武汉通知 v1 | 关系端点违反 Schema | 拒绝发布 |
| 武汉通知 v2 | 第 2 个实体 content 属性不符合原文依据校验 | 拒绝发布 |

指南 v1 的三项修正：

1. 原文“告知方式：窗口、电话”不能直接成为办理渠道，移除相应渠道节点和关系。
2. “复印件”是材料形式，改存 `format`，不能作为材料份数 `copies`。
3. 办理时限不能只保存“20 个工作日”，补全原文明示的延长条件、期限和告知义务。

修正通过正式 correction/review/publish 接口执行，记录审核说明与前后快照。工程核对不代替政府业务人员对现行效力和适用范围的审核。原始模型输出在 `70e2524c3b4f4abdbeb589939ad52593.original-extraction.json`，复核记录在 `reviewed-guide-report.json`。

这次验证说明：逐字引文存在并不等于语义正确；真实文件中的断词、异常空格和表格阅读顺序仍会影响抽取稳定性。已改进 Schema 错误反馈和字段提示，但没有放宽证据校验来掩盖失败，也没有把多次运行筛选成“全自动成功”。没有完整人工标注语料，不报告 Precision、Recall、F1。

## QA 与浏览器实际结果

- 最终全套自动化测试：**81 通过、1 跳过、2 条已知上游弃用告警**；默认跳过付费 live，但该 live 已单独真实执行通过。Ruff 检查/格式（37 个 Python 文件）和两份前端 JS 语法检查通过；73 个非忽略文件未发现本地模型密钥或访问令牌。上游工作区无改动。
- 真实资料图查询：办理部门与 4 个材料关键事实共 5 项核对通过，仅代表这些核对项。
- `scripts/validate_live_qa.py`：7 类检查通过，包括材料、部门、办理期限及延长条件、无依据补贴问题拒答、有效性待确认、指令注入拒绝、不同地区隔离；每条引用访问真实来源接口核对字符范围。
- 生成成功采用真实 `deepseek-flash`，本轮上述资料的有据回答约 0.9–2.8 秒；样本很小，不能视为吞吐/质量基准。
- 早期一轮期限回答未通过生成/引用校验，被系统拒绝；保留在 `qa-report-before-citation-retry.json`。增加有限校验反馈重试后复测 7 项通过，仍可能在后续模型调用中拒绝不合格输出。
- 发现模型可能在“仍需确认”中推测文件年份，已将该字段改为有限核实项枚举；政策事实只能放入带引用的正文，不允许通过附加说明绕过校验。
- `scripts/browser_enhancements.py --public-guide` 使用真实图数据库和 DeepSeek，没有响应替身；验证缩放、滚轮、平移、节点拖动、全屏、原文件下载、全文、高亮及 QA 引用，未出现页面脚本异常。
- 原有真实模型 → 审核 → OpenSPG 写入 → Neo4j 回读端到端测试重新执行，1 项通过。

实际浏览器截图及结果：`test-results/browser-public-policy/graph.png`、`source.png`、`qa.png`、`report.json`。合成资料的同类检查在 `test-results/browser-enhancements/`。

## 复现与限制

```powershell
& ".venv/Scripts/python.exe" -X utf8 "scripts/download_public_policies.py" --reader-text
& ".venv/Scripts/python.exe" -X utf8 "scripts/validate_public_policies.py"
# 上面只导入并抽取，不自动审核发布。核对证据并通过审核后再执行：
& ".venv/Scripts/python.exe" -X utf8 "scripts/validate_live_qa.py"
& ".venv/Scripts/python.exe" -X utf8 "scripts/browser_enhancements.py" --public-guide
```

当前 RAG 使用已发布图事实与有限原文关键词召回，不是全网政策问答，也没有声称接通通用向量检索。基础本地 OCR 已接入；真实低质量扫描件验证、复杂版面复原、政策语义完整性、跨政策冲突与有效性判断仍待加强。发布资料必须经过复核，历史文档不能因为官网可访问或发布时间较新就视为当前有效。

主要实现：`hotline/static/graph-view.js`、`hotline/static/app.js`、`hotline/qa.py`、`hotline/app.py`；领域语义提示仍集中在 `hotline/config/schema.json`。
