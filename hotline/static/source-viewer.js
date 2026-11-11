"use strict";

// PDF 与扫描图共用受鉴权的页面图像。正文保留在独立可选择文本区，不冒充 PDF 文字层。
class SourceViewer {
  constructor(
    host,
    recordId,
    { json, blob, download, evidence = null, page = 1 },
  ) {
    this.host = host;
    this.recordId = recordId;
    this.json = json;
    this.blob = blob;
    this.download = download;
    this.evidence = evidence;
    this.page = page;
    this.zoom = 1;
    this.rotation = 0;
    this.pan = { x: 0, y: 0 };
    this.sequence = 0;
    this.objectURL = null;
    this.closed = false;
    this.drawShell();
    this.ready = this.init();
  }
  node(tag, text, cls) {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }
  action(text, callback, cls = "secondary") {
    const b = this.node("button", text, cls);
    b.type = "button";
    b.addEventListener("click", () =>
      Promise.resolve()
        .then(callback)
        .catch((e) => this.message(e.message, true)),
    );
    return b;
  }
  message(text, error = false) {
    this.status.textContent = text;
    this.status.classList.toggle("error", error);
  }
  drawShell() {
    const n = (tag, text, cls) => this.node(tag, text, cls);
    this.host.classList.add("source-viewer");
    const heading = n("div", undefined, "reader-heading");
    heading.append(n("strong", "原件审阅"));
    this.method = n("span", "正在打开…", "badge");
    heading.append(this.method);
    this.toolbar = n("div", undefined, "reader-toolbar");
    this.toolbar.setAttribute("aria-label", "原文件阅读控制");
    this.prev = this.action("上一页", () => this.showPage(this.page - 1));
    this.next = this.action("下一页", () => this.showPage(this.page + 1));
    this.pageInput = n("input");
    this.pageInput.type = "number";
    this.pageInput.min = "1";
    this.pageInput.value = this.page;
    this.pageInput.setAttribute("aria-label", "原文件页码");
    this.pageInput.addEventListener("change", () =>
      this.showPage(Number(this.pageInput.value)).catch((e) =>
        this.message(e.message, true),
      ),
    );
    this.pageTotal = n("span", "/ —");
    this.zoomLabel = n("output", "100%");
    this.zoomLabel.setAttribute("aria-label", "阅读缩放比例");
    this.toolbar.append(
      this.prev,
      this.pageInput,
      this.pageTotal,
      this.next,
      this.action("缩小", () => this.setZoom(this.zoom / 1.25)),
      this.zoomLabel,
      this.action("放大", () => this.setZoom(this.zoom * 1.25)),
      this.action("适应", () => {
        this.zoom = 1;
        this.pan = { x: 0, y: 0 };
        this.updateView();
      }),
      this.action("适宽", () => {
        const rect = this.stage.getBoundingClientRect(),
          rotated = this.rotation % 180 !== 0;
        const w = rotated ? this.height : this.width,
          h = rotated ? this.width : this.height;
        this.pan = { x: 0, y: 0 };
        this.setZoom(
          (Math.max((w + 60) / rect.width, (h + 60) / rect.height) *
            rect.width) /
            (w + 60),
        );
        this.pan.y = Math.min(0, (this.svg.viewBox.baseVal.height - h) / 2 - 20);
        this.updateView();
      }),
      this.action("旋转", () => {
        this.rotation = (this.rotation + 90) % 360;
        this.pan = { x: 0, y: 0 };
        this.updateView();
      }),
    );
    const modes = n("div", undefined, "reader-modes");
    this.originalToggle = this.action("原件", () => this.setMode("original"));
    this.textToggle = this.action("识别 / 解析文字", () =>
      this.setMode("text"),
    );
    this.boxToggle = this.action("显示识别框", () => {
      this.showBoxes = !this.showBoxes;
      this.boxToggle.setAttribute("aria-pressed", String(this.showBoxes));
      this.drawBoxes();
    });
    this.boxToggle.setAttribute("aria-pressed", "false");
    modes.append(
      this.originalToggle,
      this.textToggle,
      this.boxToggle,
      this.action("下载原文件", () =>
        this.download(this.recordId, this.info.filename),
      ),
    );
    this.stage = n("div", undefined, "reader-stage");
    this.svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    this.svg.setAttribute("role", "img");
    this.svg.setAttribute("aria-label", "原文件页面");
    this.svg.setAttribute("tabindex", "0");
    this.stage.append(this.svg);
    this.textPanel = n("div", undefined, "reader-text source-fulltext");
    this.textPanel.hidden = true;
    this.status = n("p", "正在加载原文件…", "reader-status");
    this.status.setAttribute("role", "status");
    this.host.replaceChildren(
      heading,
      modes,
      this.toolbar,
      this.stage,
      this.textPanel,
      this.status,
    );
    this.svg.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        this.setZoom(this.zoom * (e.deltaY < 0 ? 1.12 : 1 / 1.12));
      },
      { passive: false },
    );
    this.svg.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || !this.width) return;
      this.drag = { x: e.clientX, y: e.clientY, pan: { ...this.pan } };
      this.svg.setPointerCapture(e.pointerId);
      this.stage.classList.add("dragging");
    });
    this.svg.addEventListener("pointermove", (e) => {
      if (!this.drag) return;
      const rect = this.svg.getBoundingClientRect(),
        vb = this.svg.viewBox.baseVal;
      this.pan = {
        x:
          this.drag.pan.x - ((e.clientX - this.drag.x) * vb.width) / rect.width,
        y:
          this.drag.pan.y -
          ((e.clientY - this.drag.y) * vb.height) / rect.height,
      };
      this.updateView();
    });
    for (const event of ["pointerup", "pointercancel", "lostpointercapture"])
      this.svg.addEventListener(event, () => {
        this.drag = null;
        this.stage.classList.remove("dragging");
      });
    this.svg.addEventListener("keydown", (e) => {
      if (["+", "=", "-", "0"].includes(e.key)) {
        e.preventDefault();
        this.setZoom(
          e.key === "0" ? 1 : this.zoom * (e.key === "-" ? 0.8 : 1.25),
        );
      }
    });
    this.resize = new ResizeObserver(() => this.updateView());
    this.resize.observe(this.stage);
  }
  async init() {
    try {
      [this.info, this.full] = await Promise.all([
        this.json(`/api/source/${this.recordId}/info`),
        this.json(`/api/source/${this.recordId}/text`),
      ]);
      if (this.closed) return;
      this.pageInput.max = this.info.page_count;
      this.pageTotal.textContent = `/ ${this.info.page_count}`;
      this.originalToggle.disabled = !this.info.visual;
      this.page = Math.max(1, Math.min(this.info.page_count || 1, this.page));
      await this.locate();
      this.renderText();
      if (this.info.visual) await this.showPage(this.page);
      else {
        this.setMode("text");
        this.method.textContent = "解析文字";
        this.message(
          this.info.filename.endsWith(".docx")
            ? "此处展示 DOCX 解析文字；原始排版请下载文件查看。"
            : "文本来源：展示已保存的解析正文。",
        );
      }
    } catch (error) {
      if (!this.closed) this.message(error.message, true);
    }
  }
  setMode(mode) {
    this.mode = mode;
    this.stage.hidden = mode !== "original";
    this.toolbar.hidden = mode !== "original";
    this.textPanel.hidden = mode !== "text";
    this.originalToggle.setAttribute(
      "aria-pressed",
      String(mode === "original"),
    );
    this.textToggle.setAttribute("aria-pressed", String(mode === "text"));
    this.boxToggle.hidden =
      mode !== "original" || !this.pageMeta?.lines?.length;
    this.updateView();
  }
  async locate() {
    this.locator = null;
    const evidence = this.evidence;
    const chunk = this.full?.chunks?.find((c) => c.id === evidence?.chunk_id);
    if (!chunk || !evidence?.quote) return;
    let start = evidence.start;
    if (!Number.isInteger(start) || chunk.text.slice(start, start + evidence.quote.length) !== evidence.quote) {
      start = chunk.text.indexOf(evidence.quote);
      if (start < 0 || chunk.text.indexOf(evidence.quote, start + 1) >= 0) return;
    }
    const locator = await this.json(`/api/evidence/${this.recordId}/${chunk.id}/locate?start=${start}&end=${start + evidence.quote.length}`);
    if (!this.closed && evidence === this.evidence) this.locator = locator;
  }
  async showPage(page) {
    if (!Number.isInteger(page) || page < 1 || page > this.info.page_count) {
      this.pageInput.value = this.page;
      throw new Error(`请输入 1 至 ${this.info.page_count} 之间的页码`);
    }
    const sequence = ++this.sequence;
    this.message(`正在打开第 ${page} 页…`);
    this.svg.replaceChildren();
    this.width = null;
    const content = await this.blob(
      `/api/source/${this.recordId}/pages/${page}?size=2200`,
    );
    if (sequence !== this.sequence || this.closed) return;
    const url = URL.createObjectURL(content),
      probe = new Image();
    probe.src = url;
    try {
      await probe.decode();
    } catch {
      URL.revokeObjectURL(url);
      throw new Error("页面图像无法打开");
    }
    if (sequence !== this.sequence || this.closed) {
      URL.revokeObjectURL(url);
      return;
    }
    if (this.objectURL) URL.revokeObjectURL(this.objectURL);
    this.objectURL = url;
    this.width = probe.naturalWidth;
    this.height = probe.naturalHeight;
    this.page = page;
    this.pageInput.value = page;
    this.prev.disabled = page === 1;
    this.next.disabled = page === this.info.page_count;
    this.pageMeta = this.info.pages.find((p) => p.page === page);
    this.rotation = 0;
    this.pan = { x: 0, y: 0 };
    this.zoom = 1;
    const ns = "http://www.w3.org/2000/svg";
    this.group = document.createElementNS(ns, "g");
    const img = document.createElementNS(ns, "image");
    img.setAttribute("href", url);
    img.setAttribute("width", this.width);
    img.setAttribute("height", this.height);
    img.setAttribute("x", -this.width / 2);
    img.setAttribute("y", -this.height / 2);
    this.boxes = document.createElementNS(ns, "g");
    this.group.append(img, this.boxes);
    this.svg.append(this.group);
    this.method.textContent =
      this.pageMeta?.method === "ocr" ? "本地 OCR" : "原文件";
    this.drawBoxes();
    this.setMode("original");
    const confidence = this.pageMeta?.confidence;
    this.message(
      `第 ${page} / ${this.info.page_count} 页 · ${this.info.filename}${confidence != null ? ` · 平均识别置信度 ${(confidence * 100).toFixed(1)}%（非准确率）` : ""}${this.pageMeta?.warnings?.length ? " · " + this.pageMeta.warnings.join("；") : ""}`,
    );
    this.renderText();
  }
  setZoom(value) {
    this.zoom = Math.max(0.4, Math.min(5, value));
    this.updateView();
  }
  updateView() {
    if (!this.width || !this.group) return;
    const rect = this.stage.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const rotated = this.rotation % 180 !== 0;
    const w = rotated ? this.height : this.width,
      h = rotated ? this.width : this.height;
    const scale =
      Math.max((w + 60) / rect.width, (h + 60) / rect.height) / this.zoom;
    const vw = rect.width * scale,
      vh = rect.height * scale;
    this.svg.setAttribute(
      "viewBox",
      `${this.pan.x - vw / 2} ${this.pan.y - vh / 2} ${vw} ${vh}`,
    );
    this.group.setAttribute("transform", `rotate(${this.rotation})`);
    this.zoomLabel.textContent = `${Math.round(this.zoom * 100)}%`;
  }
  drawBoxes() {
    if (!this.boxes) return;
    this.boxes.replaceChildren();
    for (const line of this.pageMeta?.lines || []) {
      const hit = this.locator?.regions.some((r) =>
        r.kind === "line" && r.page === this.page && r.start === line.start && r.end === line.end);
      if (!this.showBoxes && !hit) continue;
      const p = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "polygon",
      );
      p.setAttribute(
        "points",
        line.box
          .map(
            ([x, y]) =>
              `${x * this.width - this.width / 2},${y * this.height - this.height / 2}`,
          )
          .join(" "),
      );
      p.setAttribute("class", hit ? "ocr-box evidence-box" : "ocr-box");
      const title = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "title",
      );
      title.textContent = line.text + (line.confidence == null ? " · 原生文字区域" : ` · ${(line.confidence * 100).toFixed(1)}%`);
      p.append(title);
      this.boxes.append(p);
    }
  }
  renderText() {
    this.textPanel.replaceChildren();
    const chunks = this.full.chunks || [];
    if (!chunks.length)
      this.textPanel.append(
        this.node(
          "p",
          "尚无解析文字。可先阅读原件，待处理完成后重新打开。",
          "muted",
        ),
      );
    for (const chunk of chunks) {
      const block = this.node("section", undefined, "source-chunk");
      block.dataset.chunkId = chunk.id;
      const header = this.node("div", undefined, "source-chunk-heading");
      header.append(
        this.node(
          "strong",
          `${chunk.page ? `第 ${chunk.page} 页 · ` : ""}${chunk.section || "正文"}`,
        ),
      );
      if (chunk.page && this.info.visual)
        header.append(
          this.action("查看这一页原件", () => this.showPage(chunk.page)),
        );
      const text = this.node("pre");
      const quote =
        chunk.id === this.evidence?.chunk_id ? this.evidence.quote : "";
      const offset = quote ? (this.locator?.start ?? chunk.text.indexOf(quote)) : -1;
      if (offset >= 0)
        text.append(
          document.createTextNode(chunk.text.slice(0, offset)),
          this.node("mark", quote),
          document.createTextNode(chunk.text.slice(offset + quote.length)),
        );
      else text.textContent = chunk.text;
      block.append(header, text);
      for (const page of this.info?.pages || []) {
        for (const table of page.tables || []) {
          if ((page.start || 0) + table.start < chunk.start || (page.start || 0) + table.end > chunk.end) continue;
          const grid = this.node("table"), rows = new Map();
          for (const cell of page.cells.filter((c) => c.table === table.id)) {
            if (!rows.has(cell.row)) rows.set(cell.row, this.node("tr"));
            const td = this.node("td");
            td.rowSpan = cell.row_span || 1;
            td.colSpan = cell.column_span || 1;
            if (cell.header) td.append(this.node("strong", `${cell.header}：`));
            const hit = this.locator?.regions.some((r) => r.kind === "cell" && r.page === page.page && r.start === cell.start && r.end === cell.end);
            td.append(this.node(hit ? "mark" : "span", cell.text));
            td.title = `表 ${table.id} · 第 ${cell.row} 行，第 ${cell.column} 列`;
            rows.get(cell.row).append(td);
          }
          grid.append(...rows.values());
          const wrapper = this.node("div", undefined, "scroll-table");
          wrapper.append(this.node("p", `表 ${table.id} · 解析单元格（含上下文）`), grid);
          block.append(wrapper);
        }
      }
      this.textPanel.append(block);
    }
  }
  async reveal(evidence, page) {
    await this.ready;
    if (this.closed) return;
    this.evidence = evidence;
    await this.locate();
    if (evidence !== this.evidence || this.closed) return;
    this.renderText();
    if (this.info?.visual && page) await this.showPage(page);
    else this.setMode("text");
    this.host.scrollIntoView({ behavior: "smooth", block: "nearest" });
    if (this.mode === "text")
      this.textPanel
        .querySelector("mark")
        ?.scrollIntoView({ block: "nearest" });
  }
  destroy() {
    this.closed = true;
    this.sequence++;
    this.resize.disconnect();
    if (this.objectURL) URL.revokeObjectURL(this.objectURL);
  }
}
