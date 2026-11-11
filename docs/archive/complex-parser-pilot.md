# 复杂文档解析试点

默认管道：`parsing` + `pdf_tables` + RapidOCR。  
可选适配器：`hotline.parsers.mineru_adapter`（未安装则 `available()==False`，不得伪造解析结果）。

对比：

```powershell
& ".venv\Scripts\python.exe" scripts\compare_parsers.py
```

用金标准 `material_condition_coverage` 衡量材料/条件完整率；相对阶段 0 基线无提升则不默认启用 MinerU，并在验证记录中如实记载。
