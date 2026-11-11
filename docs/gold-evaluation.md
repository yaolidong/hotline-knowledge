# 金标准评测（v0）

## 规则

- 仅 `annotation_status=HUMAN_REVIEWED` 且 `schema_version=2` 的包可计分。
- 未复核包指标必须为 `null`，禁止报告 Precision/Recall/F1。
- 调参集 `split=train` 与验收集 `split=holdout` 按**文档**拆分，不得同源泄漏。
- 合成资料一律 `synthetic=true`，不得当作现行政策。

## 种子集

见 [`examples/gold/manifest.json`](../examples/gold/manifest.json)：8 包、40 问。

## 命令

```powershell
& ".venv\Scripts\python.exe" scripts\evaluate_extraction.py <actual.json> examples\gold\syn-jia-guide\pack.json --output test-results\gold\ext.json
& ".venv\Scripts\python.exe" scripts\evaluate_qa.py <answers.json> examples\gold\syn-jia-guide\pack.json --output test-results\gold\qa.json
& ".venv\Scripts\python.exe" scripts\prepare_evaluation.py <revision_id> --output data\gold\pending.json --split train
```

真实金标准本地目录建议 `data/gold/`（已在 `data/` 忽略规则内）。CI 只跑合成子集；完整 live 金标准需 `HOTLINE_RUN_GOLD=1`。
