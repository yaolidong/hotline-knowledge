# 脚本说明

安装与日常运维以 [deploy/README.md](../deploy/README.md) 为准。本目录脚本按用途分组；**历史验收脚本默认不删除**，但新环境不必全部执行。

## 部署必需 / 常用

| 脚本 | 用途 |
| --- | --- |
| `fetch_upstream.py` | 按锁文件拉取 `vendor/KAG`、`vendor/openspg` |
| `prepare_local_deploy.py` | 生成 `deploy/images.env`（已存在则拒绝覆盖） |
| `download_embedding_wheels.py` | 嵌入镜像离线 wheel |
| `create_local_project.py` | 创建/复用 `HotlineTest*` 项目 |
| `configure_local_embedding.py` | 写入应用侧 `HOTLINE_EMBEDDING_*` |
| `prepare_local_llm.py` / `configure_local_llm.py` / `check_local_llm.py` | 可选本地 Ollama/vLLM |
| `backup_hotline_data.py` / `restore_hotline_data.py` | 应用数据目录备份与隔离恢复 |
| `deploy_smoke.py` | 合成资料导入→抽取→审核发布→查询冒烟 |
| `install_docker_wsl.sh` | WSL 安装 Docker（须明确授权） |

## 评测与质量门禁

| 脚本 | 用途 |
| --- | --- |
| `prepare_evaluation.py` / `evaluate_extraction.py` / `evaluate_qa.py` | 金标准准备与评分 |
| `run_gold_smoke.py` / `run_gold_live.py` | 合成冒烟 / live 金标准（需环境变量） |
| `validate_local_llm_extraction.py` | 本地模型实抽（`HOTLINE_RUN_LOCAL_LLM=1`） |

## 历史联调 / 专项验收（按需）

`validate_*`、`browser_*`、`run_food_guide_regression.py`、`compare_parsers.py`、`retry_complex_extraction.py`、`download_public_policies.py`、`create_ocr_fixtures.py`、`generate_synthetic_documents.py` 等用于既往真实服务回归。说明见 `docs/archive/` 与 `docs/verification.md`。新部署不要求逐个重跑。
