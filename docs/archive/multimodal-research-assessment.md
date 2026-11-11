# 多模态知识图谱研究的政务适用性评估

核查日期：2026-11-04。范围：当前源码、已有三元组核查记录、论文页面、作者仓库与官方文档。未安装或运行这些外部框架，未复现实验；本记录不表示已接入其功能，也不改变现有 Schema 或已发布数据。

后续实施说明：以下表格保留研究评估时的基线；同日已完成领域语义规范化、文档区域定位、身份审核及混合检索的首轮改造，实际代码、验证和剩余限制见 [升级记录](semantic-upgrade.md)。

## 结论与当前基线

建议保留 OpenSPG/KAG 主链，优先改进领域建模、抽取验证、文档区域溯源和受约束检索。首批多模态对象应是扫描页、表格、流程图和附件。音视频对象跟踪、通用场景图和开放式知识扩展不属于当前验收重点。

“是否用文本作为中间表示”不能单独决定是否为多模态图谱；还要看是否保留可寻址的媒体证据、区域关联和可验证的对应关系。GraphRAG 描述检索与生成方式，MMKG 描述知识表示，两者可以同时存在。图文内容相似、同属某个主题与指向同一个实体也应区分。

| 当前实现 | 已确认的能力或缺口 |
|---|---|
| `hotline/ocr.py`、`parsing.py` | 保存原文、页码、OCR 行框、识别分数和文本偏移。原生 PDF 文本提取尚未统一提供区域坐标；复杂表格的单元格结构尚未成为统一证据接口 |
| `hotline/models.py` 的 `Evidence` | 当前为 chunk_id、quote、start/end；不是独立的页面区域或表格单元格定位模型 |
| `hotline/graph.py` | KAG 子图经 OpenSPG 写入；实体通过 SUPPORTED_BY 关联 Chunk，业务关系保存证据。图谱实体 ID 包含版本，canonicalKey 不是跨文档融合结果 |
| `hotline/extraction.py`、`schema.py` | 类型、端点、逐字证据、条件树结构校验；规范名与原文提及尚未分开，校验不能证明关系蕴含或条件完整性 |
| `hotline/retrieval.py`、`qa.py` | 已发布图谱与关键词原文检索、地区/事项候选澄清、引用校验。尚未接通完整向量混合检索，也未实现通用自然语言历史日期解析 |

当前更准确的定位是“保留扫描件证据的政务语义图谱”。它已有进一步建设文档多模态证据的基础，尚不具备通用视觉对象身份对齐能力。具体语义问题见 [三元组建模核查](triple-model-audit.md)，不能把功能回归通过数当作抽取准确率。

## 外部资料核查与取舍

以下能力描述来自相应一手来源；“适用建议”是本项目的工程判断，不是论文在政务语料上的已验证结论。

| 工作 | 核查结果 | 适用建议 |
|---|---|---|
| [SciMKG](https://github.com/kg-bnu/SciMKG) | 作者仓库列出 AAAI 2026、四模态、EVIA、1,356 知识点、34,630 概念和 403,400 三元组。也明确包含外部概念扩展与生成音频；用户材料的 MDKAG 图片标题不是该论文题名。AAAI 页面本次受访问保护 | 参考抽取、验证、整合的阶段划分；生成内容及通用外部知识不能变成政策依据，多模型一致也不是事实真值 |
| [MMGraphRAG](https://github.com/wanxueyao/MMGraphRAG) | README 描述 YOLO/MLLM 场景图、文本图、谱聚类候选融合和 Web 浏览 | 参考图文关联与候选匹配。政务材料优先需要版面/表格结构，不能用视觉相似度直接合并部门或事项 |
| [MegaRAG](https://aclanthology.org/2026.acl-long.2218/) | ACL Anthology 确认 ACL 2026；作者仓库提供图谱与页面检索流程。其 [LICENSE](https://github.com/AI-Application-and-Integration-Lab/MegaRAG/blob/master/LICENSE) 第 3.3 节限定研究或评估用途 | 可参考文档级上下文和页级检索思路；不能将代码可见直接理解为可投入本系统生产部署 |
| [VaLiK](https://openaccess.thecvf.com/content/ICCV2025/html/Liu_Aligning_Vision_to_Language_Annotation-Free_Multimodal_Knowledge_Graph_Construction_for_ICCV_2025_paper.html) | ICCV 官方条目确认。通过视觉描述及跨模态相似性验证构图；[仓库](https://github.com/Wings-Of-Disaster/VaLiK) 说明描述块 ID 与图片关联 | 可用于发现图文解释不一致，作为复核线索；相似性分数不能证明办理条件、有效期或机构身份 |
| [MG²-RAG](https://arxiv.org/abs/2604.04969) | arXiv 确认实体驱动视觉定位、多粒度检索及论文所报速度/成本；[ECCV 官方名单](https://eccv.ecva.net/Conferences/2026/AcceptedPapers) 列有该题名。作者 [README](https://github.com/Daboolu/MG2-RAG) 的运行配置包含 EVA-CLIP-8B、SAM3、英语 spaCy 和 GPU | 优先借鉴“事实能定位到具体区域”的表示方法。不能直接把自然图像定位效果或 43.3 倍/23.9 倍实验指标迁移为中文政务文档收益 |
| [M³KG-RAG](https://kuai-lab.github.io/cvpr2026m3kgrag/) | 作者项目页和 [CVPR 论文](https://openaccess.thecvf.com/content/CVPR2026/papers/Park_M3KG-RAG_Multi-hop_Multimodal_Knowledge_Graph-enhanced_Retrieval-Augmented_Generation_CVPR_2026_paper.pdf) 确认上下文三元组、知识定位、描述修正与 GRASP 检索剪枝 | 可借鉴证据相关性检查；剪枝须保护同一条款的否定、例外、备选材料和表格表头。无需照搬音视频或多智能体架构 |
| [MAC-MKG](https://github.com/QuXiaolong0812/MAC-MKG) | README 列出九类 Agent，明确属于初步版本，论文链接仍为待上传占位 | 作为研究原型参考，不作为现阶段运行底座；未验证其端到端可运行性 |
| [DataFlow-KG](https://zhp-li197.github.io/DataFlow-KG-Doc/zh/kg_guide/multimodal_kg_pipeline/) | 文档给出候选实体、文本关系、depicted_in 视觉关系、子图采样与 QA 生成算子 | 借鉴分阶段输入/输出和评测数据组织，复用现有 KAG 算子边界；depicted_in 不等同于对象级身份对齐。生成 QA 需人工核定后才能作为标准答案 |
| [LightRAG](https://github.com/HKUDS/LightRAG) / [RAG-Anything](https://github.com/HKUDS/RAG-Anything) | LightRAG 当前 README 确实记录 2026.05 合并多模态能力、MinerU/Docling、不同模型职责及语义切分；RAG-Anything 仓库仍可访问 | 重点参考文档解析交换格式与上下文切分；暂不替换已有 OpenSPG/KAG 与审核发布体系。现有依赖锁定版本不会自动获得这些上游功能 |
| [Neo4j LLM KG Builder](https://neo4j.com/labs/genai-ecosystem/llm-graph-builder/) | 官方页面确认文档块图与实体图、可配置 Schema；指出长英文文本效果较好，表格/图像/幻灯片较弱 | 可作为隔离的建模和展示对照；当前系统已有真实 Neo4j 图，无需为界面再次搭建建图底座 |
| [EvoGraph-R1](https://evograph-r1.github.io/) | 项目页与 [CVPR 论文](https://openaccess.thecvf.com/content/CVPR2026/papers/Lin_EvoGraph-R1_Self-Evolving_Multimodal_Knowledge_Hypergraphs_for_Agentic_Retrieval_CVPR_2026_paper.pdf) 描述检索、外部搜索、图增删改与答案的闭环 | 可借鉴缺口发现及修订建议；客服查询只能产生待审核修订，不能直接改写已发布政策 |

## 建议的实施顺序（尚未实施）

### 1. 先修正实体、属性和规则表达

- 把原文提及、规范名称、展示名称分开。证据必须逐字保留；规范化采用有版本的转换规则和审核记录，不能靠放宽原文校验允许模型自由改写。
- 区分办理事项与证照名称、申请主体与资格条件。材料备选组保留 OR、形式与数量约束；流程次序需要明确原文或版式依据。
- 对事实检查类型、关系端点、原文蕴含、限定条件和上下文覆盖。模型复核作为辅助，不能自行授予 PUBLISHED 状态。
- 首个验收对象继续使用已核查的食品经营许可指南，先人工确认标准事实，再在独立草稿版本比较抽取结果。

主要修改入口：`hotline/models.py`、`config/schema.json`、`schema.py`、`extraction.py`。先确定兼容字段及迁移方案，不能原地覆盖历史图。

### 2. 把证据定位扩展到文档区域

在现有 Evidence 和 OCR 数据之上，设计可选的文档版本、页码、块 ID、区域坐标、表格行列、原文范围与解析版本字段。原生 PDF、OCR 和表格解析统一输出定位信息；无法定位时显式保留未知，不能编造框。

拟议证据路径：业务事实 → 引文范围 → 页面区域/表格单元格 → 原文件版本。每一项是来源关联，不是自动新建一个业务实体。界面点击关系或答案即可定位区域，同时显示必要的表头、脚注及关联条件。

优先扩展 `parsing.py`、`ocr.py`、`models.py` 和 `static/source-viewer.js`；先复用已有扫描阅读器，再按标注样本的错误类型评估是否引入 MinerU/Docling/VLM。文本 LLM 接入不能视为已经具备图像输入能力。

### 3. 建立带范围的跨文档实体关联

保留“文档中的提及”和“统一业务实体”的不同身份，事实继续携带地区、业务有效时间、记录版本及各自证据。以部门类型、行政层级、事项编码、明示别名和历史沿革等产生候选；不确定项进入审核，不以字符串或向量相似度直接合并。

同一实体关联多个来源，不代表不同版本中的条件可以混成一套现行规则。应先提供可审查、可撤销的关联建议，再考虑自动化高可信匹配。

### 4. 改进检索与回答的证据组织

在当前候选澄清之上增加受约束的问题解析：自动识别地区、事项和日期，验证候选存在；只有歧义或缺失影响答案时追问。图谱与关键词/向量召回均须按权限、发布状态、地区和时间处理，排序后补齐条件组、排除条款及表格上下文，再交给模型表达。

相关性排序不能丢弃必要条件。向量或 PageRank 得分只表示检索相关性，不能替代证据真实性、时效和审核状态。外部知识补充及模型提出的图修订只进入草稿。

主要入口：`retrieval.py`、`query.py`、`qa.py`；继续使用受限参数化图查询，复用 KAG 检索能力时先落实发布状态过滤。

## 验证方式

建议固定一小批可人工核对的真实公开指南、通知和政策文件，覆盖原生 PDF、扫描 PDF、DOCX、表格、同名跨地区、修订引用及信息缺失；另保留明确标记的合成边界测试。现有官网正文验证和合成扫描验证不能代替真实扫描公文测试。

按阶段比较“现有系统 → 领域建模修正 → 区域证据/版面解析 → 可选 VLM/重排”，固定文件、问题、模型配置和发布范围，分别记录：

- 实体类型与关系 Precision/Recall/F1；规范名与原文提及分开计分，不能仅按节点总数评价。
- 条件/材料备选与排除语义是否完整，流程顺序是否有依据，跨地区误合并数。
- 事实与证据对应正确率、页码/单元格/区域定位正确率；OCR 字符错误率与政策事实错误分别统计。
- 问答证据召回、引用支持、地区/时间适用、无依据回答、应澄清却直接回答的错误。
- 每份文档和每次问答的耗时、模型调用成本及人工审核量。

当前没有这批人工标注和外部框架复现结果，不提供准确率或预期提升百分比。同一份材料的 OCR、VLM 描述及模型复核不是独立政策来源；红头、印章及视觉一致性也不能单独证明文件真实或现行有效。
