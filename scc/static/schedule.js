// schedule.js — the Auto-schedule modal (Library bulk bar + Calendar).
import {
  h, icon, thumbImg, openModal, toast, apiPost, errText, refreshState, onState, posts, postById, titleOf,
  isUnscheduled, todayYmd, ymdIn, fmtSlot, cadenceSummary, ui, go, plural, reportBulk, store,
} from "./core.js";

/**
 * openAutoSchedule({ids})        — fixed list (from the Library selection)
 * openAutoSchedule({checklist})  — checklist of every unscheduled post (from the Calendar)
 */
export function openAutoSchedule({ ids = null, checklist = false } = {}) {
  let order = (checklist ? posts().filter(isUnscheduled).map((p) => p.id) : (ids || [])).filter((id) => postById(id));
  const chosen = new Set(order);
  let seq = 0, plan = null, busy = false, waitingCaptions = false;

  if (!order.length) { toast("Nothing to schedule — every short already has a time.", { kind: "info" }); return; }

  const startInput = h("input", { type: "date", class: "input", value: todayYmd(), "aria-label": "Start date" });
  const approveBox = h("input", { type: "checkbox", checked: true });
  const listEl = h("div", { class: "sched-list" });
  const summary = h("div", { class: "sched-summary" });
  const help = h("div", { class: "sched-help" });
  const rows = new Map();

  function rowFor(id) {
    if (rows.has(id)) return rows.get(id);
    const p = postById(id);
    const res = h("div", { class: "sched-res" });
    const up = h("button", { class: "icon-btn sm", type: "button", "aria-label": "Move up", onclick: () => move(id, -1) }, icon("up", 14));
    const dn = h("button", { class: "icon-btn sm", type: "button", "aria-label": "Move down", onclick: () => move(id, 1) }, icon("down", 14));
    const cb = checklist ? h("input", { type: "checkbox", checked: chosen.has(id), "aria-label": `Include ${titleOf(p)}`, onchange: (e) => { e.target.checked ? chosen.add(id) : chosen.delete(id); preview(); } }) : null;
    const row = h("div", { class: "sched-row", draggable: true, dataset: { id } },
      h("span", { class: "grip", "aria-hidden": "true" }, icon("grip", 16)), cb,
      h("div", { class: "sched-thumb" }, thumbImg(p)),
      h("div", { class: "sched-title" }, h("div", { class: "clamp1" }, titleOf(p)), !p.caption?.trim() ? h("div", { class: "faint sm" }, "No caption yet") : null),
      res, h("div", { class: "sched-move" }, up, dn));
    const o = { row, res, cb };
    rows.set(id, o);
    return o;
  }
  function paintList() {
    listEl.replaceChildren(...order.map((id) => rowFor(id).row));
    paintResults();
  }
  function move(id, d) {
    const i = order.indexOf(id), j = i + d;
    if (j < 0 || j >= order.length) return;
    [order[i], order[j]] = [order[j], order[i]];
    paintList(); preview();
  }
  const chosenOrdered = () => order.filter((id) => chosen.has(id));

  function paintResults() {
    for (const id of order) {
      const o = rowFor(id);
      o.row.classList.toggle("off", !chosen.has(id));
      o.res.className = "sched-res";
      o.res.replaceChildren();
      if (!chosen.has(id) || !plan) continue;
      if (plan.assigned?.[id]) { o.res.classList.add("ok"); o.res.append("→ ", fmtSlot(plan.assigned[id], " ")); }
      else if (plan.errors?.[id]) { o.res.classList.add("bad"); o.res.textContent = plan.errors[id]; }
    }
    const n = plan ? Object.keys(plan.assigned || {}).length : 0;
    const bad = plan ? Object.keys(plan.errors || {}).length : 0;
    summary.replaceChildren(plan ? h("span", null, h("strong", null, plural(n, "post")), " will be scheduled", bad ? [", ", h("span", { class: "bad-text" }, `${bad} can't`)] : "", ".") : (busy ? "Working out the schedule…" : ""));
    if (btnSchedule.el) {
      btnSchedule.el.textContent = n ? `Schedule ${plural(n, "post")}` : "Schedule";
      btnSchedule.el.disabled = !n || busy;
    }
    // Suggest captions when the failures look caption-related.
    help.replaceChildren();
    const capIds = plan ? Object.entries(plan.errors || {}).filter(([, m]) => /caption/i.test(m)).map(([id]) => id) : [];
    if (capIds.length) {
      help.append(h("div", { class: "callout warn" },
        h("div", null, `${plural(capIds.length, "short")} ${capIds.length === 1 ? "needs" : "need"} a caption before ${approveBox.checked ? "it can be approved" : "posting"}.`),
        h("button", { class: "btn sm", type: "button", onclick: async () => {
          try {
            const r = await apiPost("/api/bulk", { action: "captions", ids: capIds });
            reportBulk(r, (k) => `Writing captions for ${plural(k, "short")}…`, "queued");
            waitingCaptions = true; refreshState();
          } catch (e) { toast(errText(e), { kind: "error" }); }
        } }, icon("sparkle", 14), "Write captions first")));
    }
    if (waitingCaptions) help.append(h("div", { class: "faint sm" }, "Captions are being written — I'll re-check the schedule when they're done."));
  }

  async function preview() {
    const mine = ++seq;
    const sel = chosenOrdered();
    if (!sel.length) { plan = null; paintResults(); return; }
    busy = true; paintResults();
    try {
      const r = await apiPost("/api/schedule/auto", { ids: sel, start: startInput.value || null, approve: approveBox.checked, dry_run: true });
      if (mine !== seq) return;
      plan = r;
    } catch (e) {
      if (mine !== seq) return;
      plan = { assigned: {}, errors: Object.fromEntries(sel.map((id) => [id, errText(e)])) };
    }
    busy = false; paintResults();
  }

  async function confirm(handle) {
    busy = true; paintResults();
    try {
      const r = await apiPost("/api/schedule/auto", { ids: chosenOrdered(), start: startInput.value || null, approve: approveBox.checked, dry_run: false });
      const got = Object.entries(r.assigned || {});
      const errs = Object.entries(r.errors || {});
      if (errs.length) toast(`${errs.length} couldn't be scheduled`, { kind: "error", details: errs.map(([id, m]) => `${titleOf(postById(id) || { id })}: ${m}`) });
      if (!got.length) { busy = false; paintResults(); return false; }
      toast(`Scheduled ${plural(got.length, "post")}${approveBox.checked ? " and approved" : " (still drafts — approve to post)"}.`, { kind: "success" });
      for (const [id] of got) ui.selected.delete(id);
      const first = got.map(([, iso]) => iso).sort()[0];
      ui.calAnchor = ymdIn(first);
      await refreshState();
      go("#/calendar");
      return true;
    } catch (e) {
      busy = false; paintResults();
      toast(errText(e), { kind: "error" });
      return false;
    }
  }

  // reorder by dragging rows
  let dragRow = null;
  listEl.addEventListener("dragstart", (e) => {
    const row = e.target.closest?.(".sched-row"); if (!row) return;
    dragRow = row; row.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", row.dataset.id);
  });
  listEl.addEventListener("dragover", (e) => {
    if (!dragRow) return; e.preventDefault();
    const over = e.target.closest(".sched-row"); if (!over || over === dragRow) return;
    const r = over.getBoundingClientRect();
    listEl.insertBefore(dragRow, e.clientY > r.top + r.height / 2 ? over.nextSibling : over);
  });
  listEl.addEventListener("drop", (e) => e.preventDefault());
  listEl.addEventListener("dragend", () => {
    if (!dragRow) return;
    dragRow.classList.remove("dragging"); dragRow = null;
    order = [...listEl.children].map((r) => r.dataset.id); preview();
  });

  const settingsLink = h("a", { href: "#/settings/schedule", class: "link", onclick: () => handle.close() }, "Edit cadence");
  const content = h("div", { class: "sched" },
    h("div", { class: "sched-top" },
      h("div", { class: "field" }, h("label", null, "Start date"), startInput),
      h("div", { class: "cadence" }, h("div", { class: "faint sm" }, "Posting cadence"), h("div", null, cadenceSummary()), settingsLink)),
    checklist ? h("div", { class: "sched-bar" },
      h("button", { class: "btn sm ghost", type: "button", onclick: () => { order.forEach((id) => chosen.add(id)); rows.forEach((o) => { if (o.cb) o.cb.checked = true; }); preview(); } }, "Select all"),
      h("button", { class: "btn sm ghost", type: "button", onclick: () => { chosen.clear(); rows.forEach((o) => { if (o.cb) o.cb.checked = false; }); preview(); } }, "None"),
      h("span", { class: "spacer" }),
      h("span", { class: "faint sm" }, "Drag to reorder")) : h("div", { class: "sched-bar" }, h("span", { class: "faint sm" }, "Posts fill the open slots in this order — drag to reorder"), h("span", { class: "spacer" })),
    listEl, summary, help,
    h("div", { class: "sched-opts" },
      h("label", { class: "check" }, approveBox, h("span", null, "Approve them so they post automatically")),
      h("div", { class: "sched-tools" },
        h("button", { class: "btn sm ghost", type: "button", onclick: () => { order.reverse(); paintList(); preview(); } }, "Reverse order"),
        h("button", { class: "btn sm ghost", type: "button", onclick: () => {
          for (let i = order.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [order[i], order[j]] = [order[j], order[i]]; }
          paintList(); preview();
        } }, icon("shuffle", 14), "Shuffle order"),
        h("button", { class: "btn sm", type: "button", onclick: preview }, "Preview"))));

  startInput.addEventListener("change", preview);
  approveBox.addEventListener("change", preview);

  const btnSchedule = { label: "Schedule", kind: "primary", onClick: confirm };
  const handle = openModal({
    title: checklist ? "Auto-schedule unscheduled shorts" : `Auto-schedule ${plural(order.length, "short")}`,
    content, wide: true,
    actions: [{ label: "Cancel", kind: "ghost", value: false }, btnSchedule],
    onClose: () => { stop(); },
  });
  btnSchedule.el.disabled = true;

  // When captions finish, re-run the preview.
  let lastQueueLen = 0;
  const stop = onState(() => {
    const q = store.state?.caption_queue;
    const len = (q?.running ? 1 : 0) + (q?.queued?.length || 0);
    if (waitingCaptions && lastQueueLen > 0 && len === 0) { waitingCaptions = false; preview(); }
    lastQueueLen = len;
  });

  paintList();
  preview();
}
