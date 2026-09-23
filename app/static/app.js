/* 字幕断句校验台 — 原生前端逻辑（无框架、无构建） */
(() => {
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const KIND_LABELS = {
    line_too_long: "超长",
    dwell_too_short: "停留过短",
    read_speed: "阅读过快",
  };
  const DECISION_LABELS = {
    pending: "待确认", accepted: "已采纳", rejected: "已忽略",
  };

  let current = null;          // 当前任务完整结果
  let glossary = [];           // 术语表
  let languageNames = {};      // 语言代码 → 名称

  const fmtTc = (s) => {
    if (s == null || isNaN(s)) return "--:--:--.---";
    const ms = Math.round(s * 1000);
    const h = String(Math.floor(ms / 3600000)).padStart(2, "0");
    const m = String(Math.floor((ms % 3600000) / 60000)).padStart(2, "0");
    const sec = String(Math.floor((ms % 60000) / 1000)).padStart(2, "0");
    const milli = String(ms % 1000).padStart(3, "0");
    return `${h}:${m}:${sec}.${milli}`;
  };

  async function api(path, opts) {
    const resp = await fetch(path, opts);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || `请求失败 (${resp.status})`);
    return data;
  }

  function escapeHtml(str) {
    return String(str ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;",
      '"': "&quot;", "'": "&#39;",
    }[ch]));
  }

  // ==================================================================
  // 术语表管理
  // ==================================================================

  function populateTermLanguages() {
    const options = ['<option value="any">任意语言</option>']
      .concat(Object.entries(languageNames)
        .filter(([code]) => code !== "unknown")
        .map(([code, name]) => `<option value="${code}">${name}</option>`));
    $("#term-language").innerHTML = options.join("");
  }

  async function loadGlossary() {
    try {
      const data = await api("/api/glossary");
      glossary = data.items;
      languageNames = { ...(languageNames), ...data.language_names };
    } catch (_) {
      glossary = [];
    }
    renderGlossary();
  }

  function renderGlossary() {
    const tbody = $("#glossary-table tbody");
    if (!glossary.length) {
      tbody.innerHTML =
        '<tr><td colspan="6" class="muted">暂无术语，点击「新增术语」或导入 JSON</td></tr>';
      return;
    }
    tbody.innerHTML = glossary.map((t) => {
      const lang = t.language === "any"
        ? "任意" : (languageNames[t.language] || t.language);
      const allowed = (t.allowed || [])
        .map((v) => `<span class="chip ok">${escapeHtml(v)}</span>`).join(" ")
        || '<span class="muted">—</span>';
      const forbidden = (t.forbidden || [])
        .map((v) => `<span class="chip bad">${escapeHtml(v)}</span>`).join(" ")
        || '<span class="muted">—</span>';
      return `<tr>
        <td>${lang}</td>
        <td><b>${escapeHtml(t.canonical)}</b></td>
        <td class="chips">${allowed}</td>
        <td class="chips">${forbidden}</td>
        <td class="muted">${escapeHtml(t.note)}</td>
        <td class="nowrap">
          <button class="secondary tiny" data-edit="${t.id}">编辑</button>
          <button class="secondary tiny danger" data-del="${t.id}">删除</button>
        </td>
      </tr>`;
    }).join("");

    tbody.querySelectorAll("button[data-edit]").forEach((btn) =>
      btn.addEventListener("click", () => {
        const t = glossary.find((x) => x.id === Number(btn.dataset.edit));
        openTermForm(t);
      }));
    tbody.querySelectorAll("button[data-del]").forEach((btn) =>
      btn.addEventListener("click", async () => {
        if (!confirm("确认删除该术语？")) return;
        await api(`/api/glossary/${btn.dataset.del}`, { method: "DELETE" });
        loadGlossary();
      }));
  }

  function openTermForm(t) {
    $("#term-form").hidden = false;
    $("#term-error").hidden = true;
    $("#term-id").value = t ? t.id : "";
    $("#term-language").value = t ? t.language : "any";
    $("#term-canonical").value = t ? t.canonical : "";
    $("#term-allowed").value = t ? (t.allowed || []).join(", ") : "";
    $("#term-forbidden").value = t ? (t.forbidden || []).join(", ") : "";
    $("#term-note").value = t ? (t.note || "") : "";
    $("#term-save-btn").textContent = t ? "保存修改" : "保存术语";
    $("#term-canonical").focus();
  }

  $("#term-new-btn").addEventListener("click", () => openTermForm(null));
  $("#term-cancel-btn").addEventListener("click", () => {
    $("#term-form").hidden = true;
  });

  $("#term-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errBox = $("#term-error");
    errBox.hidden = true;
    const body = {
      language: $("#term-language").value,
      canonical: $("#term-canonical").value.trim(),
      allowed: $("#term-allowed").value,
      forbidden: $("#term-forbidden").value,
      note: $("#term-note").value.trim(),
    };
    if (!body.canonical) {
      errBox.textContent = "规范词不能为空";
      errBox.hidden = false;
      return;
    }
    const id = $("#term-id").value;
    try {
      if (id) {
        await api(`/api/glossary/${id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } else {
        await api("/api/glossary", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      }
      $("#term-form").hidden = true;
      loadGlossary();
    } catch (err) {
      errBox.textContent = err.message;
      errBox.hidden = false;
    }
  });

  $("#term-import-btn").addEventListener("click", () =>
    $("#term-import-file").click());
  $("#term-import-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      const terms = Array.isArray(data) ? data : data.terms;
      if (!Array.isArray(terms)) throw new Error("JSON 需为术语数组或 {terms:[...]}");
      await api("/api/glossary/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ terms, replace_all: false }),
      });
      await loadGlossary();
      alert(`已导入 ${terms.length} 条术语（按 语言+规范词 去重合并）`);
    } catch (err) {
      alert(`导入失败：${err.message}`);
    }
    e.target.value = "";
  });

  // ==================================================================
  // 上传与断句分析
  // ==================================================================

  $("#file").addEventListener("change", (e) => {
    const label = $("#file-name");
    label.textContent = e.target.files[0]?.name || "选择 .vtt 或 .srt 文件…";
    label.parentElement.classList.toggle("has-file", !!e.target.files[0]);
  });

  $("#analyze-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errBox = $("#form-error");
    errBox.hidden = true;

    const f = $("#file").files[0];
    if (!f) {
      errBox.textContent = "请先选择字幕文件";
      errBox.hidden = false;
      return;
    }

    const body = new FormData();
    body.append("file", f);
    body.append("max_chars", $("#max-chars").value);
    body.append("min_dwell", $("#min-dwell").value);
    body.append("max_cps", $("#max-cps").value);

    const btn = $("#analyze-btn");
    btn.disabled = true;
    btn.textContent = "校验中…";
    try {
      current = await api("/api/analyze", { method: "POST", body });
      renderAll(current);
    } catch (err) {
      errBox.textContent = err.message;
      errBox.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = "开始校验断句";
      loadHistory();
    }
  });

  $("#lang-filter").addEventListener("change", renderTables);
  $("#only-issues").addEventListener("change", renderTables);

  $("#export-btn").addEventListener("click", () => {
    if (!current) return;
    window.location.href =
      `/api/submissions/${current.id}/export`;
  });

  // ==================================================================
  // 结果渲染（断句 + 术语）
  // ==================================================================

  function renderAll(data) {
    languageNames = { ...(languageNames), ...data.language_names };
    populateTermLanguages();
    $("#result-card").hidden = false;
    $("#result-file").textContent = `— ${data.filename}`;
    renderStats(data);
    populateIssueLangFilter(data);
    renderTables();
    renderTermReview(data);
    $("#result-card")
      .scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderStats(data) {
    const s = data.stats;
    const cls = s.issue_count ? "stat bad" : "stat good";
    $("#stats").innerHTML = `
      <div class="${cls}"><b>${s.original_count}</b>原始 cue</div>
      <div class="stat"><b>${s.revised_count}</b>修订后 cue</div>
      <div class="${cls}"><b>${s.issue_count}</b>问题区段</div>
      <div class="stat"><b>${s.split_cues}</b>被拆分原 cue</div>
      <div class="stat"><b>${s.merged_cues}</b>跨 cue 合并</div>
      <div class="stat"><b>${s.moved_boundaries}</b>边界重分配</div>`;
  }

  function populateIssueLangFilter(data) {
    const select = $("#lang-filter");
    const langs = new Set(data.issues.map((i) => i.language));
    const prev = select.value;
    select.innerHTML = '<option value="">全部语言</option>' +
      [...langs].map((l) =>
        `<option value="${l}">${data.language_names[l] || l}</option>`)
        .join("");
    select.value = [...langs].includes(prev) ? prev : "";
  }

  function activeLang() {
    return $("#lang-filter").value;
  }

  function renderTables() {
    if (!current) return;
    const lang = activeLang();
    const onlyIssues = $("#only-issues").checked;

    const filteredIssues = lang
      ? current.issues.filter((i) => i.language === lang)
      : current.issues;
    const badSeqs = new Map();
    for (const iss of filteredIssues) {
      if (!badSeqs.has(iss.cue_seq)) badSeqs.set(iss.cue_seq, []);
      badSeqs.get(iss.cue_seq).push(iss);
    }

    $("#issue-count-note").textContent =
      `共 ${filteredIssues.length} 个问题` +
      (lang ? `（${current.language_names[lang] || lang}）` : "");

    const tbody = $("#cues-table tbody");
    const rows = current.cues
      .filter((c) => !onlyIssues || badSeqs.has(c.seq));
    tbody.innerHTML = rows.map((c) => {
      const hits = badSeqs.get(c.seq) || [];
      const rowCls = hits.some((h) => h.kind === "line_too_long")
        ? "bad"
        : hits.length ? "warn" : "";
      const tags = hits.map((h) =>
        `<span class="tag ${h.kind}">${KIND_LABELS[h.kind] || h.kind}</span>`
      ).join("");
      const srcCls = c.redistributed ? "src merged" : "src";
      const srcLabel = c.redistributed
        ? `原 cue ${c.sources.join("+")} · 边界已重分配`
        : `原 cue ${c.sources.join(",")}`;
      return `<tr class="${rowCls}">
        <td>${c.seq + 1}</td>
        <td class="tc">${fmtTc(c.start)} →<br/>${fmtTc(c.end)}</td>
        <td class="tc">${c.duration.toFixed(2)}s</td>
        <td>${c.length}</td>
        <td>${c.cps ?? "—"}</td>
        <td class="cue-text">${tags}<span>${escapeHtml(c.text)}</span>
            <span class="${srcCls}">${srcLabel}</span></td>
      </tr>`;
    }).join("");

    const list = $("#issues-list");
    if (!filteredIssues.length) {
      list.innerHTML =
        '<li class="empty">当前筛选条件下没有无法满足约束的区段 ✓</li>';
    } else {
      list.innerHTML = filteredIssues.map((iss) => `
        <li>
          <div class="meta">
            <span class="tag ${iss.kind}">${KIND_LABELS[iss.kind] || iss.kind}</span>
            修订 cue #${iss.cue_seq + 1} ·
            ${iss.language_name} ·
            ${fmtTc(iss.start)} → ${fmtTc(iss.end)}
          </div>
          <span class="seg">${escapeHtml(iss.segment)}</span>
          <span class="reason">${escapeHtml(iss.reason)}</span>
        </li>`).join("");
    }
  }

  // ==================================================================
  // 术语审校
  // ==================================================================

  $("#term-lang-filter").addEventListener("change", renderTermFindings);
  $("#term-type-filter").addEventListener("change", renderTermFindings);
  $("#term-decision-filter").addEventListener("change", renderTermFindings);

  $("#terms-accept-all").addEventListener("click", () =>
    bulkDecision("accepted"));
  $("#terms-ignore-all").addEventListener("click", () =>
    bulkDecision("rejected"));
  $("#terms-export-btn").addEventListener("click", () => {
    if (!current) return;
    window.location.href =
      `/api/submissions/${current.id}/terms/export`;
  });
  $("#terms-report-btn").addEventListener("click", () => {
    if (!current) return;
    window.location.href =
      `/api/submissions/${current.id}/terms/report`;
  });

  async function bulkDecision(decision) {
    if (!current) return;
    const visible = getFilteredFindings();
    const pendingVisible = visible
      .filter((f) => f.decision === "pending");
    if (!pendingVisible.length) {
      alert("当前筛选条件下没有待确认项");
      return;
    }
    const label = DECISION_LABELS[decision];
    if (!confirm(`将把当前筛选条件下的 ${pendingVisible.length} 个待确认项"${label}"，继续？`))
      return;
    try {
      const data = await api(
        `/api/submissions/${current.id}/terms/decisions`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            decision,
            finding_ids: pendingVisible.map((f) => f.id),
          }),
        });
      current.term_review = data;
      renderTermReview(current);
    } catch (err) {
      alert(err.message);
    }
  }

  function renderTermReview(data) {
    const tr = data.term_review;
    if (!tr) return;
    $("#term-review-card").hidden = false;
    $("#term-review-file").textContent = `— ${data.filename}`;
    renderTermStats(tr);
    populateTermFindingsFilters(tr);
    renderTermFindings();
  }

  function renderTermStats(tr) {
    const c = tr.counts || { pending: 0, accepted: 0, rejected: 0 };
    const total = c.pending + c.accepted + c.rejected;
    const cls = c.pending ? "stat bad" : "stat good";
    $("#term-stats").innerHTML = `
      <div class="${cls}"><b>${total}</b>疑似项</div>
      <div class="stat"><b>${c.pending}</b>待确认</div>
      <div class="stat good"><b>${c.accepted}</b>已采纳</div>
      <div class="stat"><b>${c.rejected}</b>已忽略</div>`;
  }

  function populateTermFindingsFilters(tr) {
    const langSel = $("#term-lang-filter");
    const langs = new Set(tr.findings.map((f) => f.language));
    const prevLang = langSel.value;
    langSel.innerHTML = '<option value="">全部语言</option>' +
      [...langs].map((l) =>
        `<option value="${l}">${languageNames[l] || l}</option>`).join("");
    langSel.value = [...langs].includes(prevLang) ? prevLang : "";

    const typeSel = $("#term-type-filter");
    const types = new Set(tr.findings.map((f) => f.issue_type));
    const prevType = typeSel.value;
    typeSel.innerHTML = '<option value="">全部类型</option>' +
      [...types].map((t) =>
        `<option value="${t}">${tr.type_labels[t] || t}</option>`)
        .join("");
    typeSel.value = [...types].includes(prevType) ? prevType : "";
  }

  function getFilteredFindings() {
    if (!current || !current.term_review) return [];
    const lang = $("#term-lang-filter").value;
    const type = $("#term-type-filter").value;
    const decision = $("#term-decision-filter").value;
    return current.term_review.findings.filter((f) =>
      (!lang || f.language === lang) &&
      (!type || f.issue_type === type) &&
      (!decision || f.decision === decision));
  }

  function renderTermFindings() {
    const list = $("#term-findings");
    if (!current || !current.term_review) return;
    const findings = getFilteredFindings();
    const total = current.term_review.findings.length;
    $("#term-count-note").textContent =
      `显示 ${findings.length} / ${total} 个疑似项`;

    if (!findings.length) {
      list.innerHTML = '<li class="empty">当前筛选条件下无疑似项 ✓</li>';
      return;
    }

    list.innerHTML = findings.map((f) => {
      const sev = f.severity || "warn";
      const lang = languageNames[f.language] || f.language;
      const forms = (f.forms || []).map((x) =>
        `<span class="mini-chip ${x.kind}">${escapeHtml(x.surface)}`
        + ` ×${x.count}（cue ${x.cue_seqs.map((s) => s + 1).join(",")}）`
        + `</span>`).join(" ");
      const decisionCls = `decision ${f.decision}`;
      const decisionText = DECISION_LABELS[f.decision] || f.decision;
      return `<li class="term-item ${f.decision}" data-fid="${f.id}">
        <div class="term-item-head">
          <span class="tag term-tag ${sev}">${f.issue_type_label}</span>
          <b>${escapeHtml(f.canonical)}</b>
          <span class="muted">${lang} · 来源 cue #${f.cue_seq + 1}
            · ${fmtTc(f.start)} → ${fmtTc(f.end)}</span>
          <span class="${decisionCls}">${decisionText}</span>
        </div>
        <div class="term-context">…${escapeHtml(f.context_before)}
          <mark class="surface">${escapeHtml(f.surface)}</mark>
          ${escapeHtml(f.context_after)}…</div>
        ${f.cross_cue ? `<div class="term-forms">跨 cue 形式：${forms}</div>` : ""}
        <div class="term-reason">${escapeHtml(f.reason)}</div>
        <div class="term-actions">
          <label>替换为
            <input type="text" class="replacement" value="${
              escapeHtml(f.replacement ?? f.suggestion)}"
              ${f.decision !== "pending" ? "disabled" : ""} />
          </label>
          <button class="primary tiny act-accept"
            ${f.decision === "accepted" ? "disabled" : ""}>采纳</button>
          <button class="secondary tiny act-ignore"
            ${f.decision === "rejected" ? "disabled" : ""}>忽略</button>
          <button class="secondary tiny act-reset"
            ${f.decision === "pending" ? "disabled" : ""}>重置</button>
        </div>
      </li>`;
    }).join("");

    list.querySelectorAll(".term-item").forEach((li) =>
      bindTermItem(li));
  }

  function bindTermItem(li) {
    const fid = Number(li.dataset.fid);
    const finding = () => current.term_review.findings
      .find((f) => f.id === fid);

    li.querySelector(".act-accept").addEventListener("click", async () => {
      await setDecision(fid, "accepted",
        li.querySelector(".replacement").value);
    });
    li.querySelector(".act-ignore").addEventListener("click", async () => {
      await setDecision(fid, "rejected");
    });
    li.querySelector(".act-reset").addEventListener("click", async () => {
      await setDecision(fid, "pending");
    });
  }

  async function setDecision(fid, decision, replacement) {
    try {
      const tr = await api(
        `/api/submissions/${current.id}/terms/${fid}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(
            { decision, replacement: replacement ?? null }),
        });
      current.term_review = tr;
      renderTermStats(tr);
      renderTermFindings();
    } catch (err) {
      alert(err.message);
    }
  }

  // ==================================================================
  // 历史任务
  // ==================================================================

  async function loadHistory() {
    try {
      const data = await api("/api/submissions");
      $("#history-table tbody").innerHTML = data.items.map((it) => {
        const termCell = it.term_total
          ? `${it.term_pending} 待确认 / ${it.term_accepted} 采纳 / `
            + `${it.term_rejected} 忽略`
          : '<span class="muted">无</span>';
        return `<tr>
          <td>${it.id}</td>
          <td>${escapeHtml(it.filename)}</td>
          <td class="tc">${it.max_chars}字 / ${it.min_dwell}s /
              ${it.max_cps}字每秒</td>
          <td>${it.issue_count}</td>
          <td class="muted">${termCell}</td>
          <td>${it.created_at}</td>
          <td><button class="secondary" data-id="${it.id}">查看</button>
              <a href="/api/submissions/${it.id}/export"
                 download><button class="secondary tiny">断句 VTT</button></a>
              <a href="/api/submissions/${it.id}/terms/export"
                 download><button class="secondary tiny">术语 VTT</button></a>
              <a href="/api/submissions/${it.id}/terms/report"
                 download><button class="secondary tiny">报告</button></a></td>
        </tr>`;
      }).join("") || '<tr><td colspan="7" class="muted">暂无任务</td></tr>';

      $("#history-table tbody").querySelectorAll("button[data-id]")
        .forEach((btn) => btn.addEventListener("click", async () => {
          current = await api(`/api/submissions/${btn.dataset.id}`);
          renderAll(current);
        }));
    } catch (_) {
      /* 历史加载失败不阻塞主流程 */
    }
  }

  // 初始化
  (async function init() {
    try {
      const data = await api("/api/submissions");
      languageNames = data.language_names || {};
    } catch (_) { /* 忽略 */ }
    populateTermLanguages();
    await loadGlossary();
    loadHistory();
  })();
})();
