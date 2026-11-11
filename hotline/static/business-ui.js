"use strict";

let serviceOffset = 0, feedbackOffset = 0;
const feedbackLabels = {wrong_evidence: "依据不符", irrelevant: "答非所问", missing_requirement: "缺少材料或例外", outdated: "政策可能过期", helpful: "有帮助"};
const feedbackStates = {OPEN: "待核对", RESOLVED: "已处理", DISMISSED: "说明后关闭"};
const diagnosticReasons = {OTHER_SPACE: "其他资料空间", NOT_PUBLISHED: "尚未发布", REGION_MISMATCH: "地区不匹配", OTHER_SERVICE_SOURCE: "不在选定事项来源中", EXPIRED: "已到明示失效日期", NOT_YET_EFFECTIVE: "尚未施行"};

async function loadOperations() {
  const data = await api(`/api/operations/summary?days=${$("operations-filter").elements.days.value}`), root = clear("operations-summary");
  root.append(el("p", data.message, "notice"));
  const states = el("div", undefined, "service-grid");
  for (const row of data.revision_states) {
    const card = el("div", undefined, "card"); card.append(el("h2", `${row.count} 个版本`), el("p", `${stateLabels[row.task_status]} · ${stateLabels[row.knowledge_status]}`), button("查看资料", async () => {$("results-filter").elements.state.value = row.task_status === "READY" ? row.knowledge_status : row.task_status; resultOffset = 0; await tab("results");})); states.append(card);
  }
  root.append(states, el("h2", `已记录请求 · ${data.traces_sampled} 条${data.truncated ? "（达到采样上限）" : ""}`));
  const table = el("table"), header = el("tr");
  for (const title of ["查询方式", "样本数", "p50", "p95", "结果状态"]) header.append(el("th", title)); table.append(header);
  const resultLabels = {ANSWERED: "有依据答复", NO_EVIDENCE: "无足够依据", NEEDS_CLARIFICATION: "需确认范围", CONFLICT: "来源差异", VALIDITY_UNCONFIRMED: "时效待核实"};
  for (const item of data.performance) {const row = el("tr"); for (const text of [item.mode === "qa" ? "AI 证据问答" : "结构化查询", item.samples, item.p50_ms == null ? "暂无样本" : `${item.p50_ms} ms`, item.p95_ms == null ? "暂无样本" : `${item.p95_ms} ms`, Object.entries(item.statuses).map(([status, count]) => `${resultLabels[status] || status} ${count}`).join("；") || "暂无样本"]) row.append(el("td", String(text))); table.append(row);}
  const wrapper = el("div", undefined, "scroll-table"); wrapper.append(table); root.append(wrapper, button("处理答复反馈", () => tab("feedback")));
}
$("operations-filter").addEventListener("submit", guarded(loadOperations));

async function switchKnowledgeSpace(space) {
  knowledgeSpace = space;
  $("knowledge-space").value = space;
  $("test-space-banner").hidden = knowledgeSpace !== "test";
  $("chat-new").click();
  document.querySelectorAll("dialog[open]").forEach(dialog => dialog.close());
  for (const reader of readers.values()) reader.destroy();
  readers.clear();
  for (const name of ["results", "review"]) {recordRequests.set(name, (recordRequests.get(name) || 0) + 1); closeRecord(name); clear(`${name}-list`);}
  for (const id of ["service-detail", "services-list", "diagnostic-results", "feedback-list", "graph-canvas", "graph-detail"]) if ($(id)) clear(id);
  graphView?.destroy(); graphView = null;
  serviceOffset = feedbackOffset = resultOffset = reviewOffset = graphOffset = 0;
  graphFocus = "";
  $("graph-form").elements.revision_id.value = "";
  $("graph-record-scope").hidden = true;
  $("import-form").elements.synthetic.checked = knowledgeSpace === "test";
  await tab(active);
}
$("knowledge-space").addEventListener("change", guarded(() => switchKnowledgeSpace($("knowledge-space").value)));

async function loadServices() {
  const params = new URLSearchParams(formObject($("services-filter")));
  params.set("limit", "12"); params.set("offset", serviceOffset);
  const data = await api(`/api/services?${params}`);
  const root = clear("services-list");
  const summary = clear("services-summary");
  summary.append(el("span", `${data.total} 个事项组`));
  const grouping = el("details", undefined, "inline-help");
  grouping.append(el("summary", "分组规则"), el("p", data.grouping)); summary.append(grouping);
  if (data.truncated) summary.append(el("span", "图候选达到上限，请缩小数据范围", "notice"));
  $("services-prev").disabled = serviceOffset === 0;
  $("services-next").disabled = serviceOffset + data.items.length >= data.total;
  if (!data.items.length) root.append(el("p", "当前范围暂无已发布事项。请检查资料空间、地区或发布状态。", "empty-state"));
  for (const item of data.items) {
    const card = el("article", undefined, "card service-card");
    const meta = el("div", undefined, "service-meta");
    meta.append(el("span", item.region || "地区待核对", "badge"), el("span", `${item.sources.length} 个来源`, "muted small"));
    card.append(el("h2", item.name), meta);
    if (item.service_code) card.append(el("p", `实施事项编码：${item.service_code}`));
    const actions = el("div", undefined, "service-actions");
    actions.append(button("查看办理信息", () => openService(item), ""), button("在对话中查询", () => queryService(item)));
    card.append(actions);
    root.append(card);
  }
}
$("services-filter").addEventListener("submit", guarded(async () => {serviceOffset = 0; clear("service-detail"); await loadServices();}));
$("services-prev").addEventListener("click", guarded(async () => {serviceOffset = Math.max(0, serviceOffset - 12); await loadServices();}));
$("services-next").addEventListener("click", guarded(async () => {serviceOffset += 12; await loadServices();}));
async function queryService(item) {
  await tab("query");
  chatForm.elements.service.value = item.name;
  chatForm.elements.region.value = item.region;
  chatForm.elements.service_code.value = item.service_code || "";
  updateChatScope();
  $("chat-question").value = `${item.name}需要什么材料？`;
  $("chat-question").focus();
}
async function openService(item) {
  const root = clear("service-detail");
  root.append(el("p", "正在读取已发布图谱…"));
  const result = await api("/api/query", {method: "POST", body: JSON.stringify({question: "查看全部办理信息", service: item.name, service_code: item.service_code || "", region: item.region, space: knowledgeSpace})});
  root.replaceChildren();
  const card = el("article", undefined, "card service-overview"); root.append(card);
  card.append(el("h2", `${item.name} · ${item.region}`), el("p", `查询日期 ${result.as_of} · 知识已发布 · 业务有效性须结合原文明示日期核实`, "review-instruction"));
  for (const warning of result.warnings) card.append(el("p", warning, "chat-caution"));
  if (!result.facts.length) card.append(el("p", result.answer));
  const grouped = Map.groupBy ? Map.groupBy(result.facts, fact => fact.label) : result.facts.reduce((map, fact) => {if (!map.has(fact.label)) map.set(fact.label, []); map.get(fact.label).push(fact); return map;}, new Map());
  for (const [title, facts] of grouped) {
    const section = el("details", undefined, "service-fact-group"); section.append(el("summary", `${title} · ${facts.length} 项`));
    section.open = ["申请材料", "办理条件", "办理部门"].includes(title);
    for (const fact of facts) {
      const row = el("div", undefined, "fact"); row.append(el("p", fact.value), attributeView(Object.fromEntries(Object.entries(fact.attributes || {}).filter(([key, value]) => key !== "content" || value !== fact.value))));
      if (fact.rule) row.append(ruleTextView(fact.rule));
      const evidence = el("details"); evidence.append(el("summary", `原文依据（${fact.evidence.length}）`), evidenceView(fact.evidence, fact.record_id));
      row.append(evidence); section.append(row);
    }
    card.append(section);
  }
  card.append(el("h3", "来源与事项属性"));
  for (const source of item.sources) {
    const box = el("div", undefined, "fact");
    box.append(el("strong", source.title), attributeView(source.attributes), button("打开来源", async () => openSource(source.revision_id, {metadata: {title: source.title}})));
    card.append(box);
  }
  card.append(button("转到客服对话", () => queryService(item)));
  card.append(button("查看政策依据路径", () => openReasoning(item, "paths")), button("辅助核对明确条件", () => openReasoning(item, "conditions")));
  if (user.role !== "agent") card.append(button("查看相关待审资料", async () => {
    $("review-filter").elements.search.value = item.name;
    $("review-filter").elements.state.value = "DRAFT"; reviewOffset = 0;
    await tab("review");
  }));
  card.scrollIntoView({block: "start"});
}
function ruleTextView(rule) {
  if (!rule.operator) return el("p", rule.condition);
  const root = el("div", undefined, "rule-summary");
  root.append(el("strong", {AND: "以下全部满足", OR: "以下任一满足", NOT: "排除以下情形"}[rule.operator]));
  const list = el("ul");
  for (const child of rule.conditions) {const li = el("li"); li.append(ruleTextView(child)); list.append(li);}
  root.append(list);
  if (rule.basis) root.append(el("p", `逻辑依据：${rule.basis}`, "muted"));
  return root;
}

$("diagnostic-form").addEventListener("submit", guarded(async () => {
  const root = clear("diagnostic-results"), submit = $("diagnostic-form").querySelector("button");
  submit.disabled = true; root.append(el("p", "正在执行图谱与原文检索…"));
  try {
    const data = await api("/api/retrieval/diagnose", {method: "POST", body: JSON.stringify({...formObject($("diagnostic-form")), space: knowledgeSpace})});
    root.replaceChildren();
    const result = data.result, summary = el("div", undefined, "card");
    summary.append(el("h2", "查询范围与路径"), el("p", data.message), el("p", `${result.scope?.region || "地区未确认"} · ${result.scope?.service || "事项未确认"} · ${result.as_of} · ${result.date_resolution}`), el("p", `${result.retrieval?.method || "未匹配"} / ${result.retrieval?.original_text || "尚未进行原文召回"}`), el("p", data.clarification || result.answer));
    for (const service of result.services) summary.append(el("p", `图候选：${service.name} · ${service.region} · ${service.title}`));
    for (const warning of result.warnings) summary.append(el("p", warning, "notice"));
    root.append(summary);
    const candidates = el("div", undefined, "card"); candidates.append(el("h2", `原文候选 ${data.retrieval.candidate_count || 0} · 选中完整块 ${data.selected_sources.length}`));
    if (data.retrieval.has_more) candidates.append(el("p", "候选展示达到 200 项上限；诊断列表不是全部候选。"));
    for (const chunk of data.retrieval.chunks || []) {
      const block = el("details", undefined, "diagnostic-chunk");
      block.append(el("summary", `${chunk.selected ? "已选" : "未选"} · ${chunk.title} · 关键词排名 ${chunk.lexical_rank || "—"} / 向量排名 ${chunk.vector_rank || "—"}${chunk.graph_required ? " · 图事实必留" : ""}`), el("p", chunk.quote));
      if (chunk.preview_truncated) block.append(el("p", "此处为预览，打开来源查看完整证据。", "muted"));
      block.append(button("查看原文块", () => openEvidence({chunk_id: chunk.chunk_id}, chunk.revision_id))); candidates.append(block);
    }
    root.append(candidates);
    const excluded = el("div", undefined, "card"); excluded.append(el("h2", "来源过滤原因"), el("p", `按当前事项匹配资料目录，最多检查 100 个版本${data.exclusions_truncated ? "，当前达到上限" : ""}。`));
    for (const item of data.exclusions) excluded.append(el("p", `${item.title}：${item.reasons.map(reason => diagnosticReasons[reason] || reason).join("；")}`));
    if (!data.exclusions.length) excluded.append(el("p", "本次目录检查没有发现被过滤的来源。"));
    root.append(excluded);
  } finally {submit.disabled = false;}
}));

function appendBusinessActions(actions, turn) {
  const answer = turn.answer;
  if (answer.retrieval?.identity_links?.length) actions.append(el("p", `本轮使用 ${answer.retrieval.identity_links.length} 项人工确认的事项关联扩展来源，各版本事实仍分别保留。`, "muted small"));
  const evidenceText = () => answer.citations.map((c, i) => `[${c.id || i + 1}] ${c.title} · ${c.section || "正文"}${c.page ? ` · 第${c.page}页` : ""} · 版本标识 ${c.revision_id}\n${c.quote}`).join("\n\n");
  actions.append(button("复制接线话术", async () => {
    await navigator.clipboard.writeText([answer.answer, ...(answer.warnings || []), ...(answer.missing_information || []).map(x => `待核实：${x}`)].filter(Boolean).join("\n"));
    notice("已复制答复及必要提醒，请核对后使用。");
  }), button("复制工单记录", async () => {
    await navigator.clipboard.writeText([`问题：${turn.question}`, `范围：${answer.scope?.region || turn.scope.region || "待确认"} / ${answer.scope?.service || turn.scope.service || "待确认"} / ${answer.as_of}`, `资料空间：${answer.space === "test" ? "合成测试，非政策" : "正式资料"}`, answer.answer, ...(answer.warnings || []), ...(answer.missing_information || []).map(x => `待核实：${x}`), evidenceText(), `查询追踪：${answer.trace_id || "无"}`].join("\n\n"));
    notice("已复制包含依据版本的工单记录。");
  }));
  if (answer.trace_id) actions.append(button("反馈答复", () => {
    if (actions.querySelector(".answer-feedback")) return;
    const box = el("div", undefined, "answer-feedback"), category = el("select"), note = el("textarea"), question = el("textarea");
    category.setAttribute("aria-label", "反馈类别");
    for (const [key, label] of Object.entries(feedbackLabels)) {const option = el("option", label); option.value = key; category.append(option);}
    note.placeholder = "补充问题（可空，请勿填写姓名、电话或证件号码）"; note.maxLength = 500; note.rows = 2;
    question.placeholder = "可选：脱敏后的问题，供维护人员复现检索"; question.maxLength = 1000; question.rows = 2;
    box.append(category, note, question, button("填入本题（提交前请脱敏）", () => question.value = turn.question), button("提交反馈", async () => {
      const result = await api("/api/feedback", {method: "POST", body: JSON.stringify({trace_id: answer.trace_id, category: category.value, note: note.value, question: question.value})});
      box.replaceChildren(el("p", result.duplicate ? "该类反馈已记录，无需重复提交。" : "已提交知识维护队列。"));
    }, "")); actions.append(box);
  }));
  if (user.role !== "agent") actions.append(button("诊断本题检索", async () => {
    const form = $("diagnostic-form");
    form.elements.question.value = turn.question;
    for (const key of ["region", "service", "service_code"]) form.elements[key].value = answer.scope?.[key] || turn.scope[key] || "";
    form.elements.as_of.value = turn.scope.as_of || "";
    await tab("diagnostics");
  }));
}
async function loadFeedback() {
  const data = await api(`/api/feedback?limit=20&offset=${feedbackOffset}`), root = clear("feedback-list");
  $("feedback-prev").disabled = feedbackOffset === 0;
  $("feedback-next").disabled = feedbackOffset + data.items.length >= data.total;
  root.append(el("p", `共 ${data.total} 条反馈`));
  for (const item of data.items) {
    const card = el("article", undefined, "card feedback-card"), trace = item.trace;
    card.append(el("h2", `${feedbackLabels[item.category]} · ${feedbackStates[item.review.status]}`), el("p", `${trace.scope.region || "地区未确认"} / ${trace.scope.service || "事项未确认"} · ${trace.as_of}`), el("p", item.note || "未填写补充说明"), el("p", `${item.actor} · ${item.created_at} · ${trace.status} · 查询 ${item.trace_id}`, "muted"));
    if (item.question) card.append(el("p", "提交者附上的问题：" + item.question));
    for (const rid of trace.sources) card.append(button("查看本轮证据版本 " + rid.slice(0, 8), async () => {await tab("review"); await loadRevision(rid, "review");}));
    if (item.review.note) card.append(el("p", "处理记录：" + item.review.note));
    if (["admin", "maintainer"].includes(user.role)) {
      const note = el("input"); note.placeholder = "处理说明（必填）"; note.maxLength = 1000; note.setAttribute("aria-label", "反馈处理说明"); card.append(note);
      for (const [status, label] of Object.entries(feedbackStates)) if (status !== item.review.status) card.append(button(label, async () => {
        if (!note.value.trim()) throw new Error("请填写处理说明");
        await api(`/api/feedback/${item.id}/review`, {method: "POST", body: JSON.stringify({status, expected_event_id: item.review.event_id, note: note.value})});
        await loadFeedback();
      }));
    }
    root.append(card);
  }
}
$("feedback-refresh").addEventListener("click", guarded(loadFeedback));
$("feedback-prev").addEventListener("click", guarded(async () => {feedbackOffset = Math.max(0, feedbackOffset - 20); await loadFeedback();}));
$("feedback-next").addEventListener("click", guarded(async () => {feedbackOffset += 20; await loadFeedback();}));

async function showRevisionChanges(record) {
  const dialog = el("dialog", undefined, "change-dialog"), heading = el("div", undefined, "dialog-heading");
  heading.append(el("h2", "版本差异与影响"), button("关闭", () => dialog.close()));
  const baseline = el("input"); baseline.placeholder = "可选：指定基线版本 ID；留空优先比较此前已发布版本"; baseline.maxLength = 100;
  baseline.setAttribute("aria-label", "比较基线版本 ID");
  const content = el("div", undefined, "change-content");
  async function run() {
    content.replaceChildren(el("p", "正在比较原文、知识结构与已发布事项…"));
    const data = await api(`/api/revisions/${record.id}/changes?before=${encodeURIComponent(baseline.value.trim())}`);
    content.replaceChildren();
    if (data.status === "NO_BASELINE") {content.append(el("p", data.message)); return;}
    const classification = {REEXTRACTION: "同原件同文本：抽取或人工修正差异", REPARSE: "同原件：解析结果发生变化", SOURCE_CHANGE: "原件不同：需要核对来源与业务版本关系"};
    content.append(el("p", classification[data.classification], "review-instruction"));
    for (const warning of data.warnings) content.append(el("p", warning, "notice"));
    const meta = el("div", undefined, "change-columns");
    for (const [side, title] of [["before", "基线"], ["after", "本次"]]) {
      const version = data[side], box = el("div", undefined, "card");
      box.append(el("h3", `${title} · 文档版本 ${version.number}`), el("p", version.filename), el("p", `${stateLabels[version.knowledge_status] || version.knowledge_status} · ${version.model || "模型未记录"}`), button("查看此版本原件", () => openSource(version.id, {filename: version.filename})));
      meta.append(box);
    }
    content.append(meta, el("h3", "可能受影响的事项"));
    for (const item of data.affected_services) content.append(el("p", `${item.name} · ${item.region}：${item.reason}`));
    if (!data.affected_services.length) content.append(el("p", "未发现需要扩展复核的事项结构差异。"));
    for (const item of data.published_candidates) content.append(button(`已发布同名来源：${item.title || item.name}`, () => openSource(item.revision_id, {metadata: {title: item.title}})));
    if (data.graph_status === "UNAVAILABLE" || data.graph_candidates_truncated) content.append(el("p", "已发布影响范围未完整查询，请结合图服务状态与候选上限复核。", "notice"));
    content.append(el("h3", `原文差异 ${data.text_changes_total} 处`));
    for (const change of data.text_changes) {
      const row = el("details"); row.append(el("summary", `${{replace: "内容变化", insert: "新增文本", delete: "本次文本未出现"}[change.operation]} · 基线行 ${change.before_lines.join("–")} / 本次行 ${change.after_lines.join("–")}`));
      const columns = el("div", undefined, "change-columns");
      for (const [field, label] of [["before", "基线"], ["after", "本次"]]) {const block = el("div"); block.append(el("strong", label), el("pre", change[field] || "（空）")); columns.append(block);}
      row.append(columns); if (change.truncated) row.append(el("p", "此处仅展示片段，请打开原件核对完整内容。")); content.append(row);
    }
    content.append(el("h3", `实体变化 ${data.entity_changes_total} 项`));
    for (const change of data.entity_changes) {
      const row = el("details"); row.append(el("summary", `${{added: "本次新增抽取", absent: "本次未抽取到", changed: "字段变化"}[change.operation]} · ${schema.entities[change.type]?.label || change.type} · ${change.name}`));
      if (change.fields) row.append(el("p", "变化字段：" + change.fields.join("、")));
      const columns = el("div", undefined, "change-columns");
      for (const [field, title] of [["before", "基线"], ["after", "本次"]]) {
        const entity = change[field], box = el("div"); box.append(el("strong", title));
        if (entity) {box.append(el("p", entity.name), attributeView(entity.attributes)); if (entity.rule) box.append(ruleTextView(entity.rule)); box.append(evidenceView(entity.evidence, data[field].id));}
        else box.append(el("p", "本次提取列表无对应项"));
        columns.append(box);
      }
      row.append(columns); content.append(row);
    }
    content.append(el("h3", `关系变化 ${data.relation_changes_total} 项`));
    for (const change of data.relation_changes) content.append(el("p", `${change.operation === "added" ? "新增抽取" : "本次未抽取到"}：${change.source?.[1]} → ${relationLabel(change.type)} → ${change.target?.[1]}`));
    for (const item of data.ambiguous_entities) content.append(el("p", `同名实体无法唯一对应：${item.name}（${item.before_count} → ${item.after_count}），需人工核对。`, "notice"));
    if (data.has_more) content.append(el("p", "差异展示达到上限，不能将当前列表当作全部变更。", "notice"));
  }
  dialog.append(heading, baseline, button("比较版本", run, ""), content);
  dialog.addEventListener("close", () => dialog.remove()); document.body.append(dialog); dialog.showModal();
  try {await run();} catch (error) {content.replaceChildren(el("p", error.message, "error"));}
}
