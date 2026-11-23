/* Тема, графики, канбан. Работает и после переходов hx-boost. */
(function () {
  "use strict";

  var TEXTS = {};
  try { TEXTS = JSON.parse(document.getElementById("ui-texts").textContent); } catch (e) {}
  function text(key) { return TEXTS[key] || key; }

  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  // ---------- тема ----------
  function currentTheme() {
    var t = document.documentElement.dataset.theme;
    if (t) return t;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  document.addEventListener("click", function (e) {
    if (!e.target.closest("[data-theme-toggle]")) return;
    var next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("cg-theme", next); } catch (err) {}
    initCharts(document, true);
  });

  // ---------- период ----------
  document.addEventListener("change", function (e) {
    var sel = e.target.closest("[data-autosubmit]");
    if (!sel) return;
    var dates = sel.form.querySelector("[data-dates]");
    if (sel.hasAttribute("data-period-select") && dates) {
      var custom = sel.value === "custom";
      dates.hidden = !custom;
      dates.querySelectorAll("input").forEach(function (i) { i.disabled = !custom; });
      if (custom) { dates.querySelector("input").focus(); return; } // ждём выбора дат и «Показать»
    }
    sel.form.requestSubmit ? sel.form.requestSubmit() : sel.form.submit();
  });

  // ---------- графики ----------
  function fmt(v, unit) {
    if (v == null) return "—";
    var a = Math.abs(v), s;
    if (unit === "₽" && a >= 1e6) s = (v / 1e6).toLocaleString("ru-RU", { maximumFractionDigits: 1 }) + " млн ₽";
    else if (unit === "₽" && a >= 1e4) s = Math.round(v / 1e3).toLocaleString("ru-RU") + " тыс ₽";
    else s = v.toLocaleString("ru-RU", { maximumFractionDigits: 1 }) + (unit ? " " + unit : "");
    return s;
  }

  function initCharts(root, force) {
    if (!window.Chart) return;
    root.querySelectorAll("script[data-chart]").forEach(function (node) {
      var canvas = node.previousElementSibling && node.previousElementSibling.querySelector("canvas");
      if (!canvas) return;
      if (canvas._chart) { if (!force) return; canvas._chart.destroy(); }
      var spec = JSON.parse(node.textContent);
      var palette = [css("--c1"), css("--c2"), css("--c3")];
      var ink = css("--muted"), line = css("--line");
      var datasets = spec.datasets.map(function (d, i) {
        var color = spec.compare ? palette[i % 3] : (d.role === "plan" ? css("--plan") : css("--fact"));
        var base = { label: d.label, data: d.data, backgroundColor: color, borderColor: color };
        if (spec.kind === "line") {
          return Object.assign(base, { tension: .3, pointRadius: 3, borderWidth: 2, fill: false,
            borderDash: d.role === "plan" ? [5, 4] : [] });
        }
        return Object.assign(base, { borderRadius: 4, maxBarThickness: 28 });
      });
      try { canvas._chart = new Chart(canvas, {
        type: spec.kind === "line" ? "line" : "bar",
        data: { labels: spec.labels, datasets: datasets },
        options: {
          maintainAspectRatio: false, animation: false,
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { position: "bottom", labels: { color: ink, boxWidth: 10, boxHeight: 10, usePointStyle: true } },
            tooltip: { callbacks: { label: function (c) { return c.dataset.label + ": " + fmt(c.parsed.y, spec.unit); } } }
          },
          scales: {
            x: { grid: { display: false }, ticks: { color: ink } },
            y: { grid: { color: line }, border: { display: false },
                 ticks: { color: ink, maxTicksLimit: 5, callback: function (v) { return fmt(v, spec.unit); } } }
          }
        }
      }); } catch (err) { console.error("chart", err); } // один сломанный график не мешает остальной странице
    });
  }

  // ---------- канбан ----------
  function toast(text) {
    var t = document.createElement("div");
    t.className = "toast"; t.textContent = text;
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 3500);
  }

  function initBoards(root) {
    if (!window.Sortable) return;
    root.querySelectorAll("[data-board] .col__list").forEach(function (list) {
      if (list._sortable) return;
      list._sortable = Sortable.create(list, {
        group: "board-" + Array.prototype.indexOf.call(document.querySelectorAll("[data-board]"), list.closest("[data-board]")),
        animation: 150, filter: ".is-locked", preventOnFilter: false,
        delay: 180, delayOnTouchOnly: true, // на телефоне — долгое нажатие, чтобы не мешать прокрутке
        onEnd: function (ev) {
          if (ev.from === ev.to && ev.oldIndex === ev.newIndex) return;
          var card = ev.item, after = [], n = card.nextElementSibling;
          while (n) { after.push(n.dataset.id); n = n.nextElementSibling; }
          updateCounts(card.closest("[data-board]"));
          fetch("/api/kanban/move", {
            method: "POST", credentials: "same-origin",
            headers: { "Content-Type": "application/json", "X-CG": "1" },
            body: JSON.stringify({ card_id: card.dataset.id, status: ev.to.dataset.status, after_ids: after })
          }).then(function (r) {
            if (r.ok) return;
            return r.json().catch(function () { return {}; }).then(function (j) {
              toast(j.error || text("kanban_move_error"));
              setTimeout(function () { location.reload(); }, 1200);
            });
          }).catch(function () { toast(text("no_connection")); setTimeout(function () { location.reload(); }, 1200); });
        }
      });
    });
  }

  function updateCounts(board) {
    board.querySelectorAll(".col").forEach(function (col) {
      col.querySelector(".col__count").textContent = col.querySelectorAll(".card").length;
    });
  }

  // ---------- ход загрузки из 1С ----------
  var sync = { running: false, runId: null, timer: null };

  function renderSync(p) {
    var box = document.querySelector("[data-sync]");
    if (!box) return;
    box.hidden = !p.running;
    if (!p.running) return;
    box.querySelector("[data-sync-pct]").textContent = p.percent + " %";
    box.querySelector("[data-sync-fill]").style.width = p.percent + "%";
    var detail = p.total ? p.done + " из " + p.total : "";
    if (p.current) detail += (detail ? " · " : "") + p.current;
    box.querySelector("[data-sync-detail]").textContent = detail;
  }

  function pollSync() {
    clearTimeout(sync.timer);
    if (!document.body.hasAttribute("data-sync-watch")) return;
    fetch("/api/sync/progress", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (p) {
        if (!p) { sync.timer = setTimeout(pollSync, 30000); return; }
        renderSync(p);
        var finished = sync.running && !p.running && p.run_id === sync.runId;
        if (finished) {
          toast(p.errors ? text("sync_errors") : text("sync_done"));
          if (p.changed) setTimeout(function () { location.reload(); }, 900);
        }
        sync.running = p.running; sync.runId = p.run_id;
        sync.timer = setTimeout(pollSync, p.running ? 1500 : (document.hidden ? 30000 : 8000));
      })
      .catch(function () { sync.timer = setTimeout(pollSync, 15000); });
  }
  document.addEventListener("visibilitychange", function () { if (!document.hidden) pollSync(); });

  function init(root) {
    root = root || document;
    try { initBoards(root); } catch (err) { console.error("board", err); }
    initCharts(root);
  }
  document.addEventListener("DOMContentLoaded", function () { init(document); pollSync(); });
  document.addEventListener("htmx:load", function (e) { init(e.detail.elt); });
  document.addEventListener("htmx:afterSettle", function () { pollSync(); }); // после перехода по меню — сразу показать
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () { initCharts(document, true); });
})();
