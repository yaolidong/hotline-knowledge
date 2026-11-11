"use strict";
const $ = (id) => document.getElementById(id);
let token = "",
  user = null,
  schema = null,
  active = "query",
  current = null;
let graphOffset = 0,
  graphFocus = "",
  resultOffset = 0,
  reviewOffset = 0,
  importMode = "file";
let graphView = null;
let knowledgeSpace = "production";
function scopedPath(path, space = knowledgeSpace) {
  const url = new URL(path, location.origin);
  url.searchParams.set("space", space);
  return url.pathname + url.search;
}
const readers = new Map();
const recordRequests = new Map();
const revisionRequests = new Map();
const stateLabels = {
  QUEUED: "排队中",
  RUNNING: "处理中",
  READY: "抽取完成",
  FAILED: "失败",
  PARTIAL: "部分完成",
  DRAFT: "待审核",
  APPROVED: "已审核",
  PUBLISHING: "发布中",
  PUBLISHED: "已发布",
  REJECTED: "已拒绝",
  RETIRED: "已停用",
};
function relationLabel(type) {
  return (
    schema.relations[type]?.label ||
    { SUPPORTED_BY: "来源证据", HAS_CHUNK: "包含原文" }[type] ||
    type
  );
}
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function notice(message) {
  $("notification").textContent = message;
  $("notification").hidden = false;
}
async function api(path, options = {}) {
  const space = knowledgeSpace;
  const headers = { Authorization: `Bearer ${token}`, ...options.headers };
  if (options.body && !(options.body instanceof FormData))
    headers["Content-Type"] = "application/json";
  const response = await fetch(!options.method || options.method === "GET" ? scopedPath(path, space) : path, { ...options, headers });
  if (space !== knowledgeSpace) throw new Error("资料空间已切换，请在当前空间重新操作");
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: "请求失败" }));
    if (Array.isArray(error.detail) && error.detail.some((item) => item.loc?.includes("issue_resolutions")))
      throw new Error("请逐项选择质检处置结果，并填写每一项的处置理由。");
    throw new Error(typeof error.detail === "string" ? error.detail : "提交内容不符合要求，请检查必填字段和长度。");
  }
  return response.json();
}
function guarded(fn) {
  return async (event) => {
    if (event) event.preventDefault();
    try {
      await fn(event);
    } catch (error) {
      notice(error.message);
    }
  };
}
function button(text, action, cls = "secondary") {
  const b = el("button", text, cls);
  b.type = "button";
  b.addEventListener("click", guarded(action));
  return b;
}
function formObject(form) {
  return Object.fromEntries(new FormData(form).entries());
}
function showJSON(value) {
  return el("pre", JSON.stringify(value, null, 2));
}
function attributeView(attributes) {
  const labels = {
    document_number: "文号",
    issued_on: "发布日期",
    applicant_scope: "申请主体（原文细分）",
    effective_on: "施行日期",
    expires_on: "失效日期",
    repealed_on: "废止日期",
    document_type: "文件类型",
    provision_number: "条款编号",
    content: "内容",
    level: "层级",
    address: "地址",
    telephone: "联系电话",
    service_code: "事项编码",
    fees: "费用",
    time_limit: "办理时限",
    applicants: "适用对象",
    copies: "份数",
    format: "材料形式",
    order: "步骤序号",
    url: "网址",
    channel_type: "渠道类型",
    region_code: "区划代码",
  };
  const list = el("dl", undefined, "attribute-list");
  for (const [key, value] of Object.entries(attributes || {}))
    if (value) list.append(el("dt", labels[key] || key), el("dd", value));
  return list;
}
function details(title, value) {
  const node = el("details");
  node.append(el("summary", title), showJSON(value));
  return node;
}
function clear(id) {
  $(id).replaceChildren();
  return $(id);
}
async function tab(name) {
  active = name;
  $("notification").hidden = true;
  for (const section of document.querySelectorAll(".tab"))
    section.hidden = section.id !== `tab-${name}`;
  for (const b of document.querySelectorAll("nav button")) {
    b.classList.toggle("active", b.dataset.tab === name);
    b.setAttribute("aria-current", b.dataset.tab === name ? "page" : "false");
  }
  if (name === "results" || name === "review") {
    closeRecord(name);
    await loadRecords(name);
  }
  if (name === "import") await loadStatus();
  if (name === "services") await loadServices();
  if (name === "feedback") await loadFeedback();
  if (name === "operations") await loadOperations();
  if (name === "graph") await loadGraph();
}

function requireReviewField(input, message) {
  input.setAttribute("aria-invalid", "true");
  input.scrollIntoView({block: "center", behavior: "instant"});
  input.focus({preventScroll: true});
  input.addEventListener("input", () => input.removeAttribute("aria-invalid"), {once: true});
  input.addEventListener("change", () => input.removeAttribute("aria-invalid"), {once: true});
  throw new Error(message);
}

function reviewResolutions(card, record) {
  const resolutions = {};
  if (!record.quality?.requires_acknowledgement) return resolutions;
  for (const issue of record.quality?.issues || []) {
    const select = Array.from(card.querySelectorAll("select[data-quality-issue]")).find((s) => s.dataset.qualityIssue === issue.id);
    const reason = Array.from(card.querySelectorAll("input[data-issue-reason]")).find((s) => s.dataset.issueReason === issue.id);
    if (!select || !reason) throw new Error("质检项目已变化，请刷新详情后再审核。");
    if (select.value === "unresolved") requireReviewField(select, `请先核对“${issue.name}”，选择处置结果。`);
    if (!reason.value.trim()) requireReviewField(reason, `请填写“${issue.name}”的处置理由。`);
    resolutions[issue.id] = {status: select.value, note: reason.value.trim()};
  }
  return resolutions;
}

async function showRevisionGraph(record) {
  const space = record.metadata.synthetic ? "test" : "production";
  if (knowledgeSpace !== space) await switchKnowledgeSpace(space);
  $("graph-form").reset();
  $("graph-form").elements.revision_id.value = record.id;
  $("graph-record-title").textContent = `${record.metadata.title || record.filename} · v${record.number}`;
  graphOffset = 0;
  graphFocus = "";
  await tab("graph");
}
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  token = $("token").value;
  try {
    user = await api("/api/me");
    knowledgeSpace = user.default_space || "production";
    $("knowledge-space").value = knowledgeSpace;
    $("space-control").hidden = false;
    $("test-space-banner").hidden = knowledgeSpace !== "test";
    $("import-form").elements.synthetic.checked = knowledgeSpace === "test";
    schema = await api("/api/schema");
    $("token").value = "";
    $("login").hidden = true;
    $("workspace").hidden = false;
    $("logout").hidden = false;
    const roleLabels = {
      admin: "管理员",
      maintainer: "知识维护",
      reviewer: "审核人员",
      agent: "客服人员",
    };
    $("identity").textContent = `${user.name} · ${roleLabels[user.role]}`;
    $("login-error").textContent = "";
    for (const n of document.querySelectorAll("[data-maintain]"))
      n.hidden = user.role === "agent";
    document.querySelector('[data-tab="import"]').hidden = ![
      "admin",
      "maintainer",
    ].includes(user.role);
    for (const [id, items] of [
      ["entity-filter", schema.entities],
      ["relation-filter", schema.relations],
    ]) {
      const select = $(id);
      while (select.options.length > 1) select.remove(1);
      for (const [key, value] of Object.entries(items)) {
        const option = el("option", value.label);
        option.value = key;
        select.append(option);
      }
    }
    await tab("query");
  } catch (error) {
    token = "";
    $("login-error").textContent = error.message;
  }
});
$("logout").addEventListener("click", () => {
  token = "";
  location.reload();
});
for (const b of document.querySelectorAll("nav button"))
  b.addEventListener(
    "click",
    guarded(() => tab(b.dataset.tab)),
  );
async function loadStatus() {
  const s = await api("/api/status");
  const root = clear("service-status");
  for (const [title, ready, label] of [
    ["知识抽取", s.model_configured, s.model_configured ? "已配置" : "未配置"],
    ["图数据库", s.graph_ready, s.graph_ready ? "已连接" : "未连接"],
    ["本地 OCR", s.ocr?.ready, s.ocr?.ready ? "已就绪" : "未就绪"],
    ["任务处理", s.worker_enabled, s.worker_enabled ? "已启用" : "未启用"],
  ]) {
    const row = el("div", undefined, "service-row"),
      status = el("span", label);
    if (!ready) status.className = "error";
    row.append(el("span", title), status);
    root.append(row);
  }
  if (!s.ocr?.ready)
    root.append(el("p", s.ocr?.message || "扫描件需要本地 OCR 服务。", "muted"));
  if (s.graph_error) root.append(el("p", s.graph_error, "error small"));
  $("upload-limit").textContent =
    `每批最多 10 个文件，总计不超过 ${s.max_upload_mb || 20} MB。`;
}
function setImportMode(mode) {
  importMode = mode;
  $("file-import-panel").hidden = mode !== "file";
  $("text-import-panel").hidden = mode !== "text";
  for (const item of ["file", "text"])
    $("import-" + item + "-mode").setAttribute(
      "aria-pressed",
      String(mode === item),
    );
}
$("import-file-mode").addEventListener("click", () => setImportMode("file"));
$("import-text-mode").addEventListener("click", () => setImportMode("text"));
function showFiles() {
  const root = clear("selected-files");
  for (const file of $("files").files) {
    const row = el("div", undefined, "selected-file");
    row.append(
      el("span", file.name),
      el("span", (file.size / 1024 / 1024).toFixed(2) + " MB"),
    );
    root.append(row);
  }
}
$("files").addEventListener("change", showFiles);
for (const event of ["dragover", "dragenter"])
  $("drop-zone").addEventListener(event, (e) => {
    e.preventDefault();
    $("drop-zone").classList.add("dragover");
  });
for (const event of ["dragleave", "drop"])
  $("drop-zone").addEventListener(event, (e) => {
    e.preventDefault();
    $("drop-zone").classList.remove("dragover");
    if (event === "drop" && e.dataTransfer?.files.length) {
      $("files").files = e.dataTransfer.files;
      showFiles();
    }
  });
$("import-form").addEventListener(
  "submit",
  guarded(async () => {
    const values = formObject($("import-form"));
    const meta = {
      title: values.title,
      region: values.region,
      document_number: values.document_number,
      source: values.source,
      document_type: values.document_type,
      synthetic: $("import-form").elements.synthetic.checked,
      ocr_mode: importMode === "file" ? values.ocr_mode : "auto",
    };
    const files = $("files").files;
    if (meta.synthetic !== (knowledgeSpace === "test")) throw new Error("导入资料类型与当前空间不一致，请先切换正式或合成测试空间");
    let result;
    const submit = $("import-form").querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      if (importMode === "file") {
        if (!files.length)
          throw new Error("请先选择待导入文件，或切换到粘贴文本");
        const body = new FormData();
        body.append("metadata", JSON.stringify(meta));
        for (const file of files) body.append("files", file);
        result = await api("/api/import/files", { method: "POST", body });
      } else {
        result = await api("/api/import/text", {
          method: "POST",
          body: JSON.stringify({ ...meta, text: values.text }),
        });
      }
      const root = clear("import-result");
      const items = result.items || [result];
      root.append(
        el("strong", `已提交 ${items.length} 项资料`),
        el(
          "p",
          "文件将依次解析、抽取，完成后等待人工审核。重复资料会返回已有任务。",
          "muted small",
        ),
      );
      for (const [index, item] of items.entries())
        root.append(
          button(
            `${item.duplicate ? "查看已有任务" : "查看处理进度"}${items.length > 1 ? " " + (index + 1) : ""}`,
            async () => {
              await tab("results");
              await loadRevision(item.revision_id, "results");
            },
          ),
        );
      notice("资料已提交，可查看处理进度与识别结果。");
    } finally {
      submit.disabled = false;
    }
  }),
);
async function loadRecords(target) {
  const sequence = (recordRequests.get(target) || 0) + 1;
  recordRequests.set(target, sequence);
  const offset = target === "results" ? resultOffset : reviewOffset;
  const params = new URLSearchParams(formObject($(`${target}-filter`)));
  params.set("limit", "20");
  params.set("offset", offset);
  const data = await api(`/api/revisions?${params}`);
  if (recordRequests.get(target) !== sequence) return;
  $(`${target}-count`).textContent = `共 ${data.total} 个版本`;
  $(`${target}-page`).textContent = data.total
    ? `${offset + 1}–${Math.min(offset + data.items.length, data.total)} / ${data.total}`
    : "暂无记录";
  $(`${target}-prev`).disabled = offset === 0;
  $(`${target}-next`).disabled = offset + data.items.length >= data.total;
  const root = clear(`${target}-list`);
  if (!data.items.length) {
    const empty = el("div", undefined, "empty-state");
    empty.append(
      el(
        "h2",
        target === "review"
          ? "当前没有符合条件的资料"
          : "当前没有符合条件的任务",
      ),
      el("p", "可调整筛选条件，或导入新资料后查看处理结果。"),
    );
    root.append(empty);
    return;
  }
  const table = el("table"),
    head = el("tr"),
    thead = el("thead"),
    tbody = el("tbody");
  for (const name of [
    "文件 / 标题",
    "地区",
    "版本",
    "任务",
    "知识状态",
    "操作",
  ])
    head.append(el("th", name));
  thead.append(head);
  table.append(thead, tbody);
  for (const record of data.items) {
    const row = el("tr");
    if (current?.id === record.id) row.classList.add("selected");
    const title = el("td", record.metadata.title || record.filename);
    if (record.group_title) title.append(el("div", `发布快照 · ${record.group_title}`, "task-meta"));
    title.append(
      el(
        "div",
        `${record.extension?.slice(1).toUpperCase() || "文件"} · ${record.filename}${record.metadata.synthetic ? " · 合成测试" : ""}`,
        "task-meta",
      ),
    );
    row.append(
      title,
      el("td", record.metadata.region || "待确认"),
      el("td", "v" + record.number),
    );
    const task = el("td"),
      state = el("td");
    const taskLabel =
      record.task_status === "RUNNING"
        ? record.stage === "parse"
          ? "解析 / OCR"
          : "知识抽取"
        : stateLabels[record.task_status];
    task.append(
      el("span", taskLabel, `badge status-${record.task_status.toLowerCase()}`),
    );
    if (record.task_status === "RUNNING" && record.total)
      task.append(
        el(
          "div",
          `${record.progress} / ${record.total} ${record.stage === "parse" ? "页" : "文本块"}`,
          "task-meta",
        ),
      );
    if (record.error) task.title = record.error;
    state.append(
      el(
        "span",
        stateLabels[record.knowledge_status],
        `badge status-${record.knowledge_status.toLowerCase()}`,
      ),
    );
    row.append(task, state);
    const actions = el("td");
    actions.append(
      button(target === "review" ? "对照审核" : "查看", () =>
        loadRevision(record.id, target),
      ),
    );
    row.append(actions);
    tbody.append(row);
  }
  const wrap = el("div", undefined, "card scroll-table");
  wrap.append(table);
  root.append(wrap);
}
async function downloadSource(recordId, filename) {
  const response = await fetch(scopedPath(`/api/source/${recordId}`), {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new Error("原文件下载失败或来源已停用");
  const url = URL.createObjectURL(await response.blob()),
    link = el("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function createReader(host, recordId, evidence = null, page = 1) {
  return new SourceViewer(host, recordId, {
    json: api,
    download: downloadSource,
    evidence,
    page,
    blob: async (path) => {
      const response = await fetch(scopedPath(path), {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || "原件读取失败，知识状态可能已变化");
      }
      return response.blob();
    },
  });
}
async function openSource(recordId, source = {}, evidence = null) {
  const modal = el("dialog"),
    heading = el("div", undefined, "dialog-heading");
  const text = el("div");
  text.append(
    el("h2", source.metadata?.title || source.filename || "原文件与证据"),
    el(
      "p",
      `原始资料 · ${source.chunk?.page ? "第 " + source.chunk.page + " 页" : "可切换原件与解析文字"} · ${source.chunk?.section || ""}`,
      "muted",
    ),
  );
  heading.append(
    text,
    button("关闭", () => modal.close()),
  );
  modal.append(heading);
  if (source.metadata?.source) modal.append(sourceLink(source.metadata.source));
  const host = el("div");
  modal.append(host);
  const actions = el("div", undefined, "actions");
  let reader;
  actions.append(button("查看解析全文", () => reader.setMode("text")));
  for (const [key, label] of [
    ["previous", "上一段"],
    ["following", "下一段"],
  ]) {
    if (source.chunk?.[key])
      actions.append(
        button(label, async () => {
          const adjacent = await api(
            `/api/evidence/${recordId}/${source.chunk[key]}`,
          );
          modal.close();
          await openSource(recordId, adjacent, { chunk_id: adjacent.chunk.id });
        }),
      );
  }
  modal.append(actions);
  document.body.append(modal);
  modal.showModal();
  reader = createReader(host, recordId, evidence, source.chunk?.page || 1);
  modal.addEventListener(
    "close",
    () => {
      reader.destroy();
      modal.remove();
    },
    { once: true },
  );
  await reader.ready;
}
function sourceLink(source) {
  try {
    const url = new URL(source);
    if (!["http:", "https:"].includes(url.protocol))
      return el("span", source || "未填写");
    const link = el("a", "打开来源网页");
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  } catch {
    return el("span", source || "未填写");
  }
}
async function openEvidence(evidence, recordId) {
  const source = await api(`/api/evidence/${recordId}/${evidence.chunk_id}`);
  const reader = readers.get(active);
  if (reader?.recordId === recordId)
    await reader.reveal(evidence, source.chunk.page);
  else await openSource(recordId, source, evidence);
}
function evidenceView(items, recordId) {
  const box = el("div");
  for (const evidence of items || []) {
    box.append(el("blockquote", evidence.quote));
    box.append(button("定位原文", () => openEvidence(evidence, recordId)));
  }
  return box;
}
async function loadRevision(id, target) {
  const sequence = (revisionRequests.get(target) || 0) + 1;
  revisionRequests.set(target, sequence);
  const record = await api(`/api/revisions/${id}`);
  if (revisionRequests.get(target) !== sequence) return;
  current = record;
  const r = current,
    root = clear(target === "review" ? "review-detail" : "extraction-detail");
  root.hidden = false;
  $(`${target}-list`).hidden = true;
  $(`${target}-filter`).hidden = true;
  $(`${target}-prev`).closest(".pager").hidden = true;
  const back = el("div", undefined, "record-backbar");
  back.append(
    button("← 返回资料列表", () => closeRecord(target)),
    el("span", `${r.filename} · v${r.number}`, "muted small"),
  );
  root.append(back);
  readers.get(target)?.destroy();
  const workspace = el("div", undefined, "record-workspace"),
    sourceHost = el("div");
  const card = el("div", undefined, "card record-card");
  const publication = r.parsed_metadata?._group_publication;
  const groupReview = target === "review" && ["DRAFT", "REJECTED"].includes(r.knowledge_status) && ["READY", "PARTIAL", "FAILED"].includes(r.task_status) && r.chunks?.length && !publication;
  const factHost = groupReview ? el("div", undefined, "fact-review") : null;
  card.append(
    el("h2", r.metadata.title || r.filename),
    el(
      "p",
      `版本 ${r.number} · ${stateLabels[r.knowledge_status]} · ${r.metadata.region || "地区待确认"}`,
    ),
  );
  if (r.error) card.append(el("p", r.error, "error"));
  if (r.task_status === "READY") {
    const publication = el("div", undefined, "record-publication actions");
    if (r.knowledge_status === "PUBLISHED") {
      publication.append(el("span", "已发布到图谱", "badge status-published"), button("查看此版本图谱", () => showRevisionGraph(r)));
    } else if (r.knowledge_status === "APPROVED") {
      publication.append(el("span", "已审核 · 尚未发布到图谱", "badge status-approved"));
    } else if (r.knowledge_status === "DRAFT") {
      publication.append(el("span", "抽取完成 · 待审核发布", "badge status-draft"));
    }
    if (target !== "review" && ["DRAFT", "APPROVED"].includes(r.knowledge_status))
      publication.append(button(r.knowledge_status === "APPROVED" ? "去发布" : "去审核", async () => {await tab("review"); await loadRevision(id, "review");}));
    card.append(publication);
  }
  const stats = el("div", undefined, "record-stats");
  stats.append(
    el("span", `${r.chunks?.length || 0} 个文本块`),
    el("span", `${r.extraction?.entities.length || 0} 个实体`),
    el("span", `${r.extraction?.relations.length || 0} 条关系`),
  );
  card.append(stats);
  if (publication) card.append(el("p", `事实组：${publication.title}`, "badge"), button("返回原审核草稿", () => loadRevision(publication.source_revision_id, "review")));
  if (factHost) card.append(factHost);
  const chunkResults = r.parsed_metadata?._extraction || {};
  if (Object.keys(chunkResults).length) {
    const failed = Object.entries(chunkResults).filter(([, item]) => item.status === "FAILED");
    const complete = Object.values(chunkResults).filter((item) => item.status === "READY").length;
    const panel = el("details");
    panel.append(el("summary", `分块处理 · ${complete} 已完成 · ${failed.length} 失败`));
    for (const [chunkId, item] of failed) {
      const chunk = r.chunks.find((c) => c.id === chunkId);
      panel.append(el("p", `${chunk?.page ? `第 ${chunk.page} 页 · ` : ""}${chunk?.section || "正文"}：${item.error}`, "error small"));
      if (chunk) panel.append(evidenceView([{chunk_id: chunkId, quote: chunk.text, start: 0, end: chunk.text.length}], id));
      if (item.candidate) panel.append(details("未通过校验的候选（不入图）", item.candidate));
    }
    card.append(panel);
  }
  const sourcePages = r.parsed_metadata?._source?.pages || [];
  const ocrPages = sourcePages.filter((p) => p.method === "ocr");
  if (ocrPages.length)
    card.append(
      el(
        "p",
        `${ocrPages.length} 页经过 OCR 识别。请重点核对数字、材料名称与条件中的否定词；识别置信度不等于准确率。`,
        "review-instruction",
      ),
    );
  card.append(
    details("文档元数据", {
      ...r.metadata,
      sha256: r.sha256,
      model: r.model,
      extracted_at: r.extracted_at,
      parsed_metadata: Object.fromEntries(
        Object.entries(r.parsed_metadata || {}).filter(
          ([key]) => !["_source", "_extraction"].includes(key),
        ),
      ),
    }),
  );
  if (r.text) card.append(details("文本位置数据（高级）", r.chunks));
  if (r.extraction && !groupReview) {
    if (r.extraction.normalizations?.length) card.append(details("规范化记录（可对照原文）", r.extraction.normalizations));
    if (r.quality?.issues.length) {
      const checks = el("div", undefined, "review-instruction");
      checks.append(el("h3", `语义核对 · ${r.quality.issues.length} 项`), el("p", r.quality.message));
      for (const issue of r.quality.issues) {
        const label = el("label");
        label.append(el("span", `${issue.name}：${issue.message}`));
        if (target === "review" && r.knowledge_status === "DRAFT" && ["admin", "reviewer"].includes(user.role) && r.quality.requires_acknowledgement) {
          const select = el("select"), reason = el("input");
          select.dataset.qualityIssue = issue.id;
          select.setAttribute("aria-label", `处置：${issue.name}`);
          for (const [value, text] of [["unresolved", "暂不能确认（阻止通过）"], ["verified", "已对照证据确认"], ["false_positive", "规则误报，已说明原因"]]) select.append(new Option(text, value));
          reason.dataset.issueReason = issue.id; reason.maxLength = 500;
          reason.placeholder = "处置理由（必填）：核对了哪段原文，或为何属于误报";
          reason.required = true;
          reason.setAttribute("aria-label", `处置理由：${issue.name}`);
          label.append(select, reason);
        }
        checks.append(label);
      }
      card.append(checks);
    }
    card.append(el("h3", "识别出的实体"));
    for (const entity of r.extraction.entities) {
      const d = el("details");
      d.append(
        el(
          "summary",
          `${schema.entities[entity.type]?.label || entity.type} · ${entity.display_name || entity.canonical_name || entity.name}`,
        ),
        attributeView(entity.attributes),
        evidenceView(entity.evidence, id),
      );
      if (entity.canonical_name && entity.canonical_name !== entity.name)
        d.append(el("p", `原文名称：${entity.name}；规范名称：${entity.canonical_name}`, "muted small"));
      if (entity.aliases?.length)
        d.append(el("p", "别名：" + entity.aliases.join("、"), "muted small"));
      if (entity.rule) d.append(details("条件 / 材料逻辑", entity.rule));
      if (entity.sequence_basis) d.append(el("p", "原文明示流程：" + entity.sequence_basis, "muted small"));
      if (target === "review") d.append(button("跨文档实体关联", () => showIdentity(d, r, entity)));
      card.append(d);
    }
    const names = Object.fromEntries(
      r.extraction.entities.map((e) => [e.id, e.name]),
    );
    card.append(el("h3", "实体之间的关系"));
    for (const relation of r.extraction.relations) {
      const d = el("details");
      d.append(
        el(
          "summary",
          `${names[relation.source]} → ${relationLabel(relation.type)} → ${names[relation.target]}`,
        ),
        evidenceView(relation.evidence, id),
      );
      card.append(d);
    }
  }
  const actions = el("div", undefined, "actions");
  if (["admin", "maintainer"].includes(user.role) && ["PARTIAL", "FAILED"].includes(r.task_status)
      && r.knowledge_status === "DRAFT" && Object.keys(chunkResults).length)
    actions.append(button("重试失败块", async () => {
      await api(`/api/revisions/${id}/retry-extraction`, {method: "POST", body: JSON.stringify({expected_version: r.version})});
      notice("已排队，保留成功块。");
      await loadRevision(id, target);
    }));
  actions.append(
    button("刷新详情", () => loadRevision(id, target)),
    button("放大阅读原件", () => openSource(id, r)),
    button("下载原文件", () => downloadSource(id, r.filename)),
  );
  if (["admin", "maintainer"].includes(user.role))
    actions.append(
      button("重新处理（新版本）", async () => {
        await api(`/api/revisions/${id}/reprocess`, { method: "POST" });
        notice("已创建新版本，历史记录保留。");
        await loadRecords(target);
      }),
    );
  actions.append(
    button("版本差异与影响", () => showRevisionChanges(r)),
    button("查看操作历史", async () => {
      const result = await api(`/api/revisions/${id}/history`);
      card.append(details("历史记录与快照", result.items));
    }),
  );
  card.append(actions);
  if (target === "review" && r.task_status === "READY" && !groupReview && (!publication || r.knowledge_status === "PUBLISHED")) {
    const note = el("input");
    note.placeholder = "审核/修改说明（必填）";
    note.maxLength = 1000;
    note.setAttribute("aria-label", "审核说明");
    card.append(note);
    if (["admin", "reviewer"].includes(user.role)) {
      const states = {
        DRAFT: ["approve", "reject"],
        APPROVED: ["publish", "reject"],
        PUBLISHED: ["retire"],
      };
      const labels = {
        approve: "审核通过",
        reject: "拒绝",
        publish: "发布到图谱",
        retire: "停用",
      };
      const bar = el("div", undefined, "actions review-actions");
      for (const action of states[r.knowledge_status] || [])
        bar.append(
          button(
            labels[action],
            async () => {
              const resolutions = action === "approve" ? reviewResolutions(card, r) : {};
              if (!note.value.trim()) requireReviewField(note, "请填写审核说明。");
              if (action === "publish") notice("正在写入图数据库并回读校验…");
              const updated = await api(`/api/revisions/${id}/review`, {
                method: "POST",
                body: JSON.stringify({
                  action,
                  expected_version: r.version,
                  note: note.value,
                  issue_resolutions: resolutions,
                }),
              });
              await loadRecords(target);
              await loadRevision(id, target);
              if (action === "publish") {
                await showRevisionGraph(updated);
                notice("已发布，图数据库写入与回读校验通过。");
              } else {
                notice(action === "approve" ? "审核已通过。请点击“发布到图谱”完成入图。" : "操作完成。");
                if (action === "approve") {
                  $("review-detail").querySelector('[aria-label="审核说明"]').value = note.value.trim();
                  const next = $("review-detail").querySelector(".review-actions button");
                  next?.scrollIntoView({block: "center"});
                  next?.focus({preventScroll: true});
                }
              }
            },
            action === "publish" ? "" : "secondary",
          ),
        );
      card.append(bar);
    }
    if (
      ["admin", "maintainer"].includes(user.role) &&
      ["DRAFT", "REJECTED", "APPROVED"].includes(r.knowledge_status)
    ) {
      const structured = el("details", undefined, "knowledge-editor");
      structured.append(el("summary", "表单修正实体、关系与证据"));
      const editorHost = el("div");
      structured.append(editorHost);
      let created = false;
      structured.addEventListener("toggle", () => {
        if (structured.open && !created) {
          created = true;
          new KnowledgeEditor(editorHost, r, async (extraction) => {
            if (!note.value.trim()) throw new Error("请填写修改说明");
            await api(`/api/revisions/${id}/extraction`, {method: "PUT", body: JSON.stringify({expected_version: r.version, extraction, note: note.value})});
            await loadRevision(id, target);
            notice("表单修正已通过证据校验，需重新审核。");
          });
        }
      });
      card.append(structured);
      const d = el("details");
      d.append(el("summary", "高级：修正实体、关系与证据（JSON）"));
      const editor = el("textarea");
      editor.rows = 16;
      editor.value = JSON.stringify(r.extraction, null, 2);
      editor.setAttribute("aria-label", "知识修正 JSON");
      d.append(
        editor,
        button("保存修正并重新待审", async () => {
          if (!note.value.trim()) throw new Error("请填写修改说明");
          await api(`/api/revisions/${id}/extraction`, {
            method: "PUT",
            body: JSON.stringify({
              expected_version: r.version,
              extraction: JSON.parse(editor.value),
              note: note.value,
            }),
          });
          await loadRevision(id, target);
          notice("已保存；修正后的知识需重新审核。");
        }),
      );
      card.append(d);
    }
  }
  workspace.append(sourceHost, card);
  root.append(workspace);
  if (factHost) await renderFactReview(factHost, r, () => loadRevision(id, target));
  const reader = createReader(sourceHost, id);
  readers.set(target, reader);
  root.scrollIntoView({ block: "start" });
  await reader.ready;
}
async function showIdentity(host, record, entity) {
  host.querySelector(".identity-panel")?.remove();
  const panel = el("div", undefined, "identity-panel");
  host.append(panel);
  const data = await api(`/api/revisions/${record.id}/identity-candidates?entity_id=${encodeURIComponent(entity.id)}`);
  panel.append(el("p", data.message, "review-instruction"));
  const note = el("input");
  note.placeholder = "关联审核依据（必填）";
  note.setAttribute("aria-label", "关联审核依据");
  panel.append(note);
  const allowed = ["admin", "reviewer"].includes(user.role);
  const change = async (item, action, eventId = 0) => {
    if (!note.value.trim()) throw new Error("请填写关联或撤销的审核依据");
    await api("/api/identity/decisions", {method: "POST", body: JSON.stringify({
      source_revision: record.id, source_entity: entity.id,
      target_revision: item.revision_id, target_entity: item.entity_id,
      source_version: data.source_version || record.version, target_version: item.version,
      expected_event_id: eventId, action, note: note.value,
    })});
    await showIdentity(host, record, entity);
  };
  for (const item of data.items) {
    const row = el("div");
    row.append(el("strong", `${item.name} · ${item.title}`), el("p", item.reason),
      button("对照该来源", () => openEvidence(item.evidence[0], item.revision_id)));
    const previous = data.associations.find((a) => a.entities.some((e) => e.revision_id === item.revision_id && e.entity_id === item.entity_id));
    if (allowed && !previous?.active) row.append(button("确认同一实体", () => change(item, "confirm", previous?.event_id || 0)));
    panel.append(row);
  }
  for (const item of data.associations) {
    const other = item.entities.find((e) => e.revision_id !== record.id || e.entity_id !== entity.id);
    const row = el("div");
    row.append(el("p", `${other.name}：${item.active ? "已确认身份关联" : item.action === "revoke" ? "已撤销" : "来源已变化，关联失效"} · ${item.note}`));
    if (allowed && item.action === "confirm") row.append(button("撤销关联", async () => {
      const target = await api(`/api/revisions/${other.revision_id}`);
      await change({...other, version: target.version}, "revoke", item.event_id);
    }));
    panel.append(row);
  }
  if (!data.items.length) panel.append(el("p", "没有找到满足地区和类型约束的候选。", "muted"));
  if (data.has_more) panel.append(el("p", "候选达到数量上限，当前仅显示部分结果。", "muted"));
}
function closeRecord(target) {
  readers.get(target)?.destroy();
  readers.delete(target);
  const root = clear(
    target === "review" ? "review-detail" : "extraction-detail",
  );
  root.hidden = true;
  $(`${target}-list`).hidden = false;
  $(`${target}-filter`).hidden = false;
  $(`${target}-prev`).closest(".pager").hidden = false;
  revisionRequests.set(target, (revisionRequests.get(target) || 0) + 1);
}
$("refresh-results").addEventListener(
  "click",
  guarded(() => loadRecords("results")),
);
$("refresh-review").addEventListener(
  "click",
  guarded(() => loadRecords("review")),
);
$("results-prev").addEventListener(
  "click",
  guarded(() => {
    resultOffset = Math.max(0, resultOffset - 20);
    return loadRecords("results");
  }),
);
$("results-next").addEventListener(
  "click",
  guarded(() => {
    resultOffset += 20;
    return loadRecords("results");
  }),
);
for (const target of ["results", "review"]) {
  $(`${target}-filter`).addEventListener(
    "submit",
    guarded(async () => {
      if (target === "results") resultOffset = 0;
      else reviewOffset = 0;
      await loadRecords(target);
    }),
  );
  $(`${target}-filter`).elements.state.addEventListener(
    "change",
    guarded(async () => {
      if (target === "results") resultOffset = 0;
      else reviewOffset = 0;
      await loadRecords(target);
    }),
  );
}
$("review-prev").addEventListener(
  "click",
  guarded(() => {
    reviewOffset = Math.max(0, reviewOffset - 20);
    return loadRecords("review");
  }),
);
$("review-next").addEventListener(
  "click",
  guarded(() => {
    reviewOffset += 20;
    return loadRecords("review");
  }),
);
function svg(tag, attrs) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  return n;
}
let graphRequest = 0;
async function loadGraph() {
  const request = ++graphRequest;
  graphSnapshot = null;
  const params = new URLSearchParams(formObject($("graph-form")));
  $("graph-record-scope").hidden = !params.get("revision_id");
  params.set("offset", graphOffset);
  params.set("focus", graphFocus);
  graphView?.destroy();
  graphView = null;
  clear("graph-canvas");
  clear("graph-legend");
  $("graph-empty").hidden = true;
  $("graph-counts").textContent = "读取中";
  clear("graph-detail").append(
    el("h2", "选择节点或关系"),
  );
  $("graph-message").textContent = "正在读取图数据库…";
  let data;
  try {
    data = await api(`/api/graph?${params}`);
  } catch (error) {
    if (request !== graphRequest) return;
    $("graph-message").textContent = "本次图谱查询未成功。";
    $("graph-counts").textContent = "加载失败";
    throw error;
  }
  if (request !== graphRequest) return;
  renderGraph(data);
  $("graph-message").textContent =
    data.has_more ? "结果有截断，可翻页或缩小范围" : data.nodes.length ? "滚轮缩放 · 拖动平移 · 双击展开" : "";
  $("graph-next").disabled = !data.has_more;
  $("graph-prev").disabled = graphOffset === 0;
}
$("graph-zoom-in").addEventListener("click", () => graphView?.zoom(1.25));
$("graph-zoom-out").addEventListener("click", () => graphView?.zoom(1 / 1.25));
$("graph-fit").addEventListener("click", () => graphView?.fit());
$("graph-fullscreen").addEventListener(
  "click",
  guarded(async () => {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.querySelector(".graph-panel").requestFullscreen();
  }),
);
function expandNode(id) {
  graphFocus = id;
  graphOffset = 0;
  for (const name of ["keyword", "entity_type", "relation_type"])
    $("graph-form").elements[name].value = "";
  return loadGraph();
}
$("graph-form").addEventListener(
  "submit",
  guarded(() => {
    graphOffset = 0;
    graphFocus = "";
    return loadGraph();
  }),
);
$("graph-prev").addEventListener(
  "click",
  guarded(() => {
    graphOffset = Math.max(0, graphOffset - 60);
    return loadGraph();
  }),
);
$("graph-next").addEventListener(
  "click",
  guarded(() => {
    graphOffset += 60;
    return loadGraph();
  }),
);
$("graph-reset").addEventListener(
  "click",
  guarded(() => {
    graphOffset = 0;
    graphFocus = "";
    return loadGraph();
  }),
);
$("graph-clear-record").addEventListener("click", guarded(() => {
  $("graph-form").elements.revision_id.value = "";
  graphOffset = 0;
  graphFocus = "";
  return loadGraph();
}));
setInterval(() => {
  if (token && ["results", "review"].includes(active))
    loadRecords(active).catch((e) => notice(e.message));
}, 5000);
