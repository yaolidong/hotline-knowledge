"use strict";

// 仅改变浏览器视图和节点布局，不修改图数据库。
class GraphView {
  constructor(canvas, scene, positions, redraw) {
    this.canvas = canvas;
    this.scene = scene;
    this.positions = positions;
    this.redraw = redraw;
    this.scale = 1;
    this.x = this.y = 0;
    this.width = 900;
    this.height = 620;
    this.drag = null;
    this.suppressClick = false;
    this.events = new AbortController();
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(canvas);
    this.resize();
    const options = { signal: this.events.signal };
    canvas.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        this.zoom(Math.exp(-event.deltaY * 0.0015), this.point(event));
      },
      { ...options, passive: false },
    );
    canvas.addEventListener(
      "pointerdown",
      (event) => {
        if (event.button !== 0 || this.drag) return;
        const node = event.target.closest(".graph-node");
        this.suppressClick = false;
        this.drag = {
          id: event.pointerId,
          node: node?.dataset.id,
          last: this.point(event),
          origin: { x: event.clientX, y: event.clientY },
          moved: false,
        };
      },
      options,
    );
    canvas.addEventListener(
      "pointermove",
      (event) => {
        const drag = this.drag;
        if (!drag || event.pointerId !== drag.id) return;
        const p = this.point(event);
        if (
          !drag.moved &&
          Math.hypot(
            event.clientX - drag.origin.x,
            event.clientY - drag.origin.y,
          ) < 4
        )
          return;
        drag.moved = true;
        if (!canvas.hasPointerCapture(event.pointerId))
          canvas.setPointerCapture(event.pointerId);
        canvas.classList.add("dragging");
        const dx = p.x - drag.last.x,
          dy = p.y - drag.last.y;
        if (drag.node) {
          const position = positions.get(drag.node);
          position.x += dx / this.scale;
          position.y += dy / this.scale;
          redraw();
        } else {
          this.x += dx;
          this.y += dy;
          this.apply();
        }
        drag.last = p;
      },
      options,
    );
    const end = (event) => {
      if (!this.drag || event.pointerId !== this.drag.id) return;
      this.suppressClick = this.drag.moved;
      this.drag = null;
      canvas.classList.remove("dragging");
      if (canvas.hasPointerCapture(event.pointerId))
        canvas.releasePointerCapture(event.pointerId);
    };
    canvas.addEventListener("pointerup", end, options);
    canvas.addEventListener("pointercancel", end, options);
    canvas.addEventListener("lostpointercapture", end, options);
    canvas.addEventListener(
      "click",
      (event) => {
        if (this.suppressClick) {
          event.stopImmediatePropagation();
          this.suppressClick = false;
        }
      },
      { ...options, capture: true },
    );
    canvas.addEventListener(
      "keydown",
      (event) => {
        if (["+", "=", "-", "0"].includes(event.key)) {
          event.preventDefault();
          if (event.key === "0") this.fit();
          else this.zoom(event.key === "-" ? 1 / 1.25 : 1.25);
        }
      },
      options,
    );
    this.apply();
  }
  point(event) {
    return new DOMPoint(event.clientX, event.clientY).matrixTransform(
      this.canvas.getScreenCTM().inverse(),
    );
  }
  zoom(factor, center = { x: this.width / 2, y: this.height / 2 }) {
    const next = Math.min(5, Math.max(0.2, this.scale * factor));
    const ratio = next / this.scale;
    this.x = center.x - (center.x - this.x) * ratio;
    this.y = center.y - (center.y - this.y) * ratio;
    this.scale = next;
    this.apply();
  }
  fit() {
    const box = this.scene.getBBox();
    if (!box.width || !box.height) return;
    this.scale = Math.min(
      2,
      Math.max(
        0.2,
        Math.min(
          (this.width - 60) / box.width,
          (this.height - 60) / box.height,
        ),
      ),
    );
    this.x = this.width / 2 - (box.x + box.width / 2) * this.scale;
    this.y = this.height / 2 - (box.y + box.height / 2) * this.scale;
    this.apply();
  }
  resize() {
    const width = this.canvas.clientWidth,
      height = this.canvas.clientHeight;
    if (!width || !height) return;
    // 改变视口尺寸时保留视图中心与真实缩放比例，窄屏可平移探索或选择适应画布。
    this.x += (width - this.width) / 2;
    this.y += (height - this.height) / 2;
    this.width = width;
    this.height = height;
    this.canvas.setAttribute("viewBox", `0 0 ${width} ${height}`);
    this.apply();
  }
  apply() {
    this.scene.setAttribute(
      "transform",
      `translate(${this.x} ${this.y}) scale(${this.scale})`,
    );
    document.getElementById("graph-zoom-level").textContent =
      `${Math.round(this.scale * 100)}%`;
  }
  destroy() {
    if (this.drag && this.canvas.hasPointerCapture(this.drag.id))
      this.canvas.releasePointerCapture(this.drag.id);
    this.events.abort();
    this.resizeObserver.disconnect();
  }
}
