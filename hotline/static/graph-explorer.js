"use strict";

// 仅布局与选择视图；节点、关系和证据始终使用 /api/graph 的真实查询结果。
let graphSnapshot = null;
function renderGraph(raw) {
  graphSnapshot = raw;
  graphView?.destroy();
  clear("graph-canvas");
  clear("graph-legend");
  const visible = new Set(
    raw.nodes
      .filter(
        (n) =>
          $("graph-sources").checked ||
          !["SourceDocument", "Chunk"].includes(n.entityType),
      )
      .map((n) => n.id),
  );
  const data = {
    nodes: raw.nodes.filter((n) => visible.has(n.id)),
    edges: raw.edges.filter(
      (e) => visible.has(e.source) && visible.has(e.target),
    ),
  };
  const canvas = $("graph-canvas"),
    scene = svg("g", { id: "graph-scene" });
  const defs = svg("defs", {}),
    marker = svg("marker", {
      id: "graph-arrow",
      viewBox: "0 0 10 10",
      refX: 9,
      refY: 5,
      markerWidth: 5,
      markerHeight: 5,
      orient: "auto-start-reverse",
    });
  marker.append(svg("path", { d: "M0 0L10 5L0 10Z", fill: "#6d8295" }));
  defs.append(marker);
  canvas.append(defs, scene);
  $("graph-empty").hidden = data.nodes.length > 0;
  $("graph-empty").querySelector("p").textContent = "当前范围没有匹配的知识";
  $("graph-counts").textContent =
    `${data.nodes.length} 节点 / ${data.edges.length} 关系`;
  const kinds = [...Object.keys(schema.entities), "SourceDocument", "Chunk"];
  const typeLabel = (kind) =>
    schema.entities[kind]?.label ||
    { SourceDocument: "源文件", Chunk: "原文片段" }[kind] ||
    kind;
  const colors = getComputedStyle(document.documentElement);
  const color = (kind) =>
    colors
      .getPropertyValue(
        `--graph-color-${Math.max(0, kinds.indexOf(kind)) % 12}`,
      )
      .trim();
  const sourceType = (node) =>
    ["SourceDocument", "Chunk"].includes(node.entityType);
  const radius = (node) =>
    node.entityType === "ServiceItem" ? 32 : sourceType(node) ? 21 : 26;
  const nodes = new Map(data.nodes.map((node) => [node.id, node]));
  const positions = new Map(
    data.nodes.map((node) => [node.id, { id: node.id, x: 450, y: 310 }]),
  );
  const edges = data.edges.filter(
    (edge) => nodes.has(edge.source) && nodes.has(edge.target),
  );
  const nodeViews = [],
    edgeViews = [],
    legendViews = [];
  let selectedNode = null,
    selectedType = null,
    selectedEdge = null;

  function highlight() {
    const activeNodes = selectedNode ? new Set([selectedNode]) : null;
    if (activeNodes)
      for (const e of edges) {
        if (e.source === selectedNode) activeNodes.add(e.target);
        if (e.target === selectedNode) activeNodes.add(e.source);
      }
    for (const { n, g } of nodeViews) {
      g.classList.toggle(
        "is-selected",
        n.id === selectedNode || n.entityType === selectedType,
      );
      g.classList.toggle(
        "is-muted",
        !!(
          (activeNodes && !activeNodes.has(n.id)) ||
          (selectedType && n.entityType !== selectedType) ||
          (selectedEdge &&
            ![selectedEdge.source, selectedEdge.target].includes(n.id))
        ),
      );
      g.setAttribute(
        "aria-pressed",
        String(n.id === selectedNode || n.entityType === selectedType),
      );
    }
    for (const { e, group } of edgeViews) {
      const on =
        (selectedNode && [e.source, e.target].includes(selectedNode)) ||
        selectedEdge === e;
      group.classList.toggle("is-selected", !!on);
      group.classList.toggle(
        "is-muted",
        !!(
          (selectedNode && !on) ||
          (selectedEdge && !on) ||
          (selectedType &&
            ![
              nodes.get(e.source).entityType,
              nodes.get(e.target).entityType,
            ].includes(selectedType))
        ),
      );
    }
    for (const { kind, button: b } of legendViews)
      b.setAttribute("aria-pressed", String(selectedType === kind));
  }
  function emptyInspector() {
    clear("graph-detail").append(
      el("h2", "选择节点或关系"),
      el("p", "双击节点展开邻域", "muted small"),
    );
  }
  function clearSelection() {
    selectedNode = selectedType = selectedEdge = null;
    highlight();
    emptyInspector();
  }
  emptyInspector();
  for (const kind of kinds.filter((kind) =>
    data.nodes.some((node) => node.entityType === kind),
  )) {
    const count = data.nodes.filter((node) => node.entityType === kind).length;
    const b = button(
      "",
      () => {
        selectedType = selectedType === kind ? null : kind;
        selectedNode = selectedEdge = null;
        highlight();
        emptyInspector();
      },
      "graph-legend-chip",
    );
    b.setAttribute("aria-pressed", "false");
    b.title = `高亮${typeLabel(kind)}，当前 ${count} 个`;
    const icon = svg("svg", {
      viewBox: "0 0 12 12",
      width: 12,
      height: 12,
      "aria-hidden": "true",
    });
    icon.append(svg("circle", { cx: 6, cy: 6, r: 5, fill: color(kind) }));
    b.append(icon, el("span", typeLabel(kind)), el("b", count));
    $("graph-legend").append(b);
    legendViews.push({ kind, button: b });
  }
  const groups = new Map();
  for (const e of edges) {
    const key = JSON.stringify([e.source, e.target].sort());
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(e);
  }
  for (const e of edges) {
    const group = svg("g", { class: "graph-relationship" });
    const path = svg("path", {
      class: "graph-edge",
      "marker-end": "url(#graph-arrow)",
      fill: "none",
      tabindex: 0,
      role: "button",
      "aria-label": `${nodes.get(e.source).name}，${relationLabel(e.type)}，${nodes.get(e.target).name}`,
    });
    if (["SUPPORTED_BY", "HAS_CHUNK"].includes(e.type))
      group.classList.add("is-provenance");
    const hit = svg("path", { class: "graph-edge-hit", fill: "none" });
    const label = svg("text", { class: "edge-label", "text-anchor": "middle" });
    label.textContent = relationLabel(e.type);
    const inspect = () => {
      selectedEdge = e;
      selectedNode = selectedType = null;
      highlight();
      const panel = clear("graph-detail");
      panel.append(
        el("p", "关系", "eyebrow"),
        el("h2", relationLabel(e.type)),
        el(
          "p",
          `${nodes.get(e.source).name} → ${nodes.get(e.target).name}`,
          "relation-endpoints",
        ),
        evidenceView(e.properties?.evidence, e.properties?.recordId),
        details("技术属性", e.properties),
      );
    };
    group.addEventListener("click", inspect);
    path.addEventListener("keydown", (event) => {
      if (["Enter", " "].includes(event.key)) {
        event.preventDefault();
        inspect();
      }
    });
    group.append(hit, path, label);
    scene.append(group);
    const peers = groups.get(JSON.stringify([e.source, e.target].sort()));
    const bend =
      (peers.indexOf(e) - (peers.length - 1) / 2) *
      38 *
      (e.source < e.target ? 1 : -1);
    edgeViews.push({ e, group, path, hit, label, bend });
  }
  for (const n of data.nodes) {
    const g = svg("g", {
      class: "graph-node",
      tabindex: 0,
      role: "button",
      "aria-label": n.name,
      "data-id": n.id,
    });
    const circle = svg("circle", {
      cx: 0,
      cy: 0,
      r: radius(n),
      fill: color(n.entityType),
    });
    const glyph = svg("text", {
      class: "node-glyph",
      "text-anchor": "middle",
      "aria-hidden": "true",
    });
    glyph.textContent =
      {
        PolicyDocument: "文",
        PolicyProvision: "条",
        GovernmentDepartment: "部",
        ServiceItem: "事",
        EligibilityCondition: "条",
        RequiredMaterial: "材",
        ProcedureStep: "流",
        ServiceChannel: "渠",
        AdministrativeRegion: "区",
        LegalBasis: "据",
        SourceDocument: "源",
        Chunk: "段",
      }[n.entityType] || typeLabel(n.entityType).slice(0, 1);
    const label = svg("text", {
      class: "node-caption",
      "text-anchor": "middle",
    });
    const title = svg("title", {});
    title.textContent = `${n.name} · ${typeLabel(n.entityType)}`;
    const chars = Array.from(n.name),
      lines =
        chars.length > 11
          ? [
              chars.slice(0, 11).join(""),
              chars.slice(11, 22).join("") + (chars.length > 22 ? "…" : ""),
            ]
          : [n.name];
    for (const line of lines) {
      const span = svg("tspan", {});
      span.textContent = line;
      label.append(span);
    }
    g.append(circle, glyph, label, title);
    const inspect = () => {
      selectedNode = n.id;
      selectedType = selectedEdge = null;
      highlight();
      const panel = clear("graph-detail");
      panel.append(
        el("p", typeLabel(n.entityType), "eyebrow"),
        el("h2", n.name),
        el("p", n.region || "地区未标注", "muted"),
      );
      if (n.reviewStatus)
        panel.append(
          el("span", stateLabels[n.reviewStatus] || n.reviewStatus, "badge"),
        );
      panel.append(attributeView(n.attrs));
      if (n.rule) panel.append(details("完整条件逻辑", n.rule));
      const actions = el("div", undefined, "inspector-actions");
      actions.append(button("展开邻接节点", () => expandNode(n.id)));
      if (n.recordId) {
        actions.append(
          button("阅读源文件", () => openSource(n.recordId)),
          button("下载源文件", async () => {
            const info = await api(`/api/source/${n.recordId}/info`);
            await downloadSource(n.recordId, info.filename);
          }),
        );
      }
      panel.append(
        actions,
        el("h3", "原文依据"),
        evidenceView(
          n.evidence?.length
            ? n.evidence
            : n.entityType === "Chunk"
              ? [{ chunk_id: n.id, quote: n.content || "" }]
              : [],
          n.recordId,
        ),
        details("全部属性与来源信息", n),
      );
    };
    g.addEventListener("click", inspect);
    g.addEventListener("keydown", (event) => {
      if (["Enter", " "].includes(event.key)) {
        event.preventDefault();
        inspect();
      }
    });
    g.addEventListener(
      "dblclick",
      guarded(() => expandNode(n.id)),
    );
    scene.append(g);
    nodeViews.push({ n, g, circle, glyph, label });
  }
  function redraw() {
    for (const { n, circle, glyph, label } of nodeViews) {
      const p = positions.get(n.id);
      circle.setAttribute("cx", p.x);
      circle.setAttribute("cy", p.y);
      glyph.setAttribute("x", p.x);
      glyph.setAttribute("y", p.y + 5);
      Array.from(label.children).forEach((line, i) => {
        line.setAttribute("x", p.x);
        line.setAttribute("y", p.y + radius(n) + 19 + i * 16);
      });
    }
    for (const { e, path, hit, label, bend } of edgeViews) {
      const a = positions.get(e.source),
        b = positions.get(e.target);
      const dx = b.x - a.x,
        dy = b.y - a.y,
        length = Math.hypot(dx, dy) || 1;
      const x1 = a.x + (dx / length) * (radius(nodes.get(e.source)) + 3),
        y1 = a.y + (dy / length) * (radius(nodes.get(e.source)) + 3);
      const x2 = b.x - (dx / length) * (radius(nodes.get(e.target)) + 5),
        y2 = b.y - (dy / length) * (radius(nodes.get(e.target)) + 5);
      const cx = (a.x + b.x) / 2 - (dy / length) * bend,
        cy = (a.y + b.y) / 2 + (dx / length) * bend;
      const d =
        e.source === e.target
          ? `M${a.x - 20},${a.y - 20} C${a.x - 95},${a.y - 120} ${a.x + 95},${a.y - 120} ${a.x + 20},${a.y - 20}`
          : `M${x1},${y1} Q${cx},${cy} ${x2},${y2}`;
      path.setAttribute("d", d);
      hit.setAttribute("d", d);
      // 保留几何端点便于调试及拖拽契约验证。
      path.setAttribute("x1", x1);
      path.setAttribute("y1", y1);
      label.setAttribute(
        "x",
        e.source === e.target ? a.x : (a.x + b.x) / 4 + cx / 2,
      );
      label.setAttribute(
        "y",
        e.source === e.target ? a.y - 90 : (a.y + b.y) / 4 + cy / 2 - 5,
      );
    }
  }
  function layout() {
    const points = [...positions.values()];
    if ($("graph-layout-mode").value === "radial") {
      points.forEach((p, i) => {
        p.x =
          450 +
          Math.max(220, points.length * 18) *
            Math.cos((2 * Math.PI * i) / points.length);
        p.y =
          310 +
          Math.max(220, points.length * 18) *
            Math.sin((2 * Math.PI * i) / points.length);
      });
    } else {
      // 有上限的静态布局，不启动持续动画；不修改数据库对象。
      for (const p of points) {
        delete p.x;
        delete p.y;
        p.vx = p.vy = 0;
      }
      d3.forceSimulation(points)
        .stop()
        .force(
          "link",
          d3
            .forceLink(
              edges.map((e) => ({ source: e.source, target: e.target })),
            )
            .id((p) => p.id)
            .distance(175)
            .strength(0.35),
        )
        .force("charge", d3.forceManyBody().strength(-650))
        .force("collide", d3.forceCollide(76).iterations(2))
        .force("center", d3.forceCenter(450, 310))
        .tick(260);
    }
    redraw();
    graphView?.fit();
  }
  graphView = new GraphView(canvas, scene, positions, redraw);
  graphView.relayout = layout;
  graphView.clearSelection = clearSelection;
  layout();
  highlight();
  canvas.classList.toggle("hide-edge-labels", !$("graph-labels").checked);
}
$("graph-layout-mode").addEventListener("change", () => graphView?.relayout());
$("graph-labels").addEventListener("change", () =>
  $("graph-canvas").classList.toggle(
    "hide-edge-labels",
    !$("graph-labels").checked,
  ),
);
$("graph-clear-selection").addEventListener("click", () =>
  graphView?.clearSelection(),
);
$("graph-sources").addEventListener("change", () => {
  if (graphSnapshot) renderGraph(graphSnapshot);
});
