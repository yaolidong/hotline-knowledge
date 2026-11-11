# 本地 OCR 与原件审阅工作台

更新：2026-11-04。以下功能已在 Windows 本地应用、实际 OpenSPG/Neo4j 和 DeepSeek 上验证；没有更换上游架构，也没有数据库迁移。

## 使用方式

1. **数据导入**：选择文件或粘贴文本。文件支持 PDF、DOCX、TXT、PNG、JPEG、WebP。自动模式优先读取 PDF 文字层；空文字页，或含图片且文字不足 80 字的页面使用 OCR。已有错误文字层、扫描页只抽出页眉时，可选择“强制 OCR”。
2. **抽取结果**：按标题、文件名或地区搜索，按状态筛选。处理中显示实际页数或文本块进度；点“查看”进入文档工作区，失败任务同样可以阅读已上传的原件。
3. **审核与维护**：桌面显示原件和知识结果两栏，窄屏改为上下排列。点击实体或关系的“定位原文”，阅读器跳至证据页并标出匹配 OCR 行。核对后填写说明，分别执行审核和发布。原始抽取、修正和发布均有历史记录。
4. **客服查询 / 知识图谱**：引用、节点和关系可打开原件。客服角色只可读取已发布来源。PDF/扫描图支持上一页、下一页、页码跳转、缩放、拖动、适宽、旋转；图谱已有的交互能力保留。

阅读器中的“识别 / 解析文字”是可选择、可复制的正文。PDF 原件由 PDFium 实际渲染，**不是用 OCR 文字重绘的假页面**；当前没有 PDF 文字层选区、书签、批注或打印排版功能。DOCX 展示解析正文，原始 Word 排版须下载文件查看。

## 模块与复用

| 模块 | 职责 |
|---|---|
| `hotline/documents.py` | 图片签名/帧数/像素校验，EXIF 方向处理，PDF 页面渲染；全局锁保护非线程安全的 PDFium |
| `hotline/ocr.py` | RapidOCR 本地 CPU 适配器，延迟加载、推理并发锁、低置信行标记；仅接收内存图片 |
| `hotline/parsing.py` | 原生文字与 OCR 自动路由，复用现有条款切分和 Chunk 证据契约 |
| `hotline/workflow.py` | 复用持久化任务、模型抽取、校验、审核和图发布流程 |
| `hotline/static/source-viewer.js` | PDF/图片共用阅读组件，鉴权获取实际页面，OCR 框与证据定位 |
| `hotline/static/index.html` / `style.css` / `app.js` | 五个工作区的布局、状态、角色导航及真实数据交互 |

OCR 结果保存在 SQLite 已有 `parsed_metadata` JSON 内，无需新增表、清库或改动 OpenSPG Schema。新知识继续使用原有 KAG `SubGraph` / OpenSPG 写入适配器。默认 OCR 策略保持历史文件指纹算法；显式 force/off 作为不同处理元数据保留。

页面采用服务端鉴权请求与内存 Blob URL，不将令牌放入 URL、浏览器持久存储或 PDF 外链。每个页面请求都验证来源权限，客服不能绕过发布状态访问草稿。服务端不接受任意文件路径，也不执行 PDF 中的脚本或跳转。

## 本地依赖与配置

本次固定并实测：RapidOCR 3.10.0、ONNX Runtime 1.23.2、pypdfium2 5.14.0、OpenCV headless 4.12.0.88。选择 CPU 方案，复用 Python 服务和本地原文件，不增设 OCR 微服务或 GPU 前置条件。[RapidOCR 官方安装说明](https://rapidai.github.io/RapidOCRDocs/main/en/install_usage/rapidocr/install/)、[PDFium 线程安全说明](https://pypdfium2.readthedocs.io/en/stable/python_api.html)。

按 `requirements.lock` 安装即可；该 RapidOCR 版本的 wheel 包含以下模型，运行时不会因扫描文件而自动下载模型：

```text
PP-OCRv6_det_small.onnx
PP-OCRv6_rec_small.onnx
ch_ppocr_mobile_v2.0_cls_mobile.onnx
```

OCR 在本地执行。后续知识抽取/QA 仍使用原有模型配置；当前为经授权的 DeepSeek，识别出的正文会按该配置发送给模型。内网部署应将 LLM 地址配置为本地兼容服务。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| HOTLINE_OCR_ENABLED | true | 关闭后扫描件明确报错 |
| HOTLINE_OCR_MODEL_DIR | 未设置 | 默认使用安装包内模型；自定义目录须包含以上三个文件 |
| HOTLINE_OCR_MAX_PAGES | 50 | 单文件最多进行 OCR 的页面数，超过则任务失败 |
| HOTLINE_OCR_RENDER_SIDE | 2200 | OCR 渲染最大边长；允许 1000–3000 |
| HOTLINE_OCR_THREADS | 2 | CPU 推理线程数 |
| HOTLINE_OCR_REVIEW_THRESHOLD | 0.85 | 低置信行审阅提示阈值；不会删除这些行 |

PDF 总页数上限 500，图片上限 2400 万像素，多帧图片拒绝导入。原件预览单页按需渲染，最大边长 3000，不一次加载整份 PDF。OCR 模型缺失时给出明确错误，不静默调用云端 OCR。Linux 完整应用镜像尚未在本次构建验证，保留原部署文档的限制。

## 实际验收

测试资料全部明确标记“合成测试资料，不是真实政策，不得用于实际办理”。`scripts/create_ocr_fixtures.py` 生成双页扫描 PDF、文字/扫描混合 PDF 及 PNG/JPEG/WebP；这些不是从官网下载的政府原件。

| 验证 | 实际结果 |
|---|---|
| 中文 PNG 本地 OCR | 识别部门、材料、年龄/地区条件、流程、渠道和时限，生成 8 个条款块 |
| 双页扫描 PDF 本地 OCR | 两页均走 OCR，11 个文本块；页码、全文偏移、行框保留 |
| 混合 PDF | 第一页走原生文字，第二页 OCR；未把第一页面强制改为 OCR |
| PDF + DeepSeek 抽取 | 原始 11 个实体、7 条业务关系，停留 DRAFT |
| 对照原件修正 | 合并共享明确事项证据的互补属性，修正重复否定与重复条件；得到 7 个实体、6 条业务关系；原始快照保留 |
| 浏览器审核发布 | 实际调用审核/发布接口，写入 OpenSPG 并回读 Neo4j；在 HotlineTest 项目内发布 |
| 图谱 + DeepSeek QA | 事项过滤视图返回 10 节点、9 边；材料、正向条件、排除条件分别引用真实扫描页；点击引用可打开原件并高亮识别行 |
| 图片导入完整任务 | PNG → 本地 OCR → DeepSeek → 8 实体、6 关系；仍为 DRAFT，没有自动发布 |
| 原件阅读 | 翻页、按钮缩放、拖动、旋转、OCR 框、正文切换、引用定位；下载文件与上传文件逐字节一致 |
| 浏览器与界面 | 实际 Edge，1440 / 820 / 390 宽度，无页面横向溢出；完整流程无脚本/控制台错误；关键 11 组对比度通过 |

证据目录（不入 Git）：`test-results/ocr/` 保存原始扫描件、解析报告、原始/修正抽取和图片任务；`test-results/ocr-ui/` 保存实际浏览器截图、交互报告和 QA 引文。

复验：

```powershell
& ".venv/Scripts/python.exe" -X utf8 -m pytest "tests/test_ocr.py" -q
& ".venv/Scripts/python.exe" -X utf8 "scripts/create_ocr_fixtures.py"
# 已有本次验收资料和运行服务时，读取真实数据重放浏览器检查：
& ".venv/Scripts/python.exe" -X utf8 "scripts/validate_ocr_ui.py"
```

`validate_ocr_ui.py --publish-reviewed` 只允许独立 HotlineTest 项目，且必须已有本次对照修正的审计记录。它不是面向任意上传资料的自动审批脚本。脚本不包含访问令牌，凭据只从本地配置读取。

## 仍需进一步验证

- 本机访问政府网站原始 PDF 仍发生连接错误；本次重试清城区、洛阳市和武汉市的官方附件均未下载成功，记录在 `download-attempts.json`。之前已导入的真实政府正文仍是 TXT；不能将其算作政府扫描原件 OCR 验收。
- OCR 行序不等于复杂版面理解；多栏、合并单元格、印章遮挡、倾斜页面、手写和低清晰度文件需更多真实样本。尚未接入 MinerU 的复杂表格复原；90° 整页自动校正也未实现，阅读器旋转仅影响查看。
- 模型置信度不代表正确率。没有完整标注集，不报告泛化准确率或虚构 F1。低置信行保留并提示核对，OCR 后仍可能有漏字、数字或否定词错误。
- 本次实际发现了 LLM 重复事项和否定逻辑问题，已对这一合成验收版本进行人工校正；未因此宣称自动融合或条件逻辑问题普遍解决。
- 阅读器和版面设计已可用；权限仍为本地角色令牌，复杂知识编辑保留高级 JSON 修正入口，生产 SSO、负载限制与运维能力按原计划继续推进。
