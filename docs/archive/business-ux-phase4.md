# 阶段4业务体验

已具备（本仓库现有实现，本阶段补齐验收门禁）：

- 事项工作台、表单质检 `issue_resolutions`（verified / false_positive / unresolved）
- 客服「复制接线话术」「复制工单记录」与结构化反馈（`/api/feedback`，含 `trace_id`）
- 维护端「检索诊断」`/api/retrieval/diagnose`（`maintain` 角色）；客服 403
- 客服无结果时仅见合规提示，不含过滤明细

条件树：审核区以 `rule` JSON/摘要展示 AND/OR/NOT；完整可视化编辑器可继续迭代，不阻塞准生产。
