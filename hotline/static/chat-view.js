"use strict";

// 对话只存于当前页面。沿用显式业务范围，每轮重新检索；历史回答不作为政策证据。
const chat = { turns: [], pending: null, epoch: 0 };
const chatForm = $("query-form");
const sourcePlaceholder = $("chat-source-list").cloneNode(true);
function chatScope() {
  const scope = Object.fromEntries(
    ["region", "service", "as_of"].map((key) => [
      key,
      chatForm.elements[key].value.trim(),
    ]),
  );
  if (chatForm.elements.service_code.value.trim()) scope.service_code = chatForm.elements.service_code.value.trim();
  return scope;
}
function updateChatScope() {
  const scope = chatScope();
  $("chat-scope-summary").textContent =
    [scope.region, scope.service, scope.service_code, scope.as_of].filter(Boolean).join(" · ") ||
    "自动识别 · 默认今天";
}
for (const key of ["region", "service", "as_of", "service_code"])
  chatForm.elements[key].addEventListener("input", updateChatScope);
for (const key of ["region", "service"]) chatForm.elements[key].addEventListener("input", () => {chatForm.elements.service_code.value = ""; updateChatScope();});
function scrollChat() {
  $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight;
}
function busyChat(busy) {
  $("chat-send").disabled = busy;
  $("chat-stop").hidden = !busy;
  $("query-mode").disabled = busy;
  for (const key of ["region", "service", "as_of", "service_code"])
    chatForm.elements[key].disabled = busy;
}
function showChatSources(turn, focusId = null) {
  const answer = turn.answer;
  const root = clear("chat-source-list");
  $("chat-source-count").textContent = `${answer.citations.length} 条引用`;
  root.append(
    el("p", `第 ${turn.number} 轮 · ${turn.question}`, "evidence-question"),
  );
  if (!answer.citations.length && answer.services?.length) {
    $("chat-source-count").textContent = "相关指南";
    root.append(
      el("p", "以下是待确认的检索候选，尚未作为本轮政策答案。", "muted small"),
    );
    const records = new Map(
      answer.services.map((service) => [service.record_id, service]),
    );
    for (const service of records.values()) {
      const card = el("article", undefined, "chat-source-card");
      card.append(
        el("h3", service.title || service.name),
        el("p", `适用地区：${service.region || "尚未标注"}`, "muted small"),
        button("查看该指南", () => openSource(service.record_id)),
      );
      root.append(card);
    }
  } else if (!answer.citations.length)
    root.append(
      el(
        "p",
        "本轮没有可核对的引用。请补充地区、事项，或联系知识维护人员。",
        "muted",
      ),
    );
  for (const [index, citation] of answer.citations.entries()) {
    const id = citation.id || String(index + 1);
    const card = el("article", undefined, "chat-source-card");
    card.classList.toggle("is-focused", id === focusId);
    card.append(
      el(
        "span",
        `[${id}] ${citation.page ? `第 ${citation.page} 页` : "文本来源"} · ${citation.section || "正文"}`,
        "source-location",
      ),
      el("h3", citation.title || "来源文件"),
      el("blockquote", citation.quote),
    );
    if (citation.synthetic) card.append(el("span", "合成测试资料", "badge"));
    const actions = el("div", undefined, "source-actions");
    actions.append(
      button("定位原文", () => openEvidence(citation, citation.revision_id)),
      button("阅读文件", () => openSource(citation.revision_id)),
    );
    card.append(actions);
    root.append(card);
    if (id === focusId)
      requestAnimationFrame(() => card.scrollIntoView({ block: "nearest" }));
  }
  for (const item of $("answer").querySelectorAll(".chat-assistant"))
    item.classList.toggle(
      "is-current",
      item.dataset.turn === String(turn.number),
    );
}
function selectCitation(turn, citation) {
  showChatSources(turn, citation.id);
  return openEvidence(citation, citation.revision_id);
}
function renderChatAnswer(turn, card) {
  const a = turn.answer;
  card.replaceChildren();
  const heading = el("div", undefined, "chat-assistant-heading");
  heading.append(
    el("span", "政", "chat-avatar"),
    el("strong", "政务知识助手"),
    el(
      "span",
      a.generation?.used ? "基于检索证据" : "结构化检索",
      "muted small",
    ),
  );
  card.append(heading);
  if (a.status === "CONFLICT")
    card.append(el("p", "存在不同来源的要求，请分别核实。", "notice"));
  if (a.statements?.length)
    for (const statement of a.statements) {
      const paragraph = el("p", statement.text, "qa-statement");
      for (const id of statement.citation_ids) {
        const citation = a.citations.find((c) => c.id === id);
        if (citation) {
          const b = button(
            `[${id}]`,
            () => selectCitation(turn, citation),
            "secondary citation-button",
          );
          b.setAttribute("aria-label", `引用 ${id}：${citation.title}`);
          paragraph.append(b);
        }
      }
      card.append(paragraph);
    }
  else card.append(el("p", a.answer, "qa-statement"));
  const missing = (a.missing_information || []).filter(
    (item) =>
      item !== "现行有效性" ||
      !a.warnings.some((warning) => warning.includes("有效")),
  );
  if (missing.length)
    card.append(el("p", "仍需确认：" + missing.join("；"), "notice"));
  if (a.warnings?.length) {
    const warnings = el("details", undefined, "chat-warnings");
    warnings.append(el("summary", `核实提醒 · ${a.warnings.length}`));
    // 测试来源、冲突与时效提醒不能折叠后消失。
    for (const warning of a.warnings) {
      if (/合成|不同来源|多个已发布|有效/.test(warning))
        card.append(el("p", warning, "chat-caution"));
      else warnings.append(el("p", warning));
    }
    if (warnings.children.length > 1) card.append(warnings);
  }
  if (a.facts.length) {
    const facts = el("details", undefined, "chat-facts");
    facts.append(el("summary", `结构化检索事实（${a.facts.length} 条）`));
    if (turn.mode === "query") facts.open = true;
    for (const fact of a.facts) {
      const block = el("div", undefined, "fact");
      block.append(
        el("strong", fact.label),
        el("p", fact.value),
        attributeView(fact.attributes),
      );
      if (fact.rule) block.append(details("完整条件逻辑", fact.rule));
      block.append(evidenceView(fact.evidence, fact.record_id));
      facts.append(block);
    }
    card.append(facts);
  }
  if (a.validity) card.append(details("原文日期信息", a.validity));
  const services = [
    ...new Map(
      (a.services || []).map((s) => [`${s.region}\n${s.name}\n${s.service_code || ""}`, s]),
    ).values(),
  ];
  if (
    services.length &&
    (a.status === "NEEDS_CLARIFICATION" ||
      services.length > 1 ||
      services[0].name !== chatScope().service)
  ) {
    const scope = el("div", undefined, "chat-service-options");
    const confirming = a.status === "NEEDS_CLARIFICATION";
    scope.append(
      el("span", confirming ? "确认后查询本题：" : "继续查询：", "muted small"),
    );
    for (const s of services)
      scope.append(
        button(
          `${s.name} · ${s.region || "地区未知"}${s.service_code ? ` · ${s.service_code}` : ""}`,
          () => {
            if (chat.pending) return;
            chatForm.elements.service.value = s.name;
            chatForm.elements.region.value = s.region;
            chatForm.elements.service_code.value = s.service_code || "";
            updateChatScope();
            if (confirming && s.region)
              return sendChat(
                turn.question,
                { ...turn.scope, region: s.region, service: s.name, service_code: s.service_code || undefined },
                turn.mode,
              );
            $("chat-question").focus();
          },
          "secondary scope-choice",
        ),
      );
    card.append(scope);
  }
  const actions = el("div", undefined, "chat-answer-actions");
  actions.append(
    button(
      a.citations.length || !a.services?.length
        ? `查看依据（${a.citations.length}）`
        : "查看候选指南",
      () => {
        showChatSources(turn);
        document.querySelector(".chat-layout").classList.add("show-evidence");
        $("chat-evidence").scrollIntoView({ block: "nearest" });
      },
    ),
    button("复制答复", async () => {
      const citations = a.citations
        .map(
          (c, i) =>
            `[${c.id || i + 1}] ${c.title} · ${c.section || "正文"}${c.page ? ` · 第${c.page}页` : ""}\n${c.quote}`,
        )
        .join("\n\n");
      await navigator.clipboard.writeText(
        [a.answer, ...(a.warnings || []), citations]
          .filter(Boolean)
          .join("\n\n"),
      );
      notice("已复制答复、核实提醒和原文引用。");
    }),
  );
  appendBusinessActions(actions, turn);
  card.append(
    actions,
    el(
      "p",
      `${a.generation?.used ? a.generation.model + " · " : ""}${(a.latency_ms / 1000).toFixed(1)} 秒 · 第 ${turn.number} 轮`,
      "chat-answer-meta",
    ),
  );
}
async function sendChat(question, scope, mode) {
  if (chat.pending) return;
  if (chat.turns.length >= 40) {
    notice("当前对话已达 40 轮，请新建对话继续。");
    return;
  }
  for (const key of ["region", "service", "as_of", "service_code"])
    chatForm.elements[key].value = scope[key] || "";
  $("query-mode").value = mode;
  updateChatScope();
  const epoch = chat.epoch,
    controller = new AbortController();
  chat.pending = controller;
  busyChat(true);
  $("chat-welcome").hidden = true;
  const turn = { question, scope, mode, number: chat.turns.length + 1 };
  chat.turns.push(turn);
  const userMessage = el("article", undefined, "chat-user");
  userMessage.append(
    el("p", question),
    el(
      "span",
      [
        scope.region || "地区未限定",
        scope.service || "事项未限定",
        scope.as_of || "当前日期",
      ].join(" · "),
      "chat-turn-scope",
    ),
  );
  const card = el("article", undefined, "chat-assistant card");
  card.dataset.turn = turn.number;
  card.append(el("p", "正在检索已发布知识与原文依据…", "chat-pending"));
  card.setAttribute("aria-busy", "true");
  $("answer").append(userMessage, card);
  $("chat-question").value = "";
  scrollChat();
  try {
    const result = await api(`/api/${mode}`, {
      method: "POST",
      body: JSON.stringify({ question, ...scope, space: knowledgeSpace }),
      signal: controller.signal,
    });
    if (epoch !== chat.epoch) return;
    if (controller.signal.aborted)
      throw new DOMException("请求已取消", "AbortError");
    turn.answer = result;
    userMessage.querySelector(".chat-turn-scope").textContent = [
      result.scope?.region || scope.region || "地区未限定",
      result.scope?.service || scope.service || "事项未限定",
      result.date_resolution && ["AMBIGUOUS", "CONFLICT", "INVALID"].includes(result.date_resolution)
        ? "查询日期待确认" : result.as_of || scope.as_of || "当前日期",
    ].join(" · ");
    renderChatAnswer(turn, card);
    showChatSources(turn);
    // 只将唯一、已检索出的事项回填到可见控件；不把旧答案提交给模型。
    const services = [
      ...new Map(
        (result.services || []).map((s) => [`${s.region}\n${s.name}`, s]),
      ).values(),
    ];
    if (services.length === 1 && result.status !== "NEEDS_CLARIFICATION") {
      if (!scope.service) chatForm.elements.service.value = services[0].name;
      if (!scope.region) chatForm.elements.region.value = services[0].region;
      if (!scope.service_code) chatForm.elements.service_code.value = services[0].service_code || "";
      updateChatScope();
      chatForm.querySelector(".chat-scope").open = false;
    }
  } catch (error) {
    if (epoch !== chat.epoch) return;
    card.replaceChildren(
      el(
        "p",
        controller.signal.aborted
          ? "已停止等待。本次后台请求可能仍在完成，结果不会追加到对话。"
          : `本轮查询未完成：${error.message}`,
        "notice",
      ),
    );
    card.append(button("重试本轮", () => sendChat(question, scope, mode)));
  } finally {
    card.removeAttribute("aria-busy");
    if (epoch === chat.epoch) {
      chat.pending = null;
      busyChat(false);
      const scroll = $("chat-scroll");
      scroll.scrollTop +=
        userMessage.getBoundingClientRect().top -
        scroll.getBoundingClientRect().top -
        12;
      $("chat-question").focus();
    }
  }
}
chatForm.addEventListener(
  "submit",
  guarded(async () => {
    const question = $("chat-question").value.trim();
    if (question.length < 2) {
      notice("请至少输入两个字的问题。");
      return;
    }
    await sendChat(question, chatScope(), $("query-mode").value);
  }),
);
$("chat-question").addEventListener("keydown", (event) => {
  if (
    event.key === "Enter" &&
    !event.shiftKey &&
    !event.isComposing &&
    event.keyCode !== 229
  ) {
    event.preventDefault();
    if (!chat.pending) chatForm.requestSubmit();
  }
});
$("chat-stop").addEventListener("click", () => chat.pending?.abort());
$("chat-new").addEventListener("click", () => {
  chat.epoch++;
  chat.pending?.abort();
  chat.pending = null;
  chat.turns = [];
  busyChat(false);
  chatForm.reset();
  chatForm.querySelector(".chat-scope").open = false;
  document.querySelector(".chat-layout").classList.remove("show-evidence");
  updateChatScope();
  clear("answer");
  $("chat-welcome").hidden = false;
  $("chat-source-count").textContent = "待查询";
  $("chat-source-list").replaceChildren(
    ...sourcePlaceholder.cloneNode(true).childNodes,
  );
  $("chat-question").focus();
});
$("chat-evidence-close").addEventListener("click", () =>
  document.querySelector(".chat-layout").classList.remove("show-evidence"),
);
for (const suggestion of document.querySelectorAll("[data-question]"))
  suggestion.addEventListener("click", () => {
    $("chat-question").value = suggestion.dataset.question;
    $("chat-question").focus();
  });
