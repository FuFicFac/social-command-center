// editor.js — the right-hand Editor drawer. Opened by route #/post/<id>.
// Poll-safety: the form is the user's; server updates only patch status regions unless the form is clean.
import {
  h, icon, platIcon, statusChip, confirmModal, apiGet, apiPost, errText, refreshState, toast, safeUrl,
  postById, titleOf, jobOf, isLocked, effectiveCaption, ORDER, PLAT, ROUTE_LABEL,
  platOf, destinationsOf, accounts, brands, brandName, accountById,
  S, settings, tz, zonedToUtc, toWire, ymdIn, hhmmIn, todayYmd, addDays, fmtSlot, fmt12, localSlots, minuteKey, posts,
  fmtDuration, plural, $,
} from "./core.js";

let drawer, scrim, onCloseNav = () => {};
let ed = null; // the open editor session

export function initEditor({ closeNav }) {
  drawer = $("#drawer"); scrim = $("#drawerScrim"); onCloseNav = closeNav;
  scrim.addEventListener("click", () => requestClose());
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !ed) return;
    if (document.querySelector("dialog[open], .popover")) return;
    e.preventDefault(); requestClose();
  });
  window.addEventListener("beforeunload", (e) => { if (ed && isDirty()) { e.preventDefault(); e.returnValue = ""; } });
}
export const editorId = () => ed?.id || null;
export const editorDirty = () => !!ed && isDirty();

// ─────────────────────────────────────────────────────────────── form model ──
function formFromPost(p) {
  const ov = {}, custom = {};
  for (const pl of ORDER) { const t = p.overrides?.[pl]?.caption || ""; ov[pl] = t; custom[pl] = !!t.trim(); }
  return {
    // targets holds destination keys: "tiktok:<account id>", or "tiktok" with no account chosen.
    title: p.title || "", caption: p.caption || "", ov, custom, brand: p.brand || "",
    targets: new Set(destinationsOf(p).map((d) => d.key)),
    date: p.scheduled_at ? ymdIn(p.scheduled_at) : "", time: p.scheduled_at ? hhmmIn(p.scheduled_at) : "",
  };
}
const cloneForm = (f) => ({ ...f, ov: { ...f.ov }, custom: { ...f.custom }, targets: new Set(f.targets) });
function snap(f) {
  return JSON.stringify([f.title, f.caption, ORDER.map((pl) => (f.custom[pl] ? f.ov[pl].trim() : "")), [...f.targets].sort(), f.date, f.time, f.brand]);
}
const keysFor = (f, pl) => [...f.targets].filter((k) => platOf(k) === pl);
const hasPlat = (f, pl) => keysFor(f, pl).length > 0;
const platsOf = (f) => ORDER.filter((pl) => hasPlat(f, pl));
const destKey = (a) => `${a.platform}:${a.id}`;
/** The account a brand suggests for a platform (its default, else its only one). */
function suggested(brand, pl) {
  const mine = accounts().filter((a) => a.platform === pl && a.brand_id === brand && a.status !== "missing");
  return brand ? (mine.find((a) => a.is_default) || mine[0] || null) : null;
}
const isDirty = () => snap(ed.form) !== snap(ed.base);

/** form date+time → wire ISO | null | undefined (invalid) */
function scheduledFromForm(f) {
  if (!f.date && !f.time) return null;
  const date = f.date || todayYmd();
  const time = f.time || (settings().schedule?.slots?.[0] ?? "09:00");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !/^\d{2}:\d{2}$/.test(time)) return undefined;
  return toWire(zonedToUtc(date, time));
}
function buildPatch() {
  const f = ed.form, b = ed.base, patch = {};
  if (f.title !== b.title) patch.title = f.title;
  if (f.caption !== b.caption) patch.caption = f.caption;
  const ovKey = (x) => ORDER.map((pl) => (x.custom[pl] ? x.ov[pl].trim() : "")).join("\u0000");
  if (ovKey(f) !== ovKey(b)) {
    const next = {};
    for (const pl of ORDER) {
      const o = { ...(ed.post.overrides?.[pl] || {}) };
      if (f.custom[pl] && f.ov[pl].trim()) o.caption = f.ov[pl]; else delete o.caption;
      if (Object.keys(o).length) next[pl] = o;
    }
    patch.overrides = next;
  }
  if (f.brand !== b.brand) patch.brand = f.brand || null;
  // Destinations are sent whole whenever they or the brand changed, so what is saved is what is on screen.
  if (f.brand !== b.brand || [...f.targets].sort().join() !== [...b.targets].sort().join()) {
    patch.destinations = [...f.targets].map((k) => ({ platform: platOf(k), account_id: k.split(":")[1] || null }));
  }
  if (f.date !== b.date || f.time !== b.time) patch.scheduled_at = scheduledFromForm(f);
  return patch;
}

// ──────────────────────────────────────────────────────────────── open/close ──
export function openEditor(id) {
  if (ed && ed.id === id) return;
  const post = postById(id);
  if (!post) return;
  closeEditor();
  ed = { id, post, form: formFromPost(post), base: null, tab: ORDER.find((pl) => destinationsOf(post).some((d) => d.platform === pl)) || "tiktok",
    els: {}, seenAt: post.updated_at, aiPending: false, busy: false };
  ed.base = cloneForm(ed.form);
  build();
  drawer.classList.add("open"); scrim.classList.add("open"); drawer.setAttribute("aria-hidden", "false");
  setTimeout(() => ed?.els.title?.focus({ preventScroll: true }), 120);
}
export function closeEditor() {
  if (!ed) return;
  drawer.querySelector("video")?.pause();
  ed = null;
  drawer.classList.remove("open"); scrim.classList.remove("open"); drawer.setAttribute("aria-hidden", "true");
  setTimeout(() => { if (!ed) drawer.replaceChildren(); }, 250);
}
export async function requestClose() {
  if (!ed) return true;
  if (isDirty()) {
    const ok = await confirmModal({ title: "Discard unsaved changes?", message: "You've edited this short but haven't saved. Closing will lose those edits.", confirmLabel: "Discard changes", danger: true, cancelLabel: "Keep editing" });
    if (!ok) return false;
    ed.base = cloneForm(ed.form); // stop the guard from firing again
  }
  onCloseNav();
  return true;
}

// ───────────────────────────────────────────────────────────────────── build ──
function counter(el, len, limit) {
  el.textContent = limit ? `${len.toLocaleString()} / ${limit.toLocaleString()}` : len.toLocaleString();
  el.classList.toggle("over", !!limit && len > limit);
}
function build() {
  const { post, form } = ed, E = ed.els;
  const ctl = (el) => { el.classList.add("f-ctl"); return el; };

  E.title = ctl(h("input", { class: "input", type: "text", value: form.title, placeholder: "Short, punchy title", "aria-label": "Title", oninput: (e) => { form.title = e.target.value; sync(); } }));
  E.titleCount = h("span", { class: "count" });
  E.caption = ctl(h("textarea", { class: "input", rows: 5, placeholder: "Write one caption for every platform — or let AI do it.", "aria-label": "Caption", oninput: (e) => { form.caption = e.target.value; sync(); } }));
  E.caption.value = form.caption;
  E.capCount = h("span", { class: "count" });

  // Platform tabs
  E.tabBtns = {}; E.panels = {};
  const tabs = h("div", { class: "ptabs", role: "tablist" });
  const panels = h("div", { class: "ppanels" });
  for (const pl of ORDER) {
    const btn = h("button", { class: "ptab", type: "button", role: "tab", onclick: () => { ed.tab = pl; sync(); } }, platIcon(pl, 15), PLAT[pl].name);
    E.tabBtns[pl] = btn; tabs.append(btn);
    const dests = h("div", { class: "dest-list" });
    const eff = h("div", { class: "effective" });
    const note = h("div", { class: "limit-note" });
    const customBox = ctl(h("input", { type: "checkbox", checked: form.custom[pl], onchange: (e) => {
      form.custom[pl] = e.target.checked;
      if (e.target.checked && !form.ov[pl].trim()) { form.ov[pl] = effectiveCaption({ caption: form.caption, overrides: {} }, pl); ta.value = form.ov[pl]; }
      sync(); if (e.target.checked) ta.focus();
    } }));
    const ta = ctl(h("textarea", { class: "input", rows: 4, "aria-label": `${PLAT[pl].name} custom caption`, oninput: (e) => { form.ov[pl] = e.target.value; sync(); } }));
    ta.value = form.ov[pl];
    const cnt = h("span", { class: "count" });
    const ovWrap = h("div", { class: "ov-wrap" }, h("div", { class: "label-row" }, h("span", { class: "faint sm" }, `${PLAT[pl].name} ${PLAT[pl].limitWhat} limit`), cnt), ta);
    const panel = h("div", { class: "ppanel", role: "tabpanel" },
      dests,
      pl === "youtube" ? h("div", { class: "faint sm" }, "YouTube uses the title above and this text as the description.") : null,
      pl === "x" ? h("div", { class: "faint sm" }, "Upload-Post removes clickable URLs from X captions.") : null,
      eff, note,
      h("label", { class: "check" }, customBox, h("span", null, `Use a custom caption for ${PLAT[pl].name}`)),
      ovWrap);
    E.panels[pl] = { panel, dests, eff, note, customBox, ta, cnt, ovWrap };
    panels.append(panel);
  }

  // Schedule
  E.date = ctl(h("input", { class: "input", type: "date", value: form.date, "aria-label": "Date", oninput: (e) => { form.date = e.target.value; sync(); } }));
  E.time = ctl(h("input", { class: "input", type: "time", value: form.time, "aria-label": "Time", oninput: (e) => { form.time = e.target.value; sync(); } }));
  E.schedHint = h("div", { class: "hint" });
  const setWhen = (ymd, hhmm) => { form.date = ymd; form.time = hhmm; E.date.value = ymd; E.time.value = hhmm; sync(); };
  const quick = h("div", { class: "quick" },
    ctl(h("button", { class: "btn sm", type: "button", onclick: async (ev) => {
      const b = ev.currentTarget; b.disabled = true;
      try { const s = await nextFreeSlot(); if (!s) return toast("No free slot found in the next 60 days.", { kind: "info" }); setWhen(ymdIn(s), hhmmIn(s)); }
      catch (e) { toast(errText(e), { kind: "error" }); } finally { b.disabled = false; }
    } }, icon("calendar", 14), "Next free slot")),
    ctl(h("button", { class: "btn sm", type: "button", onclick: () => setWhen(addDays(todayYmd(), 1), "09:00") }, "Tomorrow 9 AM")),
    ctl(h("button", { class: "btn sm ghost", type: "button", onclick: () => setWhen("", "") }, "Clear (post as soon as approved)")));

  E.deliveries = h("div", { class: "deliv-list" });
  E.banner = h("div", { class: "ed-banner", hidden: true });
  E.issues = h("div", { class: "ed-issues", hidden: true });
  E.error = h("div", { class: "ed-error", hidden: true });
  E.lockNote = h("div", { class: "callout info", hidden: true });
  E.statusRow = h("div", { class: "ed-status" });
  E.aiBtn = ctl(h("button", { class: "btn sm", type: "button", onclick: rewriteAI }, icon("sparkle", 14), h("span", null, "Rewrite with AI")));
  E.unapproveNote = h("div", { class: "faint sm", hidden: true }, "Saving text changes will un-approve this short — approve it again afterwards.");
  E.destNote = h("div", { class: "hint warn-text", hidden: true }, "Saving a different set of accounts clears the approval — approve it again once the accounts are right.");
  E.brand = ctl(h("select", { class: "input", "aria-label": "Brand", onchange: (e) => {
    form.brand = e.target.value;
    // Re-suggest: every platform already selected switches to this brand's account, when it has one.
    for (const pl of platsOf(form)) {
      const acct = suggested(form.brand, pl);
      if (!acct) continue;
      for (const k of keysFor(form, pl)) form.targets.delete(k);
      form.targets.add(destKey(acct));
    }
    sync();
  } }));
  E.brandHint = h("div", { class: "hint" });

  E.save = h("button", { class: "btn", type: "button", onclick: () => save() }, "Save");
  E.approve = h("button", { class: "btn", type: "button", onclick: toggleApprove }, "Approve");
  E.publish = h("button", { class: "btn danger", type: "button", onclick: postNow }, icon("bolt", 15), "Post now");
  E.remove = h("button", { class: "btn ghost danger", type: "button", onclick: removePost }, icon("trash", 15), "Remove");

  E.video = h("video", { class: "ed-video", controls: true, playsinline: true, preload: "metadata", poster: `/thumb/${post.id}.jpg`, src: `/media/${post.id}` });

  drawer.replaceChildren(
    h("div", { class: "ed-head" },
      h("div", { class: "ed-head-t" }, h("div", { class: "ed-file", title: post.filename }, post.filename), E.statusRow),
      h("button", { class: "icon-btn", type: "button", "aria-label": "Close editor", onclick: () => requestClose() }, icon("x"))),
    h("div", { class: "ed-body" },
      E.banner, E.lockNote,
      h("div", { class: "ed-video-wrap" }, E.video, post.duration != null ? h("span", { class: "vdur" }, fmtDuration(post.duration)) : null),
      h("div", { class: "field" }, h("div", { class: "label-row" }, h("label", null, "Title ", h("span", { class: "faint" }, "· YouTube title")), E.titleCount), E.title),
      h("div", { class: "field" },
        h("div", { class: "label-row" }, h("label", null, "Caption ", h("span", { class: "faint" }, "· default for all platforms")), E.capCount), E.caption,
        h("div", { class: "row-between" }, E.aiBtn, E.unapproveNote)),
      h("div", { class: "field" }, h("label", null, "Brand"), E.brand, E.brandHint),
      h("div", { class: "field" }, h("label", null, "Where it goes ", h("span", { class: "faint" }, "· pick the exact accounts")), tabs, panels, E.destNote),
      h("div", { class: "field" },
        h("label", null, "Schedule"),
        h("div", { class: "when-row" }, E.date, E.time),
        quick, E.schedHint),
      h("div", { class: "field" }, h("label", null, "Where it stands"), E.deliveries)),
    h("div", { class: "ed-foot" }, E.issues, E.error,
      h("div", { class: "ed-foot-row" }, E.save, E.approve, E.publish, h("span", { class: "spacer" }), E.remove)));

  sync();
  patchStatus();
}

// ─────────────────────────────────────────────────────────────── sync & patch ──
/** Re-derive everything that depends on the form (counters, previews, tabs, hints, dirty). */
function sync() {
  if (!ed) return;
  const { form } = ed, E = ed.els;
  counter(E.titleCount, form.title.length, 100);
  const inherited = platsOf(form).filter((pl) => !(form.custom[pl] && form.ov[pl].trim()));
  const lim = inherited.length ? Math.min(...inherited.map((pl) => PLAT[pl].limit)) : null;
  counter(E.capCount, form.caption.length, lim);
  for (const pl of ORDER) {
    const x = E.panels[pl];
    const text = form.custom[pl] && form.ov[pl].trim() ? form.ov[pl] : form.caption;
    const on = hasPlat(form, pl);
    E.tabBtns[pl].classList.toggle("on", ed.tab === pl);
    E.tabBtns[pl].classList.toggle("off", !on);
    E.tabBtns[pl].setAttribute("aria-selected", String(ed.tab === pl));
    x.panel.hidden = ed.tab !== pl;
    paintDestinations(pl);
    x.customBox.checked = form.custom[pl];
    x.ovWrap.hidden = !form.custom[pl];
    if (pl === "youtube") x.eff.replaceChildren(h("div", { class: "eff-title" }, form.title || h("em", { class: "faint" }, "No title yet")), h("div", null, text || h("em", { class: "faint" }, "No description yet")));
    else x.eff.replaceChildren(text ? text : h("em", { class: "faint" }, "Nothing yet — write a caption above or use ✨ Rewrite with AI."));
    const effLen = text.length, over = effLen > PLAT[pl].limit;
    x.note.textContent = over ? `${effLen.toLocaleString()} characters — over ${PLAT[pl].name}'s ${PLAT[pl].limit.toLocaleString()} limit.` : "";
    x.note.hidden = !over;
    if (form.custom[pl]) counter(x.cnt, form.ov[pl].length, PLAT[pl].limit);
  }
  const when = scheduledFromForm(form);
  const hint = ed.els.schedHint;
  hint.className = "hint";
  if (when === undefined) { hint.textContent = "Enter a valid date and time."; hint.classList.add("bad-text"); }
  else if (when === null) hint.textContent = `No time set — it posts as soon as it's approved. Times are in ${tz().replace("_", " ")}.`;
  else {
    const past = Date.parse(when) < Date.now();
    hint.textContent = `${fmtSlot(when)} (${tz().replace("_", " ")})${past ? " — that's in the past, so it will post right away once approved." : ""}`;
    if (past) hint.classList.add("warn-text");
  }
  const dirty = isDirty();
  E.save.disabled = !dirty || ed.busy;
  E.save.classList.toggle("primary", dirty);
  E.save.textContent = dirty ? "Save changes" : "Saved";
  const textChanged = form.title !== ed.base.title || form.caption !== ed.base.caption || ORDER.some((pl) => (form.custom[pl] ? form.ov[pl].trim() : "") !== (ed.base.custom[pl] ? ed.base.ov[pl].trim() : ""));
  E.unapproveNote.hidden = !(textChanged && ed.post.approved_at);
  const destChanged = [...form.targets].sort().join() !== [...ed.base.targets].sort().join() || form.brand !== ed.base.brand;
  E.destNote.hidden = !(destChanged && ed.post.approved_at);
  const bsig = JSON.stringify([brands(), form.brand]);
  if (E.brandSig !== bsig) {
    E.brandSig = bsig;
    E.brand.replaceChildren(h("option", { value: "", selected: !form.brand }, "No brand yet"),
      ...brands().map((b) => h("option", { value: b.id, selected: b.id === form.brand }, b.name)));
  }
  E.brandHint.textContent = form.brand
    ? `Suggesting ${brandName(form.brand)}'s accounts. You can still change any destination below.`
    : "Choose a brand to have its accounts suggested, or pick accounts yourself below.";
}

/** The account checklist for one platform. Rebuilt only when the choices or the selection change. */
function paintDestinations(pl) {
  const { form } = ed, x = ed.els.panels[pl];
  const chosen = keysFor(form, pl);
  const list = accounts().filter((a) => a.platform === pl && (a.status !== "missing" || chosen.includes(destKey(a))));
  const sig = JSON.stringify([list.map((a) => [a.id, a.label, a.brand_id, a.is_default, a.status, a.profile]), chosen, form.brand, isLocked(ed.post)]);
  if (x.destSig === sig) return;
  x.destSig = sig;
  const locked = isLocked(ed.post);
  const box = (key, checked) => {
    const el = h("input", { type: "checkbox", class: "f-ctl", checked, disabled: locked, onchange: (e) => { e.target.checked ? form.targets.add(key) : form.targets.delete(key); sync(); } });
    return el;
  };
  const pick = suggested(form.brand, pl);
  const order = (a) => (a.brand_id === form.brand && form.brand ? 0 : 1);
  const rows = [...list].sort((a, b) => order(a) - order(b)).map((a) => {
    const key = destKey(a);
    const via = a.conn === "direct" ? "direct sign-in" : `Upload-Post profile “${a.profile}”`;
    return h("label", { class: `check dest${chosen.includes(key) ? " on" : ""}` }, box(key, chosen.includes(key)),
      h("span", { class: "dest-main" },
        h("span", { class: "dest-name" }, platIcon(pl, 15), h("strong", null, a.label || a.platform_id),
          a.brand_name ? h("span", { class: "chip chip-muted" }, a.brand_name) : h("span", { class: "chip chip-warn" }, "No brand"),
          pick && pick.id === a.id ? h("span", { class: "chip chip-ok" }, "Suggested") : null,
          a.status === "expired" ? h("span", { class: "chip chip-bad" }, "Reconnect") : null,
          a.status === "missing" ? h("span", { class: "chip chip-bad" }, "Not connected") : null),
        h("span", { class: "faint sm" }, `${a.name && a.name !== a.label ? a.name + " · " : ""}${via}`)));
  });
  // A destination saved before an account was chosen: keep it visible so it can be replaced.
  if (chosen.includes(pl) || !list.length) {
    rows.push(h("label", { class: `check dest${chosen.includes(pl) ? " on" : ""}` }, box(pl, chosen.includes(pl)),
      h("span", { class: "dest-main" },
        h("span", { class: "dest-name" }, platIcon(pl, 15), h("strong", null, `Post to ${PLAT[pl].name}`), list.length ? h("span", { class: "chip chip-warn" }, "No account chosen") : null),
        h("span", { class: "faint sm" }, list.length ? "Pick one of the accounts above instead — a live post needs a named account." : `No ${PLAT[pl].name} account is connected yet. Add one in Connections & Settings.`))));
  }
  const many = chosen.length > 1 ? h("div", { class: "hint" }, `This short goes to ${chosen.length} ${PLAT[pl].name} accounts.`) : null;
  x.dests.replaceChildren(...rows, ...(many ? [many] : []));
}

const BUCKET_CHIP = { draft: ["Draft", "muted"], scheduled: ["Scheduled", "ok"], posting: ["Posting…", "info"], posted: ["Posted", "ok"], attention: ["Needs attention", "bad"] };
/** Patch the regions the server owns: status chips, deliveries, button states. Never touches inputs. */
function patchStatus() {
  const { post } = ed, E = ed.els;
  const [bl, bk] = BUCKET_CHIP[post.bucket] || ["", "muted"];
  E.statusRow.replaceChildren(...[
    h("span", { class: `chip chip-${bk}` }, bl),
    post.approved_at ? h("span", { class: "chip chip-ok" }, "Approved") : null,
    (post.duplicate_of || []).length ? h("span", { class: "chip chip-warn", title: post.duplicate_of.map((id) => postById(id)?.filename || id).join(", ") }, "Possible duplicate") : null].filter(Boolean));

  const sig = JSON.stringify(post.destinations);
  if (E.delivSig !== sig) {
    E.delivSig = sig;
    const rows = (post.destinations || []).filter((d) =>
      d.status !== "skipped" || d.url || d.remote_id || d.meta?.previously_uploaded).map((d) => {
      const pl = d.platform;
      const url = d.url ? safeUrl(d.url) : null;
      const note = d.meta?.note;
      const reconcile = d.needs_request_check;
      const who = d.account || "no account chosen";
      return h("div", { class: "deliv" },
        h("div", { class: "deliv-top" }, platIcon(pl, 16), h("strong", null, PLAT[pl].name),
          h("span", { class: d.account ? "deliv-acct" : "deliv-acct warn-text" }, who),
          d.brand ? h("span", { class: "chip chip-muted" }, d.brand) : null,
          statusChip(d.status), h("span", { class: "spacer" }),
          url ? h("a", { class: "link", href: url, target: "_blank", rel: "noopener noreferrer" }, "Open ↗") : null,
          !reconcile && (d.status === "failed" || d.status === "needs_review") ? h("button", { class: "btn sm", type: "button", onclick: async () => {
            try { await apiPost(`/api/posts/${post.id}/retry`, { keys: [d.key] }); toast(`${PLAT[pl].name} ${d.account || ""} will be tried again.`, { kind: "success" }); refreshState(); }
            catch (e) { toast(errText(e), { kind: "error" }); }
          } }, "Retry") : null),
        d.account_state === "missing" && d.status !== "published" ? h("div", { class: "deliv-err" }, "This account is no longer connected. Reconnect it, or pick another account above.") : null,
        d.account_state === "expired" ? h("div", { class: "deliv-err" }, "This account's sign-in expired. Reconnect it in Upload-Post, then discover accounts again.") : null,
        reconcile || (d.meta?.history_unverified && d.status === "pending") ? h("div", {class:"deliv-note"},
          h("span", null, reconcile ? "Check the original request and the account before retrying. " : "Previous uploads on this account have not been checked. "),
          ...[["existing","Already uploaded"],["absent",reconcile ? "Checked request and account · no upload" : "Checked account · no match"]].map(([decision,label]) =>
            h("button", {type:"button",class:"btn sm",onclick:async () => {
              const url = decision === "existing" ? window.prompt("Original post link, if available", "") : "";
              if (url === null) return;
              try {await apiPost(`/api/posts/${post.id}/history`, {key:d.key,decision,url});await refreshState();}
              catch(e) {toast(errText(e),{kind:"error"});}
            }},label))) : null,
        d.account_id ? h("div", { class: "deliv-note" }, "Account ID · ", accountById(d.account_id)?.platform_id || "saved with this post", d.profile ? ` · via ${d.profile}` : "") : null,
        d.remote_id && d.remote_id !== "dryrun" ? h("div", { class: "deliv-note" }, "Post ID · ", d.remote_id) : null,
        d.meta?.request_id ? h("div", { class: "deliv-note" }, "Upload request · ", d.meta.request_id) : null,
        d.error ? h("div", { class: "deliv-err" }, d.error) : null,
        note ? h("div", { class: "deliv-note" }, note) : null);
    });
    E.deliveries.replaceChildren(...(rows.length ? rows : [h("div", { class: "faint sm" }, "No destinations selected.")]));
  }

  const job = jobOf(post.id);
  const aiBusy = !!job && /caption/.test(job);
  E.aiBtn.disabled = aiBusy || isLocked(post);
  E.aiBtn.lastChild.textContent = aiBusy ? (job.includes("queued") ? "Queued…" : "Drafting…") : "Rewrite with AI";
  E.aiBtn.classList.toggle("busy", aiBusy);

  const approved = !!post.approved_at;
  E.approve.textContent = approved ? "Approved ✓ — Unapprove" : "Approve";
  E.approve.classList.toggle("approved", approved);
  E.approve.disabled = isLocked(post) || ed.busy;
  E.publish.disabled = isLocked(post) || ed.busy || (!!job && !aiBusy);
  E.remove.disabled = post.bucket === "posting";

  const locked = isLocked(post);
  for (const el of drawer.querySelectorAll(".f-ctl")) el.disabled = locked;
  E.lockNote.hidden = !locked;
  if (locked) E.lockNote.textContent = post.bucket === "posting" ? "This short is being posted right now — editing is paused." : "This short has been posted, so it can't be edited.";
}

/** Called on every state poll. */
export function updateEditor() {
  if (!ed) return;
  const post = postById(ed.id);
  if (!post) {
    if (!ed.removing) toast("That short is no longer in the library.", { kind: "info" });
    ed.base = cloneForm(ed.form); onCloseNav(); return;
  }
  // ignore stale responses that predate our own last save
  if (Date.parse(post.updated_at) < Date.parse(ed.seenAt)) { return; }
  const external = post.updated_at !== ed.seenAt;
  ed.post = post;
  if (external) {
    ed.seenAt = post.updated_at;
    if (!isDirty()) {
      const f = formFromPost(post);
      const changedText = f.title !== ed.form.title || f.caption !== ed.form.caption;
      ed.form = f; ed.base = cloneForm(f);
      pushFormToInputs();
      if (ed.aiPending && changedText) { toast("New AI draft loaded.", { kind: "success" }); ed.aiPending = false; }
      hideBanner();
    } else showBanner(post);
  }
  patchStatus();
  sync();
}
function pushFormToInputs() {
  const { form } = ed, E = ed.els;
  E.title.value = form.title; E.caption.value = form.caption; E.date.value = form.date; E.time.value = form.time;
  for (const pl of ORDER) E.panels[pl].ta.value = form.ov[pl];
  sync();
}
function showBanner(post) {
  const ai = ed.aiPending;
  const E = ed.els;
  E.banner.hidden = false;
  E.banner.replaceChildren(
    h("div", null, ai ? "New AI draft available — load it?" : "This short was updated elsewhere."),
    h("div", { class: "banner-actions" },
      h("button", { class: "btn sm primary", type: "button", onclick: () => {
        // Take the server's text; keep the user's schedule/platform edits.
        const f = formFromPost(post);
        ed.form.title = f.title; ed.form.caption = f.caption; ed.form.ov = f.ov; ed.form.custom = f.custom;
        ed.base.title = f.title; ed.base.caption = f.caption; ed.base.ov = { ...f.ov }; ed.base.custom = { ...f.custom };
        ed.aiPending = false; hideBanner(); pushFormToInputs();
      } }, "Load it"),
      h("button", { class: "btn sm ghost", type: "button", onclick: () => { ed.aiPending = false; hideBanner(); } }, "Keep mine")));
}
function hideBanner() { if (ed) { ed.els.banner.hidden = true; ed.els.banner.replaceChildren(); } }

// ──────────────────────────────────────────────────────────────────── actions ──
async function nextFreeSlot() {
  try {
    const r = await apiGet(`/api/slots?from=${todayYmd()}&days=60`);
    const s = (r.slots || []).find((x) => !x.post_id);
    return s ? new Date(s.at) : null;
  } catch (e) {
    if (e.status !== 404) throw e;
    const taken = new Set(posts().filter((p) => p.scheduled_at && p.id !== ed?.id).map((p) => minuteKey(p.scheduled_at)));
    const s = localSlots(todayYmd(), 60).find((x) => Date.parse(x.at) > Date.now() && !taken.has(minuteKey(x.at)));
    return s ? new Date(s.at) : null;
  }
}
function setBusy(b) { ed.busy = b; sync(); patchStatus(); }
function showError(msg, issues) {
  const E = ed.els;
  E.error.hidden = !msg; E.error.textContent = msg || "";
  E.issues.hidden = !(issues && issues.length);
  E.issues.replaceChildren(...(issues && issues.length ? [h("div", { class: "issues-title" }, msg || "Fix these first"), h("ul", null, issues.map((i) => h("li", null, i)))] : []));
  if (issues && issues.length) E.error.hidden = true;
}

async function save() {
  if (!ed) return false;
  const patch = buildPatch();
  if (!Object.keys(patch).length) return true;
  if ("scheduled_at" in patch && patch.scheduled_at === undefined) { showError("Enter a valid date and time."); return false; }
  showError(null);
  setBusy(true);
  const id = ed.id;
  try {
    const r = await apiPost(`/api/posts/${id}`, patch);
    if (!ed || ed.id !== id) return true;
    if (r.post) { ed.post = { ...ed.post, ...r.post }; if (r.post.updated_at) ed.seenAt = r.post.updated_at; }
    ed.base = cloneForm(ed.form);
    if (r.unapproved) toast("Saved — your edits cleared the approval. Approve it again when you're happy.", { kind: "info" });
    else toast("Saved.", { kind: "success" });
    refreshState();
    return true;
  } catch (e) {
    if (ed) showError(errText(e), e.issues);
    toast(errText(e), { kind: "error" });
    return false;
  } finally { if (ed) setBusy(false); }
}

async function toggleApprove() {
  const id = ed.id;
  if (ed.post.approved_at) {
    setBusy(true);
    try { await apiPost(`/api/posts/${id}/unapprove`, {}); toast("Unapproved — back to draft.", { kind: "info" }); await refreshState(); }
    catch (e) { showError(errText(e), e.issues); } finally { if (ed) setBusy(false); }
    return;
  }
  if (isDirty() && !(await save())) return;
  setBusy(true); showError(null);
  try {
    await apiPost(`/api/posts/${id}/approve`, {});
    toast(ed?.post.scheduled_at ? `Approved — posts ${fmtSlot(ed.post.scheduled_at)}.` : "Approved — posts as soon as the scheduler runs.", { kind: "success" });
    await refreshState();
  } catch (e) { if (ed) showError(e.status === 400 ? "This can't be approved yet" : errText(e), e.issues?.length ? e.issues : (e.status === 400 ? [e.message] : [])); }
  finally { if (ed) setBusy(false); }
}

async function postNow() {
  const id = ed.id;
  const targets = ORDER.flatMap((pl) => keysFor(ed.form, pl));
  if (!targets.length) return showError("Pick at least one account first.");
  const plats = S().platforms;
  const isLive = (k) => (plats[platOf(k)]?.route || "dryrun") !== "dryrun";
  const list = h("ul", { class: "post-now-list" }, targets.map((k) => {
    const pl = platOf(k), acct = accountById(k.split(":")[1]), live = isLive(k);
    return h("li", null, platIcon(pl, 16), h("strong", null, PLAT[pl].name),
      h("span", { class: acct ? "" : "warn-text" }, acct ? (acct.label || acct.platform_id) : "no account chosen"),
      acct?.brand_name ? h("span", { class: "chip chip-muted" }, acct.brand_name) : null,
      h("span", { class: `chip ${live ? "chip-bad" : "chip-muted"}` }, live ? "LIVE" : "DRY RUN"),
      live && !acct ? h("span", { class: "faint sm" }, "will wait until an account is chosen") : null);
  }));
  const anyLive = targets.some(isLive);
  const ok = await confirmModal({
    title: "Post this right now?", confirmLabel: anyLive ? "Post it now" : "Run dry-run post", danger: anyLive,
    content: h("div", { class: "modal-text" },
      h("p", null, anyLive ? "This goes out immediately to exactly these accounts — any schedule is ignored." : "Nothing will actually post — every platform is in dry-run mode."), list),
  });
  if (!ok || !ed) return;
  if (isDirty() && !(await save())) return;
  setBusy(true); showError(null);
  try { await apiPost(`/api/posts/${id}/publish`, { confirm: "post" }); toast("Posting started.", { kind: "success" }); await refreshState(); }
  catch (e) { if (ed) showError(errText(e), e.issues); } finally { if (ed) setBusy(false); }
}

async function removePost() {
  const id = ed.id;
  const ok = await confirmModal({ title: "Remove this short?", message: "It leaves the queue. The video file stays in your Shorts folder.", confirmLabel: "Remove", danger: true });
  if (!ok || !ed) return;
  try {
    ed.removing = true;
    await apiPost(`/api/posts/${id}/remove`, {});
    ed.base = cloneForm(ed.form);
    toast("Removed from the queue.", { kind: "success" });
    onCloseNav(); refreshState();
  } catch (e) { if (ed) { ed.removing = false; showError(errText(e)); } }
}

async function rewriteAI() {
  const id = ed.id;
  ed.aiPending = true;
  ed.els.aiBtn.disabled = true; ed.els.aiBtn.lastChild.textContent = "Queued…";
  try { await apiPost(`/api/posts/${id}/captions`, {}); toast("Writing a new caption — it can take a minute.", { kind: "info" }); refreshState(); }
  catch (e) { ed.aiPending = false; toast(errText(e), { kind: "error" }); if (ed) patchStatus(); }
}
