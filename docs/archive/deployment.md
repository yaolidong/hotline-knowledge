# 本地与服务部署（历史联调笔记）

> **安装主线已迁移至 [deploy/README.md](../../deploy/README.md)。**  
> 本文仅保留本次环境已验证边界与联调记录，命令可能过时；新环境请勿以本文逐步安装。

## 1. 本次环境与可运行范围

本地已有 `.venv`、固定上游源码和被忽略的 `.env`。经用户明确授权，已在 WSL Ubuntu 24.04 安装 Docker Engine 29.9.2 / Compose 5.7.0，启动 OpenSPG、Neo4j、MySQL、MinIO、CPU 嵌入服务。Windows 应用使用 DeepSeek，真实 P0 和浏览器全流程已通过。

验证部署采用 Windows Python 应用 + WSL 容器依赖。应用端口 8080、OpenSPG 8887、Neo4j 7474/7687 仅监听本机；MySQL/MinIO 不映射宿主机端口；嵌入映射 `127.0.0.1:8090`。完整应用 Linux 镜像仍未与路径 A 同等深度验证。

当前专用测试项目：命名空间 `HotlineTest`，项目 ID `1`，Neo4j 数据库 `hotlinetest`。

## 2. 与现行手册的关系

| 主题 | 现行位置 |
| --- | --- |
| 从零逐步安装 | [deploy/README.md](../../deploy/README.md) |
| OCR / 原件审阅 | [../ocr-and-workbench.md](../ocr-and-workbench.md) |
| 语义 / 嵌入联调历史 | [semantic-upgrade.md](semantic-upgrade.md) |
| 验证边界 | [../verification.md](../verification.md) |

历史安装命令（fetch_upstream / uv / init / compose / create_local_project / schema）已并入 deploy 手册并经 2026-11-11 实测修订（含 `$LinuxRoot` 路径拼接、WSL 保活、嵌入 `/health`、审核须确认质检项等）。
