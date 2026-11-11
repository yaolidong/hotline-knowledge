"use strict";

// 编辑器只产生候选修正，保存仍经过服务端 Schema、原文证据与乐观锁校验。
class KnowledgeEditor {
  constructor(host, record, save) {
    this.host = host;
    this.record = record;
    this.value = structuredClone(record.extraction);
    this.save = save;
    this.kind = "entities";
    this.index = 0;
    this.draw();
  }
  field(parent, title, value, change, multiline = false) {
    const label = el("label", title), input = el(multiline ? "textarea" : "input");
    input.value = value ?? "";
    input.setAttribute("aria-label", title);
    if (multiline) input.rows = 3;
    input.addEventListener("input", () => change(input.value));
    label.append(input); parent.append(label); return input;
  }
  select(parent, title, options, value, change) {
    const label = el("label", title), select = el("select");
    select.setAttribute("aria-label", title);
    for (const [key, name] of options) {
      const option = el("option", name); option.value = key; select.append(option);
    }
    select.value = value;
    select.addEventListener("change", () => change(select.value));
    label.append(select); parent.append(label); return select;
  }
  draw() {
    this.host.replaceChildren();
    this.host.append(el("p", "修改后点击保存，并填写上方审核说明。名称、属性和逻辑依据须来自原文；保存后回到待审核。", "muted"));
    const tabs = el("div", undefined, "actions");
    for (const [key, title] of [["entities", "实体"], ["relations", "关系"]])
      tabs.append(button(`${title}（${this.value[key].length}）`, () => {this.kind = key; this.index = 0; this.draw();}, this.kind === key ? "" : "secondary"));
    this.host.append(tabs);
    const items = this.value[this.kind];
    if (items.length) {
      this.index = Math.min(this.index, items.length - 1);
      this.select(this.host, "选择编辑对象", items.map((item, i) => [String(i), this.kind === "entities" ? `${schema.entities[item.type]?.label || item.type} · ${item.name}` : `${this.name(item.source)} — ${relationLabel(item.type)} → ${this.name(item.target)}`]), String(this.index), value => {this.index = Number(value); this.draw();});
      const item = items[this.index], fields = el("div", undefined, "editor-fields");
      this.host.append(fields);
      if (this.kind === "entities") this.entityFields(fields, item); else this.relationFields(fields, item);
      this.evidenceFields(fields, item);
      fields.append(button("移除当前对象（保存后生效）", () => {
        if (this.kind === "entities" && this.value.relations.some(r => r.source === item.id || r.target === item.id))
          throw new Error("该实体仍有关联关系，请先修正或移除关系");
        items.splice(this.index, 1); this.index = Math.max(0, this.index - 1); this.draw();
      }));
    }
    this.host.append(button(this.kind === "entities" ? "新增实体" : "新增关系", () => {
      const chunk = this.record.manualChunk;
      const evidence = chunk ? [{chunk_id: chunk.id, quote: chunk.text, start: 0, end: chunk.text.length}] : [];
      items.push(this.kind === "entities" ? {id: crypto.randomUUID().replaceAll("-", ""), type: "ServiceItem", name: "", aliases: [], attributes: {}, evidence, rule: null} : {source: "", target: "", type: Object.keys(schema.relations)[0], evidence});
      this.index = items.length - 1; this.draw();
    }));
    this.host.append(button("检查修改摘要", () => {
      this.host.querySelector(".editor-diff")?.remove();
      const box = el("div", undefined, "editor-diff review-instruction");
      const before = this.record.extraction;
      for (const key of ["entities", "relations"]) {
        const changed = this.value[key].filter((item, i) => JSON.stringify(item) !== JSON.stringify(before[key][i])).length;
        box.append(el("p", `${key === "entities" ? "实体" : "关系"}：${before[key].length} → ${this.value[key].length}，${changed} 项新增或变化。删除、调整顺序也可能改变对应位置，请对照原件核对。`));
      }
      this.host.append(box);
    }));
    this.host.append(button("保存表单修正并重新待审", async () => {
      for (const item of [...this.value.entities, ...this.value.relations]) {
        for (const evidence of item.evidence) {
          const chunk = this.record.chunks.find(c => c.id === evidence.chunk_id);
          if (!chunk || !Number.isInteger(evidence.start) || chunk.text.slice(evidence.start, evidence.end) !== evidence.quote || !evidence.quote)
            throw new Error("证据引文或偏移不匹配，请逐项核对原文");
        }
      }
      await this.save(this.value);
    }, ""));
  }
  name(id) { return [...this.value.entities, ...(this.record.contextEntities || [])].find(e => e.id === id)?.name || "未选择"; }
  entityFields(host, item) {
    this.select(host, "实体类型", Object.entries(schema.entities).map(([key, value]) => [key, value.label]), item.type, value => {
      item.type = value;
      item.attributes = Object.fromEntries(Object.entries(item.attributes).filter(([key]) => schema.entities[value].attributes.includes(key)));
      if (!schema.rule_entities.includes(value)) item.rule = null;
      this.draw();
    });
    this.field(host, "原文名称", item.name, value => item.name = value, true);
    this.field(host, "短展示名（须来自证据，可空）", item.display_name, value => item.display_name = value);
    this.field(host, "原文明示别名（每行一个）", (item.aliases || []).join("\n"), value => item.aliases = value.split("\n").map(x => x.trim()).filter(Boolean), true);
    const attributes = el("div", undefined, "form-grid");
    const labels = {content: "内容", copies: "份数", format: "材料形式", fees: "费用", time_limit: "办理时限", applicants: "适用对象", applicant_scope: "申请主体", service_code: "事项编码", level: "层级", address: "地址", telephone: "电话", order: "明示序号", document_number: "文号", provision_number: "条款编号", issued_on: "发布日期", effective_on: "施行日期", expires_on: "失效日期", repealed_on: "废止日期", document_type: "文件类型", channel_type: "渠道类型", url: "网址", region_code: "区划代码"};
    for (const key of schema.entities[item.type].attributes)
      this.field(attributes, labels[key] || key, item.attributes[key], value => {if (value) item.attributes[key] = value; else delete item.attributes[key];});
    host.append(attributes);
    if (item.type === "ProcedureStep") this.field(host, "原文明示的流程链（可空）", item.sequence_basis, value => item.sequence_basis = value, true);
    if (schema.rule_entities.includes(item.type)) {
      const ruleHost = el("div", undefined, "rule-builder");
      host.append(el("h4", "条件 / 材料逻辑"), ruleHost);
      if (item.rule) {
        this.ruleFields(ruleHost, item.rule, value => item.rule = value, 0);
        host.append(button("移除逻辑，保留原文待核对", () => {item.rule = null; this.draw();}));
      } else host.append(button("添加有原文依据的逻辑", () => {item.rule = {operator: "AND", conditions: [{condition: ""}, {condition: ""}]}; this.draw();}));
    }
  }
  ruleFields(host, rule, replace, depth) {
    const box = el("fieldset", undefined, "rule-node"); host.append(box);
    this.select(box, "逻辑", [["LEAF", "条件原文"], ["AND", "全部满足 AND"], ["OR", "任一满足 OR"], ["NOT", "排除 NOT"]], rule.operator || "LEAF", value => {
      replace(value === "LEAF" ? {condition: ""} : {operator: value, conditions: Array.from({length: value === "NOT" ? 1 : 2}, () => ({condition: ""}))});
      this.draw();
    });
    if (!rule.operator) this.field(box, "逐字条件", rule.condition, value => rule.condition = value);
    else {
      this.field(box, "连接逻辑的原文依据", rule.basis, value => {if (value) rule.basis = value; else delete rule.basis;}, true);
      for (const [index, child] of rule.conditions.entries()) {
        if (depth >= 10) {box.append(el("p", "已到嵌套上限，请简化表达或使用高级编辑")); break;}
        this.ruleFields(box, child, value => rule.conditions[index] = value, depth + 1);
        if (rule.conditions.length > 2 && rule.operator !== "NOT") box.append(button(`移除第 ${index + 1} 项`, () => {rule.conditions.splice(index, 1); this.draw();}));
      }
      if (rule.operator !== "NOT" && rule.conditions.length < 100) box.append(button("添加子条件", () => {rule.conditions.push({condition: ""}); this.draw();}));
    }
  }
  relationFields(host, item) {
    this.select(host, "关系类型", Object.entries(schema.relations).map(([key, value]) => [key, value.label]), item.type, value => {item.type = value; this.draw();});
    for (const [key, title, allowed] of [["source", "源实体", "sources"], ["target", "目标实体", "targets"]]) {
      const choices = [...this.value.entities, ...(this.record.contextEntities || [])].filter(e => schema.relations[item.type][allowed].includes(e.type));
      if (!choices.some(e => e.id === item[key])) item[key] = "";
      this.select(host, title, [["", "请选择"], ...choices.map(e => [e.id, e.name || "未命名实体"])], item[key], value => item[key] = value);
    }
  }
  evidenceFields(host, item) {
    host.append(el("h4", "原文证据"));
    for (const [index, evidence] of item.evidence.entries()) {
      const box = el("fieldset", undefined, "evidence-editor"); host.append(box);
      this.select(box, `证据 ${index + 1} · 原文块`, [["", "请选择原文块"], ...this.record.chunks.map(c => [c.id, `${c.page ? "第" + c.page + "页 · " : ""}${c.section || "正文"} · ${c.text.slice(0, 45)}`])], evidence.chunk_id, value => {evidence.chunk_id = value; evidence.quote = ""; evidence.start = null; evidence.end = null; this.draw();});
      const chunk = this.record.chunks.find(c => c.id === evidence.chunk_id);
      if (chunk) { const context = el("details"); context.append(el("summary", "展开原文块并选择引文"), el("pre", chunk.text)); box.append(context); }
      const update = () => {
        const position = Number(offset.value);
        evidence.start = offset.value !== "" && Number.isInteger(position) ? position : null;
        evidence.end = evidence.start === null ? null : evidence.start + evidence.quote.length;
      };
      this.field(box, "逐字引文", evidence.quote, value => {
        evidence.quote = value;
        const at = chunk?.text.indexOf(value) ?? -1;
        offset.value = value && at >= 0 && chunk.text.indexOf(value, at + 1) < 0 ? String(at) : "";
        update();
      }, true);
      const offset = this.field(box, "块内起始位置（重复引文需核对位置）", evidence.start, update);
      offset.type = "number"; offset.min = "0";
      box.append(button("定位原件", () => openEvidence(evidence, this.record.id)), button("移除此证据", () => {item.evidence.splice(index, 1); this.draw();}));
    }
    if (item.evidence.length < 30) host.append(button("增加证据", () => {item.evidence.push({chunk_id: "", quote: "", start: null, end: null}); this.draw();}));
  }
}
