/* 只展示后端校验过的事实路径与三值表达式，不在客户端生成政策结论。 */
async function openReasoning(item, mode) {
  const dialog = el("dialog", undefined, "change-dialog"), heading = el("div", undefined, "section-heading"), body = el("div");
  heading.append(el("h2", mode === "paths" ? "政策依据路径" : "明确条件辅助核对"), button("关闭", () => dialog.close()));
  dialog.append(heading, el("p", `${item.name} · ${item.region}${item.service_code ? ` · ${item.service_code}` : ""}`), body);
  dialog.addEventListener("close", () => dialog.remove()); document.body.append(dialog); dialog.showModal();
  const scope = {question: "查看政策依据与办理条件", service: item.name, region: item.region, service_code: item.service_code || "", space: knowledgeSpace, as_of: item.as_of || ""};
  body.append(el("p", "正在检查已发布图谱和证据…"));
  try {
    const data = await api(`/api/reasoning/${mode}`, {method: "POST", body: JSON.stringify(scope)});
    if (!dialog.open) return;
    body.replaceChildren(el("p", data.message, "notice"));
    for (const warning of data.query?.warnings || []) body.append(el("p", warning, "muted"));
    if (mode === "paths") {
      if (!data.paths.length) body.append(el("p", "当前范围没有带可验证原文的依据路径。"));
      for (const [index, path] of data.paths.entries()) {
        const box = el("details"); box.open = index === 0; box.append(el("summary", `路径 ${index + 1} · ${path.length} 跳 · ${path.map(step => step.source).concat(path.at(-1).target).join(" → ")}`));
        for (const step of path) box.append(el("p", `${step.source} → ${relationLabel(step.relation)} → ${step.target}`), evidenceView(step.evidence, step.record_id));
        body.append(box);
      }
      if (data.has_more) body.append(el("p", "路径达到查询上限；当前结果不代表全部政策依据。", "notice"));
      return;
    }
    if (data.status !== "CHECKED") return;
    const form = el("form"), inputs = new Map(), results = el("div");
    form.append(el("p", "仅根据已确认的信息选择。没有信息时保留“尚不清楚”；不要凭经验补全申请人情况。"));
    for (const leaf of data.leaves) {
      const label = el("label", leaf), input = el("select");
      for (const [value, title] of [["UNKNOWN", "尚不清楚"], ["TRUE", "已确认这一命题成立"], ["FALSE", "已确认这一命题不成立"]]) input.append(new Option(title, value));
      inputs.set(leaf, input); label.append(input); form.append(label);
    }
    const submit = el("button", "核对已知条件"); submit.type = "submit"; form.append(submit);
    form.addEventListener("submit", guarded(async () => {
      submit.disabled = true;
      try {
        const current = await api("/api/reasoning/conditions", {method: "POST", body: JSON.stringify({...scope, rule_set_hash: data.rule_set_hash, observations: Object.fromEntries([...inputs].map(([leaf, input]) => [leaf, input.value]))})});
        if (!dialog.open) return;
        renderChecks(current);
      } finally {submit.disabled = false;}
    }));
    body.append(form, results); renderChecks(data);
    function renderChecks(value) {
      results.replaceChildren(el("p", value.message, "notice"));
      for (const rule of value.rules || []) {
        const box = el("details"); box.open = true;
        box.append(el("summary", `${rule.value} · ${truthLabel(rule.evaluation.value)}`), evaluationTree(rule.evaluation), evidenceView(rule.evidence, rule.record_id));
        results.append(box);
      }
    }
  } catch (error) {body.replaceChildren(el("p", error.message, "error"));}
}
function truthLabel(value) {return {TRUE: "该表达式成立", FALSE: "该表达式不成立", UNKNOWN: "信息不足"}[value];}
function evaluationTree(node) {
  const root = el("div", undefined, "rule-summary");
  root.append(el("p", `${node.condition || {AND: "同时满足", OR: "任一满足", NOT: "不属于 / 排除"}[node.operator]}：${truthLabel(node.value)}`));
  for (const child of node.conditions || []) root.append(evaluationTree(child));
  if (node.basis) root.append(el("p", `逻辑原文：${node.basis}`, "muted small"));
  return root;
}
