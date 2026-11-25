// library.js — grid of shorts with filters, search, multi-select and the bulk bar.
import {
  h, icon, platIcon, thumbImg, toast, confirmModal, openPopover, apiPost, errText, refreshState, reportBulk,
  posts, postById, titleOf, jobOf, isLocked, fmtDuration, fmtSlot, platformDots, activeDeliveries, STATUS,
  ORDER, PLAT, ui, openPost, plural, settings, store, debounce, destinationSummary,
} from "./core.js";
import { openAutoSchedule } from "./schedule.js";

const FILTERS = [
  ["all", "All"], ["draft", "Drafts"], ["captions", "Needs captions"],
  ["scheduled", "Scheduled"], ["posted", "Posted"], ["attention", "Needs attention"],
];
const needsCaption = (p) => p.bucket === "draft" && !(p.caption || "").trim();
const MATCH = {
  all: () => true,
  draft: (p) => p.bucket === "draft",
  captions: needsCaption,
  scheduled: (p) => p.bucket === "scheduled" || p.bucket === "posting",
  posted: (p) => p.bucket === "posted",
  attention: (p) => p.bucket === "attention",
};

export function createLibrary() {
  const root = h("div", { class: "view library" });
  const countsEl = h("div", { class: "sub" });
  const pills = h("div", { class: "pills", role: "tablist" });
  const search = h("input", { class: "input search", type: "search", placeholder: "Search title or filename", "aria-label": "Search shorts", value: ui.libQuery });
  const grid = h("div", { class: "vgrid" });
  const empty = h("div", { class: "empty-wrap" });
  const bar = h("div", { class: "bulkbar", hidden: true });
  const importBtn = h("button", { class: "btn", type: "button", onclick: doImport }, icon("plus", 16), "Import new shorts");
  const runBtn = h("button", { class: "btn", type: "button", onclick: doRun }, icon("bolt", 16), "Run scheduler now");
  root.append(
    h("div", { class: "view-head" }, h("div", null, h("h1", null, "Library"), countsEl), h("div", { class: "head-actions" }, importBtn, runBtn)),
    h("div", { class: "toolbar" }, pills, h("span", { class: "spacer" }), h("div", { class: "searchbox" }, icon("search", 16), search)),
    grid, empty, bar);

  const cards = new Map(); // id -> {el, sig}
  let visible = [];
  let lastIdx = null;

  // ── actions ──
  async function doImport() {
    importBtn.disabled = true;
    try {
      const r = await apiPost("/api/import", {});
      const folder = store.state?.settings?.shorts_folder || "your Shorts folder";
      const bits = [];
      if (r.imported) bits.push(`Imported ${plural(r.imported, "new short")} (writing captions now)`);
      if (r.updated) bits.push(`swapped in ${plural(r.updated, "newer cut")}`);
      toast(bits.length ? bits.join(" · ") + "." : `No new shorts in ${folder}.`, { kind: bits.length ? "success" : "info" });
      refreshState();
    } catch (e) { toast(errText(e), { kind: "error" }); } finally { importBtn.disabled = false; }
  }
  async function doRun() {
    runBtn.disabled = true;
    try { await apiPost("/api/run", {}); toast("Scheduler pass started.", { kind: "success" }); setTimeout(refreshState, 900); }
    catch (e) { toast(errText(e), { kind: "error" }); } finally { runBtn.disabled = false; }
  }
  const selIds = () => [...ui.selected].filter((id) => postById(id));

  async function bulk(action, ids, extra = {}) {
    const r = await apiPost("/api/bulk", { action, ids, ...extra });
    refreshState();
    return r;
  }
  async function runBulk(action, msg, failVerb, extra) {
    const ids = selIds(); if (!ids.length) return;
    try {
      const r = await bulk(action, ids, extra);
      const failed = reportBulk(r, msg, failVerb);
      ui.selected = new Set(failed); // keep only the ones that need another look
      renderSelection();
    } catch (e) { toast(errText(e), { kind: "error" }); }
  }

  const bCaptions = h("button", { class: "btn sm", type: "button", onclick: () => runBulk("captions", (n) => `Writing captions for ${plural(n, "short")}…`, "queued") }, icon("sparkle", 15), "Write captions");
  const bApprove = h("button", { class: "btn sm", type: "button", onclick: () => runBulk("approve", (n) => `Approved ${plural(n, "short")}.`, "approved") }, icon("check", 15), "Approve");
  const bAuto = h("button", { class: "btn sm primary", type: "button", onclick: () => {
    const ids = visibleOrderSelected().filter((id) => !isLocked(postById(id)));
    if (!ids.length) return toast("Pick shorts that haven't been posted yet.", { kind: "info" });
    openAutoSchedule({ ids });
  } }, icon("calendar", 15), "Auto-schedule…");
  const bUnsched = h("button", { class: "btn sm", type: "button", onclick: unschedule }, "Unschedule");
  const bPlat = h("button", { class: "btn sm", type: "button", onclick: () => platformsPopover(bPlat) }, "Platforms…", icon("chevD", 14));
  const bBrand = h("button", { class: "btn sm", type: "button", onclick: () => brandPopover(bBrand) }, "Brand…", icon("chevD", 14));
  const bRemove = h("button", { class: "btn sm danger", type: "button", onclick: remove }, icon("trash", 15), "Remove");
  const bClear = h("button", { class: "btn sm ghost", type: "button", onclick: () => { ui.selected.clear(); renderSelection(); } }, "Clear");
  const nSel = h("strong", { class: "nsel" });
  bar.append(nSel, h("span", { class: "bar-sep" }), bCaptions, bApprove, bAuto, bUnsched, bPlat, bBrand, h("span", { class: "spacer" }), bRemove, bClear);

  const visibleOrderSelected = () => visible.filter((p) => ui.selected.has(p.id)).map((p) => p.id)
    .concat(selIds().filter((id) => !visible.some((p) => p.id === id)));

  async function unschedule() {
    const ids = selIds(); if (!ids.length) return;
    try {
      // The server also returns them to draft, so an unscheduled short never posts by itself.
      const r = await bulk("unschedule", ids);
      const failed = reportBulk(r, (n) => `Unscheduled ${plural(n, "short")} (back to drafts).`, "unscheduled");
      ui.selected = new Set(failed); renderSelection();
    } catch (e) { toast(errText(e), { kind: "error" }); }
  }
  async function remove() {
    const ids = selIds(); if (!ids.length) return;
    const ok = await confirmModal({
      title: `Remove ${plural(ids.length, "short")}?`, danger: true, confirmLabel: "Remove",
      message: "They leave the queue. The video files stay in your Shorts folder.",
    });
    if (!ok) return;
    await runBulk("remove", (n) => `Removed ${plural(n, "short")}.`, "removed");
  }
  function brandPopover(anchor) {
    const ids = selIds(); if (!ids.length) return;
    const list = store.state?.brands || [];
    const pop = openPopover(anchor, h("div", { class: "pop-body" },
      h("div", { class: "pop-title" }, `Brand for ${plural(ids.length, "short")}`),
      h("div", { class: "faint sm" }, "Unposted destinations switch to the brand's accounts. Approvals are cleared so you can check them."),
      [...list, { id: "", name: "No brand" }].map((b) => h("button", { class: "btn sm ghost", type: "button", onclick: async () => {
        pop.close();
        await runBulk("brand", (n) => `Set ${b.name} on ${plural(n, "short")}.`, "updated", { brand: b.id || null });
      } }, b.name))));
  }
  function platformsPopover(anchor) {
    const ids = selIds(); if (!ids.length) return;
    const common = ORDER.filter((pl) => ids.every((id) => postById(id).deliveries?.[pl] && postById(id).deliveries[pl].status !== "skipped"));
    const boxes = ORDER.map((pl) => ({ pl, cb: h("input", { type: "checkbox", checked: common.includes(pl) }) }));
    const pop = openPopover(anchor, h("div", { class: "pop-body" },
      h("div", { class: "pop-title" }, `Post ${plural(ids.length, "short")} to`),
      boxes.map(({ pl, cb }) => h("label", { class: "check" }, cb, platIcon(pl, 16), h("span", null, PLAT[pl].name))),
      h("div", { class: "pop-actions" },
        h("button", { class: "btn sm ghost", type: "button", onclick: () => pop.close() }, "Cancel"),
        h("button", { class: "btn sm primary", type: "button", onclick: async () => {
          const targets = boxes.filter((b) => b.cb.checked).map((b) => b.pl);
          if (!targets.length) return toast("Pick at least one platform.", { kind: "info" });
          pop.close();
          await runBulk("targets", (n) => `Updated platforms on ${plural(n, "short")}.`, "updated", { targets });
        } }, "Apply"))));
  }

  // ── rendering ──
  function filtered() {
    const q = ui.libQuery.trim().toLowerCase();
    return posts().filter((p) => MATCH[ui.libFilter](p) && (!q || titleOf(p).toLowerCase().includes(q) || (p.filename || "").toLowerCase().includes(q)));
  }
  function renderPills() {
    const all = posts();
    pills.replaceChildren(...FILTERS.map(([k, label]) => {
      const n = all.filter(MATCH[k]).length;
      return h("button", { class: `pill${ui.libFilter === k ? " on" : ""}`, type: "button", role: "tab", "aria-selected": ui.libFilter === k, onclick: () => { ui.libFilter = k; lastIdx = null; render(); } },
        label, h("span", { class: "pill-n" }, n));
    }));
  }
  function whenLine(p) {
    if (p.bucket === "posted") return h("span", { class: "when muted" }, "Posted");
    if (p.scheduled_at) {
      return h("span", { class: "when" }, icon("clock", 13), fmtSlot(p.scheduled_at), p.approved_at ? null : h("em", { class: "faint" }, " · not approved"));
    }
    if (p.approved_at) return h("span", { class: "when warn" }, icon("bolt", 13), "Approved · posts as soon as possible");
    return h("span", { class: "when muted" }, "Draft");
  }
  function statusPill(p) {
    if (p.bucket === "attention") {
      const bad = activeDeliveries(p).find(([, d]) => d.status === "failed") || activeDeliveries(p).find(([, d]) => d.status === "needs_review");
      return h("span", { class: `chip chip-${bad?.[1].status === "failed" ? "bad" : "warn"}` }, bad ? `${PLAT[bad[0]].name}: ${STATUS[bad[1].status][0]}` : "Needs attention");
    }
    if (p.bucket === "posting") return h("span", { class: "chip chip-info" }, "Posting…");
    if (p.bucket === "posted") {
      const first = activeDeliveries(p)[0]; const st = first?.[1].status;
      return h("span", { class: `chip chip-${STATUS[st]?.[1] || "ok"}` }, STATUS[st]?.[0] || "Posted");
    }
    return null;
  }
  function sigOf(p) {
    return JSON.stringify([p.updated_at, p.bucket, p.title, p.caption ? 1 : 0, p.scheduled_at, p.approved_at, p.duplicate_of, jobOf(p.id),
      Object.entries(p.deliveries || {}).map(([k, d]) => [k, d.status, d.error ? 1 : 0]),
      p.brand_name, (p.destinations || []).map((d) => [d.key, d.account, d.status])]);
  }
  function fillCard(entry, p) {
    const el = entry.el;
    const job = jobOf(p.id);
    el.className = `vcard${p.bucket === "attention" ? " attention" : ""}${job ? " busy" : ""}${isLocked(p) ? " posted" : ""}`;
    const cb = h("input", { type: "checkbox", "aria-label": `Select ${titleOf(p)}` });
    cb.addEventListener("click", (e) => {
      e.stopPropagation();
      const idx = visible.findIndex((x) => x.id === p.id);
      if (e.shiftKey && lastIdx != null && lastIdx !== idx) {
        const [a, b] = [Math.min(lastIdx, idx), Math.max(lastIdx, idx)];
        for (let i = a; i <= b; i++) cb.checked ? ui.selected.add(visible[i].id) : ui.selected.delete(visible[i].id);
      } else cb.checked ? ui.selected.add(p.id) : ui.selected.delete(p.id);
      lastIdx = idx;
      renderSelection();
    });
    entry.cb = cb;
    const dupNames = (p.duplicate_of || []).map((id) => postById(id)?.filename || id);
    const hasCap = !!(p.caption || "").trim();
    el.replaceChildren(
      h("div", { class: "vthumb" },
        thumbImg(p),
        h("label", { class: "vcheck", onclick: (e) => e.stopPropagation() }, cb),
        p.duration != null ? h("span", { class: "vdur" }, fmtDuration(p.duration)) : null,
        job ? h("div", { class: "vshimmer" }, h("span", null, job.includes("caption") ? "Writing caption…" : job)) : null),
      h("div", { class: "vbody" },
        h("div", { class: "vtitle", title: titleOf(p) }, titleOf(p)),
        !hasCap && p.bucket !== "posted" ? h("div", { class: "vhint" }, "No caption yet") : null,
        h("div", { class: "vmeta" }, platformDots(p), statusPill(p)),
        h("div", { class: "vacct", title: destinationSummary(p) }, destinationSummary(p, { short: true }) || "Nothing selected"),
        whenLine(p),
        dupNames.length ? h("span", { class: "badge dup", title: `Looks like: ${dupNames.join(", ")}` }, "Possible duplicate") : null));
    renderCardSelected(entry, p.id);
  }
  function renderCardSelected(entry, id) {
    const on = ui.selected.has(id);
    entry.el.classList.toggle("selected", on);
    if (entry.cb) entry.cb.checked = on;
  }
  function renderSelection() {
    for (const [id, entry] of cards) renderCardSelected(entry, id);
    const n = selIds().length;
    bar.hidden = n === 0;
    nSel.textContent = `${n} selected`;
    root.classList.toggle("has-bar", n > 0);
  }

  function render() {
    const all = posts();
    const c = { draft: 0, scheduled: 0, posted: 0, attention: 0 };
    all.forEach((p) => { if (p.bucket in c) c[p.bucket]++; else if (p.bucket === "posting") c.scheduled++; });
    countsEl.textContent = all.length
      ? `${plural(all.length, "short")} · ${c.draft} drafts · ${c.scheduled} scheduled · ${c.posted} posted${c.attention ? ` · ${c.attention} need attention` : ""}`
      : "Nothing imported yet";
    renderPills();
    // drop selections of vanished posts
    for (const id of [...ui.selected]) if (!postById(id)) ui.selected.delete(id);

    visible = filtered();
    const seen = new Set();
    const els = [];
    for (const p of visible) {
      seen.add(p.id);
      let entry = cards.get(p.id);
      const sig = sigOf(p);
      if (!entry) {
        entry = { el: h("article", { class: "vcard", tabindex: 0, dataset: { id: p.id } }), sig: null };
        entry.el.addEventListener("click", () => openPost(p.id));
        entry.el.addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === " ") && e.target === entry.el) { e.preventDefault(); openPost(p.id); } });
        cards.set(p.id, entry);
      }
      if (entry.sig !== sig) { entry.sig = sig; fillCard(entry, p); }
      els.push(entry.el);
    }
    for (const id of [...cards.keys()]) if (!seen.has(id)) { cards.delete(id); }
    const same = els.length === grid.children.length && els.every((e, i) => grid.children[i] === e);
    if (!same) grid.replaceChildren(...els);

    // empty states
    empty.replaceChildren();
    if (!visible.length && store.state) {
      if (!all.length) {
        empty.append(h("div", { class: "empty" },
          h("div", { class: "empty-ico" }, icon("video", 30)),
          h("h3", null, "No shorts yet"),
          h("p", null, `Drop vertical MP4s into ${settings().shorts_folder || "your Shorts folder"}, then import them.`),
          h("button", { class: "btn primary", type: "button", onclick: doImport }, icon("plus", 16), "Import new shorts")));
      } else {
        empty.append(h("div", { class: "empty" },
          h("h3", null, "Nothing matches"),
          h("p", null, ui.libQuery ? "Try a different search, or clear the filter." : "No shorts in this group right now."),
          h("button", { class: "btn", type: "button", onclick: () => { ui.libFilter = "all"; ui.libQuery = ""; search.value = ""; render(); } }, "Show everything")));
      }
    }
    renderSelection();
  }

  search.addEventListener("input", debounce(() => { ui.libQuery = search.value; lastIdx = null; render(); }, 120));
  render();
  return { el: root, update: render, destroy() {} };
}
