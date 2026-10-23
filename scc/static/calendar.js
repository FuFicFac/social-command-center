// calendar.js — week + month planner with drag & drop, slot picker and an Unscheduled tray.
import {
  h, icon, thumbImg, platformDots, openModal, toast, apiGet, apiPost, errText, refreshState, openPost,
  posts, postById, titleOf, isLocked, isUnscheduled, settings, ui, localSlots, minuteKey, zonedToUtc, toWire,
  ymdIn, hhmmIn, todayYmd, addDays, addMonths, weekStart, dowOf, fmtYmd, fmtSlot, fmtTime, fmt12, DAY_KEYS, plural, debounce,
} from "./core.js";
import { openAutoSchedule } from "./schedule.js";

let dragId = null; // id of the post being dragged (dataTransfer can't be read during dragover)

export function createCalendar() {
  if (!ui.calAnchor) ui.calAnchor = todayYmd();
  const root = h("div", { class: "view calendar" });
  const label = h("div", { class: "cal-label" });
  const segWeek = h("button", { class: "seg-btn", type: "button", onclick: () => setMode("week") }, "Week");
  const segMonth = h("button", { class: "seg-btn", type: "button", onclick: () => setMode("month") }, "Month");
  const grid = h("div", { class: "cal-grid" });
  const tray = h("aside", { class: "cal-tray" });
  root.append(
    h("div", { class: "view-head" }, h("div", null, h("h1", null, "Calendar"), h("div", { class: "sub" }, "Drag shorts onto a time to schedule them.")),
      h("div", { class: "head-actions" }, h("button", { class: "btn primary", type: "button", onclick: () => openAutoSchedule({ checklist: true }) }, icon("sparkle", 16), "Auto-schedule…"))),
    h("div", { class: "cal-toolbar" },
      h("div", { class: "nav-btns" },
        h("button", { class: "icon-btn", type: "button", "aria-label": "Previous", onclick: () => shift(-1) }, icon("chevL")),
        h("button", { class: "btn sm", type: "button", onclick: () => { ui.calAnchor = todayYmd(); render(true); } }, "Today"),
        h("button", { class: "icon-btn", type: "button", "aria-label": "Next", onclick: () => shift(1) }, icon("chevR"))),
      label, h("span", { class: "spacer" }), h("div", { class: "seg" }, segWeek, segMonth)),
    h("div", { class: "cal-main" }, h("div", { class: "cal-gridwrap" }, grid), tray));

  let lastSig = "", slots = { key: "", ok: false, list: [] }, slotSeq = 0, pending = false;

  function setMode(m) { ui.calMode = m; render(true); }
  function shift(n) {
    ui.calAnchor = ui.calMode === "week" ? addDays(ui.calAnchor, 7 * n) : addMonths(ui.calAnchor, n);
    render(true);
  }
  const range = () => {
    if (ui.calMode === "week") return { start: weekStart(ui.calAnchor), days: 7 };
    return { start: weekStart(ui.calAnchor.slice(0, 8) + "01"), days: 42 };
  };

  // ── slot data: API for the future (authoritative), settings for the past / when the API is unavailable ──
  async function loadSlots(start, days, key) {
    const mine = ++slotSeq;
    try {
      const r = await apiGet(`/api/slots?from=${start}&days=${days}`);
      if (mine !== slotSeq) return;
      slots = { key, ok: true, list: r.slots || [], sched: slots.sched };
    } catch { if (mine !== slotSeq) return; slots = { key, ok: false, list: [], sched: slots.sched }; }
    render(true);
  }
  function slotsByDay(start, days) {
    const now = Date.now();
    const local = localSlots(start, days);
    let list = local;
    if (slots.ok && slots.key === `${start}|${days}`) {
      list = [...local.filter((s) => Date.parse(s.at) <= now),
        ...slots.list.map((s) => ({ at: s.at, ymd: ymdIn(s.at), time: hhmmIn(s.at), post_id: s.post_id }))];
    }
    const map = new Map();
    for (const s of list.sort((a, b) => Date.parse(a.at) - Date.parse(b.at))) {
      if (!map.has(s.ymd)) map.set(s.ymd, []);
      map.get(s.ymd).push(s);
    }
    return map;
  }

  // ── moving posts ──
  const setWhen = (id, iso) => apiPost(`/api/posts/${id}`, { scheduled_at: iso });
  const occupant = (id, iso) => posts().find((q) => q.id !== id && q.scheduled_at && minuteKey(q.scheduled_at) === minuteKey(iso));
  async function nextFreeAfter(iso) {
    try {
      const r = await apiGet(`/api/slots?from=${ymdIn(iso)}&days=120`);
      return (r.slots || []).find((s) => !s.post_id && Date.parse(s.at) > Date.parse(iso))?.at || null;
    } catch {
      const taken = new Set(posts().filter((p) => p.scheduled_at).map((p) => minuteKey(p.scheduled_at)));
      return localSlots(ymdIn(iso), 120).find((s) => Date.parse(s.at) > Date.parse(iso) && !taken.has(minuteKey(s.at)))?.at || null;
    }
  }
  async function place(id, iso) {
    const p = postById(id); if (!p || isLocked(p)) return;
    if (Date.parse(iso) < Date.now()) return toast("That time has already passed.", { kind: "info" });
    if (p.scheduled_at && minuteKey(p.scheduled_at) === minuteKey(iso)) return;
    const occ = occupant(id, iso);
    try {
      if (!occ) { await setWhen(id, iso); toast(`Moved to ${fmtSlot(iso)}.`, { kind: "success" }); }
      else if (isLocked(occ)) { toast("That slot already holds a posted short.", { kind: "info" }); }
      else if (p.scheduled_at) {
        const old = p.scheduled_at;
        await setWhen(id, iso);
        try { await setWhen(occ.id, old); } catch (e) { await setWhen(id, old).catch(() => {}); throw e; }
        toast(`Swapped “${titleOf(p)}” and “${titleOf(occ)}”.`, { kind: "success" });
      } else {
        const free = await nextFreeAfter(iso);
        if (!free) { toast("No free slot to move the other short to.", { kind: "error" }); return; }
        await setWhen(occ.id, free); await setWhen(id, iso);
        toast(`Placed “${titleOf(p)}” there; “${titleOf(occ)}” moved to ${fmtSlot(free)}.`, { kind: "success" });
      }
    } catch (e) { toast(errText(e), { kind: "error" }); }
    refreshState();
  }
  async function placeOnDay(id, ymd) {
    const p = postById(id); if (!p || isLocked(p)) return;
    if (ymd < todayYmd()) return toast("That day has already passed.", { kind: "info" });
    const now = Date.now();
    const daySlots = (slotsByDay(ymd, 1).get(ymd) || []).filter((s) => Date.parse(s.at) > now);
    const free = daySlots.find((s) => !occupant(id, s.at));
    let iso;
    if (p.scheduled_at) {
      iso = toWire(zonedToUtc(ymd, hhmmIn(p.scheduled_at)));
      if (occupant(id, iso)) iso = free?.at;
    } else iso = free?.at || toWire(zonedToUtc(ymd, settings().schedule?.slots?.[0] || "09:00"));
    if (!iso) return toast("No free slot that day — drop it onto a specific slot to swap.", { kind: "info" });
    if (occupant(id, iso)) return toast("No free slot that day — drop it onto a specific slot to swap.", { kind: "info" });
    await place(id, iso);
  }

  // ── drag & drop plumbing ──
  function dropTarget(el, handler) {
    el.addEventListener("dragover", (e) => { if (!dragId) return; e.preventDefault(); e.dataTransfer.dropEffect = "move"; el.classList.add("dragover"); });
    el.addEventListener("dragleave", (e) => { if (!el.contains(e.relatedTarget)) el.classList.remove("dragover"); });
    el.addEventListener("drop", (e) => {
      e.preventDefault(); e.stopPropagation(); el.classList.remove("dragover");
      const id = dragId; dragId = null; if (id) handler(id);
    });
  }
  function draggable(el, p) {
    if (isLocked(p)) return;
    el.draggable = true;
    el.addEventListener("dragstart", (e) => {
      dragId = p.id; e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", p.id);
      requestAnimationFrame(() => el.classList.add("dragging")); root.classList.add("is-dragging");
    });
    el.addEventListener("dragend", () => {
      dragId = null; el.classList.remove("dragging"); root.classList.remove("is-dragging");
      root.querySelectorAll(".dragover").forEach((x) => x.classList.remove("dragover"));
      if (pending) { pending = false; render(true); }
    });
  }

  // ── pieces ──
  function chip(p, { compact = false } = {}) {
    const el = h("div", { class: `cchip b-${p.bucket}${p.approved_at ? "" : " unapproved"}${compact ? " compact" : ""}`, tabindex: 0, dataset: { id: p.id },
      title: `${titleOf(p)}${p.scheduled_at ? " — " + fmtSlot(p.scheduled_at) : ""}${p.approved_at ? "" : " (not approved)"}` },
    h("div", { class: "cchip-thumb" }, thumbImg(p)),
    h("div", { class: "cchip-body" }, h("div", { class: "cchip-title" }, titleOf(p)), platformDots(p)));
    el.addEventListener("click", () => openPost(p.id));
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") openPost(p.id); });
    draggable(el, p);
    return el;
  }
  function dayHeaderLabel(ymd) { return [h("span", { class: "dname" }, fmtYmd(ymd, { weekday: "short" })), h("span", { class: "dnum" }, String(+ymd.slice(8)))]; }

  function renderWeek(start, byDay) {
    const today = todayYmd(), now = Date.now();
    const bySlot = new Map(), extras = new Map();
    const sched = posts().filter((p) => p.scheduled_at);
    for (let i = 0; i < 7; i++) { extras.set(addDays(start, i), []); }
    const slotKeys = new Set();
    for (const [, list] of byDay) for (const s of list) slotKeys.add(minuteKey(s.at));
    for (const p of sched) {
      const k = minuteKey(p.scheduled_at);
      if (slotKeys.has(k) && !bySlot.has(k)) bySlot.set(k, p);
      else { const d = ymdIn(p.scheduled_at); if (extras.has(d)) extras.get(d).push(p); }
    }
    const cols = [];
    const noCadence = ![...byDay.values()].some((l) => l.length) && !(settings().schedule?.slots || []).length;
    for (let i = 0; i < 7; i++) {
      const ymd = addDays(start, i);
      const dayPast = ymd < today;
      const col = h("div", { class: `dcol${ymd === today ? " today" : ""}${dayPast ? " past" : ""}`, dataset: { ymd } },
        h("div", { class: "dhead" }, dayHeaderLabel(ymd)));
      const body = h("div", { class: "dbody" });
      for (const s of byDay.get(ymd) || []) {
        const k = minuteKey(s.at), p = bySlot.get(k), past = Date.parse(s.at) <= now;
        const cell = h("div", { class: `slot ${p ? "filled" : "empty"}${past ? " past" : ""}`, dataset: { at: s.at } },
          h("div", { class: "slot-time" }, fmt12(s.time || hhmmIn(s.at))));
        if (p) cell.append(chip(p));
        else if (!past) cell.append(h("button", { class: "slot-add", type: "button", onclick: () => openSlotPicker(s) }, "+ Add"));
        if (!past) dropTarget(cell, (id) => place(id, s.at));
        body.append(cell);
      }
      for (const p of (extras.get(ymd) || []).sort((a, b) => Date.parse(a.scheduled_at) - Date.parse(b.scheduled_at))) {
        body.append(h("div", { class: "slot extra" }, h("div", { class: "slot-time" }, fmtTime(p.scheduled_at)), chip(p)));
      }
      if (!dayPast) dropTarget(body, (id) => placeOnDay(id, ymd));
      if (!body.children.length) body.append(h("div", { class: "day-empty" }, noCadence ? "" : "—"));
      col.append(body);
      cols.push(col);
    }
    const nodes = [h("div", { class: "week" }, cols)];
    if (noCadence) nodes.unshift(h("div", { class: "callout warn" }, "No posting times are set yet. ", h("a", { class: "link", href: "#/settings/schedule" }, "Set your cadence →")));
    return nodes;
  }

  function renderMonth(start) {
    const today = todayYmd();
    const month = ui.calAnchor.slice(0, 7);
    const byDay = new Map();
    for (const p of posts().filter((q) => q.scheduled_at)) {
      const d = ymdIn(p.scheduled_at);
      if (!byDay.has(d)) byDay.set(d, []);
      byDay.get(d).push(p);
    }
    const head = h("div", { class: "mhead" }, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => h("div", null, d)));
    const cells = [];
    for (let i = 0; i < 42; i++) {
      const ymd = addDays(start, i);
      const list = (byDay.get(ymd) || []).sort((a, b) => Date.parse(a.scheduled_at) - Date.parse(b.scheduled_at));
      const cell = h("div", { class: `mday${ymd.slice(0, 7) !== month ? " other" : ""}${ymd === today ? " today" : ""}${ymd < today ? " past" : ""}`, dataset: { ymd } },
        h("button", { class: "mnum", type: "button", title: "Open this week", onclick: () => { ui.calAnchor = ymd; ui.calMode = "week"; render(true); } }, String(+ymd.slice(8))));
      for (const p of list.slice(0, 3)) {
        const c = h("div", { class: `mchip b-${p.bucket}${p.approved_at ? "" : " unapproved"}`, tabindex: 0, title: `${titleOf(p)} — ${fmtSlot(p.scheduled_at)}` },
          h("span", { class: "mt" }, fmtTime(p.scheduled_at).replace(" ", "").toLowerCase()), h("span", { class: "mtitle" }, titleOf(p)));
        c.addEventListener("click", () => openPost(p.id));
        c.addEventListener("keydown", (e) => { if (e.key === "Enter") openPost(p.id); });
        draggable(c, p);
        cell.append(c);
      }
      if (list.length > 3) cell.append(h("button", { class: "mmore", type: "button", onclick: () => { ui.calAnchor = ymd; ui.calMode = "week"; render(true); } }, `+${list.length - 3} more`));
      if (ymd >= today) dropTarget(cell, (id) => placeOnDay(id, ymd));
      cells.push(cell);
    }
    return [h("div", { class: "month" }, head, h("div", { class: "mgrid" }, cells))];
  }

  function renderTray() {
    const un = posts().filter(isUnscheduled);
    tray.replaceChildren(...[
      h("div", { class: "tray-head" }, h("strong", null, `Unscheduled (${un.length})`)),
      h("div", { class: "faint sm tray-sub" }, "Drag onto a slot or a day."),
      h("div", { class: "tray-list" }, un.length
        ? un.map((p) => { const c = chip(p, { compact: true }); if (p.approved_at) c.append(h("span", { class: "asap", title: "Approved with no time: it posts as soon as the scheduler runs" }, "ASAP")); return c; })
        : h("div", { class: "empty small" }, posts().length ? "Everything has a time. Nice." : "No shorts yet — import some from the Library.")),
      un.length ? h("button", { class: "btn sm", type: "button", onclick: () => openAutoSchedule({ checklist: true }) }, icon("sparkle", 14), "Auto-schedule these…") : null].filter(Boolean));
    // tray is also a drop target: dropping a scheduled post here clears its time (only if it isn't approved)
    tray.classList.remove("dragover");
  }
  dropTarget(tray, async (id) => {
    const p = postById(id);
    if (!p || !p.scheduled_at) return;
    if (p.approved_at) return toast("Unapprove it first — an approved short without a time posts right away.", { kind: "info" });
    try { await apiPost("/api/schedule/clear", { ids: [id] }); toast("Moved back to Unscheduled.", { kind: "success" }); } catch (e) { toast(errText(e), { kind: "error" }); }
    refreshState();
  });

  // ── slot picker ──
  function openSlotPicker(slot) {
    const approveBox = h("input", { type: "checkbox", checked: true });
    const q = h("input", { class: "input", type: "search", placeholder: "Search unscheduled shorts", "aria-label": "Search" });
    const list = h("div", { class: "picker-list", role: "radiogroup" });
    let chosen = null;
    const paint = () => {
      const term = q.value.trim().toLowerCase();
      const items = posts().filter(isUnscheduled).filter((p) => !term || titleOf(p).toLowerCase().includes(term) || (p.filename || "").toLowerCase().includes(term));
      list.replaceChildren(...(items.length ? items.map((p) => {
        const r = h("input", { type: "radio", name: "pick", checked: chosen === p.id, onchange: () => { chosen = p.id; handle.actions[1].el.disabled = false; } });
        return h("label", { class: "picker-row" }, r, h("div", { class: "sched-thumb" }, thumbImg(p)),
          h("div", { class: "sched-title" }, h("div", { class: "clamp2" }, titleOf(p)), h("div", { class: "faint sm" }, p.approved_at ? "Approved · posts ASAP" : (p.caption?.trim() ? "Draft" : "Draft · no caption yet"))));
      }) : [h("div", { class: "empty small" }, "No unscheduled shorts. Import more, or take one off another day.")]));
    };
    q.addEventListener("input", debounce(paint, 100));
    const handle = openModal({
      title: `Add a short · ${fmtSlot(slot.at, " at ")}`,
      content: h("div", { class: "picker" }, q, list, h("label", { class: "check" }, approveBox, h("span", null, "Approve it too (so it posts automatically)"))),
      actions: [{ label: "Cancel", kind: "ghost", value: false },
        { label: "Schedule here", kind: "primary", onClick: async () => {
          if (!chosen) return false;
          const id = chosen, p = postById(id);
          try {
            await setWhen(id, slot.at);
            if (approveBox.checked && !p.approved_at) {
              try { await apiPost(`/api/posts/${id}/approve`, {}); toast(`Scheduled and approved for ${fmtSlot(slot.at)}.`, { kind: "success" }); }
              catch (e) { toast("Scheduled, but it couldn't be approved yet.", { kind: "error", details: e.issues?.length ? e.issues : [errText(e)] }); }
            } else toast(`Scheduled for ${fmtSlot(slot.at)}.`, { kind: "success" });
            refreshState();
          } catch (e) { toast(errText(e), { kind: "error" }); return false; }
        } }],
    });
    handle.actions = [null, { el: null }];
    handle.actions[1].el = handle.foot.lastChild; handle.actions[1].el.disabled = true;
    paint();
  }

  // ── render ──
  function render(force = false) {
    if (dragId) { pending = true; return; }
    const { start, days } = range();
    const key = `${start}|${days}`;
    const schedSig = posts().map((p) => `${p.id}@${p.scheduled_at || ""}`).join(",");
    const sig = JSON.stringify([ui.calMode, ui.calAnchor, key, slots.key === key, slots.ok,
      posts().map((p) => [p.id, p.scheduled_at, p.bucket, p.title, p.approved_at, p.updated_at, Object.values(p.deliveries || {}).map((d) => d.status)]),
      settings().schedule, settings().timezone]);
    if (!force && sig === lastSig) return;
    lastSig = sig;
    if (slots.key !== key || (slots.ok && slots.sched !== schedSig)) {
      slots.sched = schedSig;
      if (slots.key !== key) slots = { key: "", ok: false, list: [], sched: schedSig };
      loadSlots(start, days, key);
    }
    segWeek.classList.toggle("on", ui.calMode === "week");
    segMonth.classList.toggle("on", ui.calMode === "month");
    if (ui.calMode === "week") {
      const end = addDays(start, 6);
      label.textContent = `${fmtYmd(start)} – ${fmtYmd(end, start.slice(0, 7) === end.slice(0, 7) ? { day: "numeric" } : { month: "short", day: "numeric" })}, ${end.slice(0, 4)}`;
      grid.replaceChildren(...renderWeek(start, slotsByDay(start, 7)));
    } else {
      label.textContent = fmtYmd(ui.calAnchor.slice(0, 8) + "01", { month: "long", year: "numeric" });
      grid.replaceChildren(...renderMonth(start));
    }
    grid.dataset.mode = ui.calMode;
    renderTray();
  }

  render(true);
  return { el: root, update: () => render(false), destroy() { slotSeq++; } };
}
