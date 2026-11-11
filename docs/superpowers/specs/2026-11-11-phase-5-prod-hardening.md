# 阶段 5：准生产硬化

日期：2026-11-11  
状态：已实现（代码与离线测试）；应用容器与本地 LLM 实抽需环境显式验收

## 交付

| 项 | 实现 |
|---|---|
| ACL | 既有四角色 + production/test 隔离；客服仅 `PUBLISHED` 原件；维护可见草稿；反馈列表维护角色；`tests/test_prod_hardening.py` + `test_business_operations.py` |
| 限流 | `hotline/rate_limit.py` 进程内令牌桶；`HOTLINE_RATE_LIMIT_*`；中间件对 query/qa/import/feedback POST 返回 429 |
| 备份恢复 | `scripts/backup_hotline_data.py`、`scripts/restore_hotline_data.py`；deploy README 文档化 Neo4j/MySQL 卷快照与隔离 `doctor` |
| 应用容器 | `deploy/Dockerfile` + compose `hotline`；WSL 上 `docker compose … config --services` 已确认含 `hotline` 且与依赖同项目；完整镜像构建/联通按 README §8 在有 Docker 引擎时执行 |
| 监控 | `/health` 依赖探测摘要；`/api/status` 限流与本地 LLM 探测（仅本机预设 URL）；`GET /api/operations/audit-export` |
| 本地 LLM 实抽 | `HOTLINE_RUN_LOCAL_LLM=1 python scripts/validate_local_llm_extraction.py` → `test-results/local-llm/`；F1 不足标「仅接口可用」；未设环境变量时脚本 skip |

## 明确未做

SSO/组织目录、分布式限流、自动资格裁定、把「计划」写成「已全环境生产验证」。

## 验收命令

```powershell
& ".venv/Scripts/python.exe" -m pytest tests/test_prod_hardening.py -q
& ".venv/Scripts/python.exe" scripts/backup_hotline_data.py --output-root data/backups
& ".venv/Scripts/python.exe" scripts/restore_hotline_data.py --backup data/backups/<dir> --target data/restore-drill
# 可选：HOTLINE_RUN_LOCAL_LLM=1 实抽
# WSL：docker compose --project-name hotline-dev --env-file deploy/images.env \
#   -f vendor/openspg/dev/release/docker-compose.yml -f deploy/compose.override.yml up -d --build hotline
```

离线回归：`tests/test_prod_hardening.py` 全绿；备份→隔离恢复冒烟通过；`validate_local_llm_extraction` 默认 skip。
