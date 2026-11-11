"use strict";

const factStates = {PENDING: "待核对", ACCEPTED: "已确认", RETURNED: "已退回", REJECTED: "已拒绝", DRAFT: "待审核", APPROVED: "已审核", PUBLISHED: "已发布", PUBLISHING: "发布中", RETIRED: "已停用"};

function reviewNote(host, label = "处理依据") {
  const wrapper = el("label", label), input = el("textarea");
  input.rows = 2; input.maxLength = 1000; input.setAttribute("aria-label", label);
  wrapper.append(input); host.append(wrapper);
  return () => {
    if (!input.value.trim()) requireReviewField(input, `请填写${label}。`);
    return input.value.trim();
  };
}

function openFactEditor(host, record, item, reload) {
  host.replaceChildren();
  const note = reviewNote(host, "修改依据"), form = el("div"); host.append(form);
  const editor = new KnowledgeEditor(form, record, async extraction => {
    await api(`/api/revisions/${record.id}/extraction`, {method: "PUT", body: JSON.stringify({expected_version: record.version, note: note(), extraction})});
    await reload(); notice("修正已保存，事实组需重新审核。");
  });
  if (item) {
    editor.kind = item.kind === "entity" ? "entities" : "relations";
    editor.index = editor.value[editor.kind].findIndex(value => item.kind === "entity" ? value.id === item.value.id : value.source === item.value.source && value.target === item.value.target && value.type === item.value.type);
    editor.draw();
  }
  host.scrollIntoView({block: "start"});
}

function openManualBlock(host, record, chunk, reload) {
  host.replaceChildren(); host.append(el("h3", `人工补录 · ${chunk.section || "原文块"}`));
  const source = el("details"); source.append(el("summary", "核对本块原文"), el("pre", chunk.text)); host.append(source);
  const note = reviewNote(host, "补录依据");
  const emptyLabel = el("label", undefined, "check"), empty = el("input"); empty.type = "checkbox";
  empty.setAttribute("aria-label", "本块无可抽取事实"); emptyLabel.append(empty, document.createTextNode("本块无可抽取事实")); host.append(emptyLabel);
  const form = el("div"); host.append(form);
  new KnowledgeEditor(form, {...record, extraction: {schema_version: 2, entities: [], relations: []}, contextEntities: record.extraction?.entities || [], manualChunk: chunk}, async extraction => {
    await api(`/api/revisions/${record.id}/chunks/${chunk.id}/manual`, {method: "PUT", body: JSON.stringify({expected_version: record.version, note: note(), no_facts: empty.checked, extraction})});
    await reload(); notice("人工补录已保存，仍需事实组审核。");
  });
  host.scrollIntoView({block: "start"});
}

async function renderFactReview(host, record, reload) {
  const reloadRecord = reload;
  reload = async () => {
    const groups = [...host.querySelectorAll('.fact-group[open]')].map(node => node.dataset.groupId);
    const items = [...host.querySelectorAll('.fact-row[open]')].map(node => node.dataset.factKey);
    await reloadRecord();
    for (const id of groups) document.querySelectorAll(`[data-group-id="${CSS.escape(id)}"]`).forEach(node => node.open = true);
    for (const key of items) document.querySelectorAll(`[data-fact-key="${CSS.escape(key)}"]`).forEach(node => node.open = true);
  };
  const data = await api(`/api/revisions/${record.id}/fact-review`);
  if (!host.isConnected) return;
  if (data.version !== record.version) throw new Error("资料已更新，请刷新详情。");
  const canReview = ["admin", "reviewer"].includes(user.role), canEdit = ["admin", "maintainer"].includes(user.role);
  host.append(el("h3", "事实组审核"));
  const editorHost = el("div", undefined, "fact-editor-host");
  if (data.frozen) {
    host.append(el("p", "已有发布快照；修改内容需新建修订草稿。", "notice"));
    if (canEdit) {
      const note = reviewNote(host, "修订依据");
      host.append(button("新建修订草稿", async () => {
        const copy = await api(`/api/revisions/${record.id}/review-copy`, {method: "POST", body: JSON.stringify({expected_version: record.version, note: note()})});
        await loadRevision(copy.id, "review"); notice("已复制原文和抽取结果，历史发布快照保留。");
      }));
    }
  }
  const failed = record.chunks.filter(c => record.parsed_metadata?._extraction?.[c.id]?.status === "FAILED");
  if (failed.length) {
    const panel = el("details"); panel.open = !data.groups.length;
    panel.append(el("summary", `待补录 · ${failed.length} 块`));
    for (const chunk of failed) {
      const row = el("div", undefined, "fact-row");
      row.append(el("strong", `${chunk.page ? `第 ${chunk.page} 页 · ` : ""}${chunk.section || "正文"}`), el("p", chunk.text.slice(0, 180)));
      if (canEdit && !data.frozen) row.append(button("补录此块", () => openManualBlock(editorHost, record, chunk, reload)));
      panel.append(row);
    }
    host.append(panel);
  }
  if (!data.groups.length) host.append(el("p", "暂无可审核事实组；请先补录或修正抽取结果。", "empty-state"));
  for (const group of data.groups) {
    const panel = el("details", undefined, "fact-group"); panel.dataset.groupId = group.id;
    const status = group.publication_status || group.status;
    panel.append(el("summary", `${group.title} · ${factStates[status]} · ${group.item_keys.length} 项`));
    const rows = el("div"); panel.append(rows);
    for (const item of data.items.filter(i => group.item_keys.includes(i.key))) {
      const row = el("details", undefined, "fact-row"); row.dataset.factKey = item.key;
      row.append(el("summary", `${item.kind === "entity" ? "实体" : "关系"} · ${item.label} · ${factStates[item.status]}`));
      row.append(evidenceView(item.value.evidence, record.id));
      if (item.decision.note) row.append(el("p", item.decision.note));
      if (item.value.attributes) row.append(attributeView(item.value.attributes));
      if (item.value.rule) row.append(details("条件逻辑", item.value.rule));
      if (!data.frozen) {
        if (canReview) {
          const note = reviewNote(row);
          const actions = el("div", undefined, "actions");
          for (const [action, label] of [["accept", "确认此条"], ["return", "退回修正"], ["reject", "拒绝此条"]]) actions.append(button(label, async () => {
            await api(`/api/revisions/${record.id}/facts/${encodeURIComponent(item.key)}/decision`, {method: "POST", body: JSON.stringify({expected_version: record.version, action, note: note()})});
            await reload(); notice(action === "return" ? "已退回；相关事实组暂不能发布。" : "条目处理已保存。");
          }));
          row.append(actions);
        }
        if (canEdit) row.append(button("修正此条", () => openFactEditor(editorHost, record, item, reload)));
      }
      rows.append(row);
    }
    const snapshotId = group.decision.publication_revision_id;
    if (snapshotId) panel.append(button("查看发布快照", async () => {await loadRevision(snapshotId, "review");}));
    if (snapshotId && status === "PUBLISHED") panel.append(button("查看此组图谱", async () => showRevisionGraph(await api(`/api/revisions/${snapshotId}`))));
    if (canReview && !snapshotId) {
      const coverage = el("details", undefined, "coverage-review");
      coverage.append(el("summary", `全文覆盖核对 · ${record.chunks.length} 块`));
      const controls = {};
      for (const chunk of record.chunks) {
        const entry = el("details"), included = group.chunk_ids.includes(chunk.id);
        entry.append(el("summary", `${included ? "本组依据" : "核对关联"} · ${chunk.page ? `第 ${chunk.page} 页 · ` : ""}${chunk.section || "正文"}`), el("pre", chunk.text));
        const select = el("select"); select.dataset.coverageChunk = chunk.id;
        select.setAttribute("aria-label", `条款归属 ${chunk.id}`);
        select.append(new Option("请选择核对结论", ""), new Option(included ? "已核对，本组依据完整" : "与本组无关，已检查引用与例外", included ? "included" : "unrelated"));
        entry.append(select);
        const reason = el("input"); reason.maxLength = 1000; reason.placeholder = "非本组条款的排除依据";
        reason.setAttribute("aria-label", `排除依据 ${chunk.id}`); reason.hidden = included;
        entry.append(reason); controls[chunk.id] = {select, reason, included}; coverage.append(entry);
      }
      panel.append(coverage);
      const checks = {};
      for (const [key, title] of Object.entries(data.checks)) {
        const label = el("label", undefined, "check"), input = el("input"); input.type = "checkbox";
        input.dataset.groupCheck = key; label.append(input, document.createTextNode(title)); panel.append(label); checks[key] = input;
      }
      for (const issue of group.quality.issues) {
        const block = el("div", undefined, "review-instruction"); block.append(el("p", `${issue.name}：${issue.message}`));
        const select = el("select"); select.dataset.qualityIssue = issue.id;
        for (const [key, label] of [["unresolved", "暂不能确认"], ["verified", "已对照原件确认"], ["false_positive", "规则误报"]]) select.append(new Option(label, key));
        const reason = el("input"); reason.dataset.issueReason = issue.id; reason.maxLength = 500; reason.placeholder = "处置理由（必填）";
        block.append(select, reason); panel.append(block);
      }
      const note = reviewNote(panel, "事实组审核依据"), actions = el("div", undefined, "actions");
      actions.append(button("审核通过此组", async () => {
        const payload = {action: "approve", expected_version: record.version, note: note(), checks: {}, coverage: {}, issue_resolutions: reviewResolutions(panel, {quality: {...group.quality, requires_acknowledgement: true}})};
        for (const [key, input] of Object.entries(checks)) {
          if (!input.checked) requireReviewField(input, "请完成事实组完整性核对。");
          payload.checks[key] = true;
        }
        for (const [cid, control] of Object.entries(controls)) {
          if (!control.select.value) {coverage.open = true; control.select.closest("details").open = true; requireReviewField(control.select, "请逐块确认条款归属。");}
          if (!control.included && !control.reason.value.trim()) {coverage.open = true; control.reason.closest("details").open = true; requireReviewField(control.reason, "请说明非本组条款的排除依据。");}
          payload.coverage[cid] = {status: control.select.value, note: control.reason.value.trim()};
        }
        await api(`/api/revisions/${record.id}/groups/${group.id}/review`, {method: "POST", body: JSON.stringify(payload)});
        await reload(); notice("事实组已审核，可发布此组。");
      }, ""), button("退回此组", async () => {
        await api(`/api/revisions/${record.id}/groups/${group.id}/review`, {method: "POST", body: JSON.stringify({action: "return", expected_version: record.version, note: note()})});
        await reload(); notice("事实组已退回修正。");
      }));
      panel.append(actions);
    }
    if (canReview && group.status === "APPROVED" && !["PUBLISHED", "PUBLISHING", "RETIRED"].includes(group.publication_status)) {
      panel.append(button(snapshotId ? "重试发布此组" : "发布此组", async () => {
        notice("正在写入事实组并回读图数据库…");
        try {
          const published = await api(`/api/revisions/${record.id}/groups/${group.id}/publish`, {method: "POST", body: JSON.stringify({expected_version: record.version, note: group.decision.note})});
          await reload(); await showRevisionGraph(published); notice("事实组已发布，图写入与回读通过。");
        } catch (error) {await reload(); throw error;}
      }, ""));
    }
    host.append(panel);
  }
  if (canEdit && !data.frozen) host.append(button("补充或修正事实", () => openFactEditor(editorHost, record, null, reload)));
  host.append(editorHost);
}
