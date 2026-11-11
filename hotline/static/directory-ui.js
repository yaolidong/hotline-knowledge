/* 目录字段映射只决定如何读取原件，不赋予自动发布权限。 */
(() => {
  const section = el("details", undefined, "card directory-import");
  section.append(el("summary", "事项目录 · CSV"), el("p", "UTF-8 CSV，每批最多 50 条，同一地区。Excel 请先导出为 CSV。", "muted small"));
  const file = el("input"), label = el("label", "选择目录文件"), previewButton = button("读取表头与样例", preview, ""), content = el("div");
  file.type = "file"; file.accept = ".csv"; file.id = "directory-file";
  label.append(file); section.append(label, previewButton, content); $("tab-import").append(section);
  let snapshot = null;
  file.addEventListener("change", () => {snapshot = null; content.replaceChildren();});
  async function preview() {
    if (!file.files.length) throw new Error("请选择 CSV 目录文件");
    const selected = file.files[0], body = new FormData(); body.append("file", selected);
    previewButton.disabled = true;
    try {
      const data = await api("/api/import/directory/preview", {method: "POST", body});
      if (file.files[0] !== selected) return;
      snapshot = selected; content.replaceChildren(el("p", `共 ${data.row_count} 条。${data.message}`, "notice"));
      const form = el("form"), fields = el("div", undefined, "form-grid");
      for (const [key, title] of Object.entries(data.fields)) {
        const label = el("label", title + (data.required.includes(key) ? " *" : "（可选）")), select = el("select");
        select.name = key; select.required = data.required.includes(key); select.append(new Option("不映射 / 请选择", ""));
        for (const name of data.headers) select.append(new Option(name, name));
        select.value = data.suggested_mapping[key] || ""; label.append(select); fields.append(label);
      }
      form.append(fields);
      for (const [name, title, required, max] of [["directory_region", "本批实施地区（须与每条记录一致）", true, 100], ["directory_source", "目录来源 / 数据契约说明", true, 1000], ["directory_title", "资料标题（可选）", false, 300]]) {
        const label = el("label", title), input = el("input"); input.name = name; input.required = required; input.maxLength = max; label.append(input); form.append(label);
      }
      const table = el("table"), head = el("tr"), wrapper = el("div", undefined, "scroll-table");
      for (const name of data.headers) head.append(el("th", name)); table.append(head);
      for (const values of data.sample) {const row = el("tr"); for (const value of values) row.append(el("td", value)); table.append(row);}
      wrapper.append(el("p", "原始字段样例（最多 5 条）"), table); form.append(wrapper);
      const submit = el("button", "按映射创建抽取任务"), result = el("div"); submit.type = "submit"; form.append(submit, result);
      form.addEventListener("submit", guarded(async () => {
        if (file.files[0] !== snapshot) throw new Error("文件已更换，请重新读取表头");
        const mapping = Object.fromEntries(Object.keys(data.fields).map(key => [key, form.elements[key].value]).filter(([, value]) => value));
        const body = new FormData(); body.append("file", snapshot);
        body.append("metadata", JSON.stringify({title: form.elements.directory_title.value, region: form.elements.directory_region.value.trim(), source: form.elements.directory_source.value, synthetic: knowledgeSpace === "test", document_type: "guide", directory_mapping: mapping}));
        submit.disabled = true;
        try {
          const response = await api("/api/import/directory", {method: "POST", body});
          result.replaceChildren(el("p", response.duplicate ? "已存在相同目录和映射，复用已有版本。" : "任务已创建，抽取结果需审核后发布。"), button("查看目录任务", async () => {await tab("results"); await loadRevision(response.revision_id, "results");}));
        } finally {submit.disabled = false;}
      })); content.append(form);
    } finally {previewButton.disabled = false;}
  }
})();
