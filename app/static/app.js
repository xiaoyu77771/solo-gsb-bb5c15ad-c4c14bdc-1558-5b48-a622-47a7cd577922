/* 字幕断句校验台 — 原生前端逻辑（无框架、无构建） */
(() => {
  const $ = (sel) => document.querySelector(sel);

  const KIND_LABELS = {
    line_too_long: "超长",
    dwell_too_short: "停留过短",
    read_speed: "阅读过快",
  };

  let current = null;

  const fmtTc = (s) => {
    if (s == null || isNaN(s)) return "--:--:--.---";
    const ms = Math.round(s * 1000);
    const h = String(Math.floor(ms / 3600000)).padStart(2, "0");
    const m = String(Math.floor((ms % 3600000) / 60000)).padStart(2, "0");
    const sec = String(Math.floor((ms % 60000) / 1000)).padStart(2, "0");
    const milli = String(ms % 1000).padStart(3, "0");
    return `${h}:${m}:${sec}.${milli}`;
  };

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
    btn.textContent = "断句计算中…";
    try {
      const resp = await fetch("/api/analyze", { method: "POST", body });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "分析失败");
      current = data;
      render(data);
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
    const lang = $("#lang-filter").value;
    const url = `/api/submissions/${current.id}/export` +
      (lang ? `?language=${encodeURIComponent(lang)}` : "");
    window.location.href = url;
  });

  function render(data) {
    $("#result-card").hidden = false;
    $("#result-file").textContent = `— ${data.filename}`;
    renderStats(data);

    const select = $("#lang-filter");
    const langs = new Set(data.issues.map((i) => i.language));
    const prev = select.value;
    select.innerHTML = '<option value="">全部语言</option>' +
      [...langs].map((l) =>
        `<option value="${l}">${data.language_names[l] || l}</option>`).join("");
    select.value = [...langs].includes(prev) ? prev : "";

    renderTables();
    $("#result-card").scrollIntoView({ behavior: "smooth", block: "start" });
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

  async function loadHistory() {
    try {
      const resp = await fetch("/api/submissions");
      const data = await resp.json();
      $("#history-table tbody").innerHTML = data.items.map((it) => {
        const s = it.stats || {};
        return `<tr>
          <td>${it.id}</td>
          <td>${escapeHtml(it.filename)}</td>
          <td class="tc">${it.max_chars}字 / ${it.min_dwell}s /
              ${it.max_cps}字每秒</td>
          <td>${it.issue_count}</td>
          <td>${it.created_at}</td>
          <td><button class="secondary" data-id="${it.id}">查看</button>
              <a href="/api/submissions/${it.id}/export"
                 download><button class="secondary">VTT</button></a></td>
        </tr>`;
      }).join("") || '<tr><td colspan="6" class="muted">暂无任务</td></tr>';

      $("#history-table tbody").querySelectorAll("button[data-id]")
        .forEach((btn) => btn.addEventListener("click", async () => {
          const resp = await fetch(`/api/submissions/${btn.dataset.id}`);
          current = await resp.json();
          render(current);
        }));
    } catch (_) {
      /* 历史加载失败不阻塞主流程 */
    }
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;",
      '"': "&quot;", "'": "&#39;",
    }[ch]));
  }

  loadHistory();
})();
