// settings.js — Connections & Settings. Built once per visit; polling only patches status regions,
// so nothing the user is typing (keys, times, folder) is ever overwritten.
import {
  h, icon, platIcon, confirmModal, toast, apiPost, errText, refreshState, S, settings, ORDER, PLAT, ROUTE_LABEL,
  DAY_NAMES, relTime, fmt12, tz, plural, pref,
} from "./core.js";

const ext = (url, text) => h("a", { class: "link", href: url, target: "_blank", rel: "noopener noreferrer" }, text, " ↗");
const ROUTES = ["dryrun", "direct", "upload_post"];
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const ZONES = ["America/Los_Angeles", "America/Denver", "America/Phoenix", "America/Chicago", "America/New_York", "America/Anchorage", "Pacific/Honolulu",
  "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Madrid", "Europe/Athens", "Asia/Tokyo", "Australia/Sydney", "UTC"];

function field(label, input, hint) { return h("div", { class: "field" }, h("label", null, label), input, hint ? h("div", { class: "hint" }, hint) : null); }
const secret = (ph) => h("input", { class: "input", type: "password", autocomplete: "new-password", spellcheck: false, placeholder: ph });
const text = (ph, v = "") => h("input", { class: "input", type: "text", autocomplete: "off", spellcheck: false, placeholder: ph, value: v });
function steps(list) { return h("ol", { class: "steps" }, list.map((s) => h("li", null, s))); }
function panel(title, sub, stepList, body, open = false) {
  return h("details", { class: "setup", open }, h("summary", null, h("span", { class: "sum-t" }, title), h("span", { class: "faint sm" }, sub), icon("chevD", 16)),
    h("div", { class: "setup-body" }, steps(stepList), body));
}
/** Disable a button while an async action runs; surface errors as toasts. */
function act(btn, fn) {
  return async () => {
    btn.disabled = true;
    try { await fn(); } catch (e) { toast(errText(e), { kind: "error", details: e.issues || [] }); } finally { btn.disabled = false; }
  };
}
const connLine = (s) => {
  if (!s || s === "idle") return null;
  if (s.startsWith("waiting")) return ["info", "Waiting for you in the browser… finish signing in there."];
  if (s.startsWith("connected")) return ["ok", "Connected" + (s.includes(":") ? " as" + s.slice(s.indexOf(":") + 1) : "") + "."];
  if (s.startsWith("error")) return ["bad", s.replace(/^error:?\s*/, "") || "Something went wrong."];
  return ["muted", s];
};

export function createSettings(section) {
  const root = h("div", { class: "view settings" });
  const patchers = [];
  const conn = () => S()?.connections || {};
  root.append(h("div", { class: "view-head" }, h("div", null, h("h1", null, "Connections & Settings"), h("div", { class: "sub" }, "Connect your accounts, pick how each one posts, and set your weekly rhythm."))));

  // ───────── platform cards ─────────
  const cardsWrap = h("div", { class: "pcards" });
  for (const pl of ORDER) {
    const routes = ROUTES.filter((r) => r === "dryrun" || r in S().platforms[pl].configured);
    const detail = h("div", { class: "pc-detail" });
    const connState = h("div", { class: "pc-conn" });
    const result = h("div", { class: "pc-result", hidden: true });
    const seg = h("div", { class: "seg route-seg", role: "group", "aria-label": `${PLAT[pl].name} route` });
    const segBtns = {};
    for (const r of routes) {
      segBtns[r] = h("button", { class: "seg-btn", type: "button", onclick: () => setRoute(pl, r) }, ROUTE_LABEL[r]);
      seg.append(segBtns[r]);
    }
    const disc = h("span", { class: "pc-disc" });
    const testBtn = h("button", { class: "btn sm", type: "button" }, "Test connection");
    testBtn.addEventListener("click", act(testBtn, async () => {
      result.hidden = false; result.className = "pc-result"; result.textContent = "Checking…";
      try { const r = await apiPost("/api/connect/verify", { platform: pl }); result.className = `pc-result ${r.ok ? "ok" : "bad"}`; result.textContent = r.detail || (r.ok ? "Looks good." : "That didn't work."); }
      catch (e) { result.className = "pc-result bad"; result.textContent = errText(e); }
    }));
    const dot = h("span", { class: "sdot" });
    cardsWrap.append(h("div", { class: "pcard" },
      h("div", { class: "pc-head" }, platIcon(pl, 22), h("h3", null, PLAT[pl].name), h("span", { class: "spacer" }), dot),
      detail, connState, seg,
      h("div", { class: "pc-actions" }, testBtn, disc), result));
    let discSig = "";
    patchers.push(() => {
      const p = S().platforms[pl];
      detail.textContent = p.detail;
      dot.className = `sdot ${p.route === "dryrun" ? "grey" : p.ready ? "green" : "amber"}`;
      dot.title = p.route === "dryrun" ? "Dry run" : p.ready ? "Live and ready" : "Needs setup";
      for (const r of routes) { const on = p.route === r; segBtns[r].classList.toggle("on", on); segBtns[r].setAttribute("aria-pressed", String(on)); }
      const cs = connLine(S().connect?.[pl]);
      connState.className = `pc-conn ${cs ? cs[0] : ""}`; connState.textContent = cs ? cs[1] : "";
      const sig = JSON.stringify(p.configured);
      if (sig !== discSig) {
        discSig = sig;
        disc.replaceChildren(...ROUTES.filter((r) => p.configured?.[r]).map((r) => {
          const b = h("button", { class: "btn sm ghost danger", type: "button" }, `Disconnect ${ROUTE_LABEL[r]}`);
          b.addEventListener("click", act(b, async () => {
            const message = r === "upload_post" ? "This removes the shared Upload-Post key for every platform." : `This removes the saved ${ROUTE_LABEL[r]} sign-in for ${PLAT[pl].name}.`;
            const ok = await confirmModal({ title: `Disconnect ${ROUTE_LABEL[r]}?`, message, confirmLabel: "Disconnect", danger: true });
            if (!ok) return;
            await apiPost("/api/disconnect", { platform: pl, route: r }); toast(`${PLAT[pl].name} disconnected.`, { kind: "success" }); await refreshState();
          }));
          return b;
        }));
      }
    });
  }
  async function setRoute(pl, route) {
    const p = S().platforms[pl];
    if (p.route === route) return;
    if (route !== "dryrun") {
      const notReady = !p.configured?.[route];
      const ok = await confirmModal({
        title: `Switch ${PLAT[pl].name} to ${ROUTE_LABEL[route]}?`, confirmLabel: "Go live", danger: true,
        content: h("div", { class: "modal-text" }, h("p", null, h("strong", null, "Approved posts will start posting for real.")),
          notReady ? h("p", { class: "warn-text" }, `${ROUTE_LABEL[route]} isn't connected for ${PLAT[pl].name} yet — posts will fail until you finish the setup below.`) : null),
      });
      if (!ok) return;
    }
    try { await apiPost("/api/settings", { routing: { [pl]: route } }); toast(`${PLAT[pl].name} → ${ROUTE_LABEL[route]}.`, { kind: "success" }); await refreshState(); }
    catch (e) { toast(errText(e), { kind: "error", details: e.issues || [] }); }
  }
  root.append(h("section", { class: "card" }, h("h2", null, "Platforms"), h("p", { class: "muted" }, "Each platform starts in Dry run, which shows what would post without posting anything."), cardsWrap));


  // ───────── brands & accounts ─────────
  // Typed inputs live outside the repainted region, so a poll never eats what you're typing.
  const acctBody = h("div", { class: "acct-body" });
  const linkOut = h("div", { class: "pc-result", hidden: true });
  const newBrand = text("New brand name, e.g. I'm on the A-List");
  const addBrand = h("button", { class: "btn", type: "button" }, icon("plus", 14), "Add brand");
  addBrand.addEventListener("click", act(addBrand, async () => {
    if (!newBrand.value.trim()) return toast("Type a brand name first.", { kind: "info" });
    await apiPost("/api/brands", { name: newBrand.value.trim() }); newBrand.value = "";
    toast("Brand added.", { kind: "success" }); await refreshState();
  }));
  const newProfile = text("New profile name, e.g. alist");
  const addProfile = h("button", { class: "btn", type: "button" }, icon("plus", 14), "Create profile");
  addProfile.addEventListener("click", act(addProfile, async () => {
    if (!newProfile.value.trim()) return toast("Type a profile name first.", { kind: "info" });
    await apiPost("/api/accounts/profile", { profile: newProfile.value.trim() }); newProfile.value = "";
    toast("Profile created. Use “Connect accounts” on it to sign in to its socials.", { kind: "success" }); await refreshState();
  }));
  const discoverBtn = h("button", { class: "btn primary", type: "button" }, "Discover accounts");
  discoverBtn.addEventListener("click", act(discoverBtn, async () => {
    const r = await apiPost("/api/accounts/discover", {});
    toast(`Found ${r.detail}.${r.proven ? ` Matched ${plural(r.proven, "earlier upload")} to its account.` : ""}`, { kind: "success" });
    await refreshState();
  }));
  const brandSelect = (current, onPick, blank = "Not assigned") => {
    const sel = h("select", { class: "input sm-select", "aria-label": "Brand" },
      h("option", { value: "", selected: !current }, blank),
      ...(S().brands || []).map((b) => h("option", { value: b.id, selected: b.id === current }, b.name)));
    sel.addEventListener("change", async () => {
      sel.disabled = true;
      try { await onPick(sel.value); await refreshState(); }
      catch (e) { toast(errText(e), { kind: "error" }); sel.value = current || ""; } finally { sel.disabled = false; }
    });
    return sel;
  };
  const acctRow = (a) => h("div", { class: `acct-row st-${a.status}` },
    platIcon(a.platform, 18),
    h("div", { class: "acct-id" }, h("strong", null, a.label || a.platform_id),
      h("div", { class: "faint sm" }, `${PLAT[a.platform].name}${a.name && a.name !== a.label ? " · " + a.name : ""} · ID ${a.platform_id}`),
      a.status !== "verified" ? h("div", { class: "bad-text sm" }, a.detail || (a.status === "expired" ? "Sign-in expired." : "No longer connected.")) : null),
    h("span", { class: "spacer" }),
    a.status === "verified" ? h("span", { class: "chip chip-ok" }, "Verified") : h("span", { class: "chip chip-bad" }, a.status === "expired" ? "Reconnect" : "Not connected"),
    a.brand_id && a.status === "verified" ? (a.is_default
      ? h("span", { class: "chip chip-muted", title: "This is the account the brand suggests for new shorts" }, "Suggested")
      : h("button", { class: "btn sm ghost", type: "button", title: "Suggest this account for the brand's new shorts. Posts already approved keep their account.",
        onclick: async () => { try { await apiPost("/api/accounts/map", { account_id: a.id, brand_id: a.brand_id, default: true }); await refreshState(); } catch (e) { toast(errText(e), { kind: "error" }); } } }, "Make suggested")) : null,
    brandSelect(a.brand_id, (brand) => apiPost("/api/accounts/map", { account_id: a.id, brand_id: brand || null })),
    a.conn === "direct" && a.status === "verified" ? h("button", { class: "btn sm ghost danger", type: "button", onclick: async () => {
      const ok = await confirmModal({ title: `Sign ${a.label || "this channel"} out?`, message: "Its saved sign-in is removed from your Keychain. Other channels are not affected.", confirmLabel: "Sign out", danger: true });
      if (!ok) return;
      try { await apiPost("/api/accounts/remove", { account_id: a.id }); await refreshState(); } catch (e) { toast(errText(e), { kind: "error" }); }
    } }, "Sign out") : null);
  let acctSig = "";
  patchers.push(() => {
    const st = S();
    const sig = JSON.stringify([st.brands, st.accounts, st.profiles, st.upload_post_plan, st.receipt_review, st.settings?.import_brand]);
    if (sig === acctSig) return;
    acctSig = sig;
    const accts = st.accounts || [], profiles = (st.profiles || []).filter((p) => p.present);
    const plan = st.upload_post_plan || {};
    const used = profiles.length, kids = [];
    kids.push(h("div", { class: "acct-brands" }, (st.brands || []).map((b) => {
      const mine = accts.filter((a) => a.brand_id === b.id && a.status !== "missing");
      return h("div", { class: "brand-card" }, h("div", { class: "brand-name" }, b.name),
        mine.length ? mine.map((a) => h("div", { class: "brand-acct" }, platIcon(a.platform, 15), h("span", null, a.label || a.platform_id), a.is_default ? null : h("span", { class: "faint sm" }, "also available"))) : h("div", { class: "faint sm" }, "No accounts yet — assign some below."));
    })));
    kids.push(h("h3", null, "Upload-Post profiles ", h("span", { class: "faint" }, plan.limit ? `· ${used} of ${plan.limit} in use on your plan` : (used ? `· ${used} found` : ""))));
    if (!profiles.length) kids.push(h("div", { class: "callout info" }, "No profiles listed yet. Press Discover accounts to read them from Upload-Post."));
    for (const p of profiles) {
      const mine = accts.filter((a) => a.conn === "upload_post" && a.profile === p.name);
      const live = mine.filter((a) => a.status === "verified");
      const linkBtn = h("button", { class: "btn sm", type: "button" }, "Connect accounts");
      linkBtn.addEventListener("click", act(linkBtn, async () => {
        const r = await apiPost("/api/accounts/link", { profile: p.name });
        const href = r.url && /^https:\/\//.test(r.url) ? r.url : null;
        linkOut.hidden = false; linkOut.className = "pc-result ok";
        linkOut.replaceChildren(`Sign-in page for “${p.name}” is ready: `, href ? ext(href, "open it in Upload-Post") : "no link returned", ". When you've connected the accounts there, come back and press Discover accounts.");
      }));
      kids.push(h("div", { class: "profile-card" },
        h("div", { class: "profile-head" }, h("strong", null, p.name), h("span", { class: "faint sm" }, live.length ? plural(live.length, "account") : "nothing connected yet"), h("span", { class: "spacer" }),
          live.length ? h("span", { class: "faint sm" }, "Assign all to") : null,
          live.length ? brandSelect("", (brand) => brand ? apiPost("/api/accounts/map", { profile: p.name, brand_id: brand }) : Promise.resolve(), "Choose brand…") : null,
          linkBtn),
        mine.map(acctRow)));
    }
    const direct = accts.filter((a) => a.conn === "direct");
    if (direct.length) kids.push(h("h3", null, "YouTube channels signed in directly"), h("div", { class: "profile-card" }, direct.map(acctRow)));
    const review = st.receipt_review || [];
    if (review.length) {
      kids.push(h("h3", { id: "history" }, "Upload history to confirm ", h("span", { class: "faint" }, `· ${review.length}`)),
        h("p", { class: "muted" }, "These earlier uploads don't say which account received them. Until you confirm one, the same file is held for that platform so it can't be posted twice. Their links are kept either way."));
      const byPlat = new Map();
      for (const r of review) { if (!byPlat.has(r.platform)) byPlat.set(r.platform, []); byPlat.get(r.platform).push(r); }
      for (const [pl, rows] of byPlat) {
        const options = accts.filter((a) => a.platform === pl);
        const pickAll = h("select", { class: "input sm-select", "aria-label": `Account for all ${PLAT[pl].name} uploads` }, h("option", { value: "" }, "Choose account…"), ...options.map((a) => h("option", { value: a.id }, `${a.label || a.platform_id}${a.brand_name ? " · " + a.brand_name : ""}`)));
        const allBtn = h("button", { class: "btn sm", type: "button" }, `Confirm all ${rows.length}`);
        allBtn.addEventListener("click", act(allBtn, async () => {
          if (!pickAll.value) return toast("Choose the account first.", { kind: "info" });
          await apiPost("/api/receipts/assign", { account_id: pickAll.value, items: rows.map((r) => ({ sha256: r.sha256, platform: r.platform })) });
          toast(`Confirmed ${plural(rows.length, "upload")}.`, { kind: "success" }); await refreshState();
        }));
        kids.push(h("div", { class: "profile-card" },
          h("div", { class: "profile-head" }, platIcon(pl, 18), h("strong", null, PLAT[pl].name), h("span", { class: "spacer" }),
            options.length ? [pickAll, allBtn] : h("span", { class: "faint sm" }, `Connect a ${PLAT[pl].name} account to confirm these.`)),
          rows.map((r) => {
            const one = h("select", { class: "input sm-select", "aria-label": "Account" }, h("option", { value: "" }, "Which account?"), ...options.map((a) => h("option", { value: a.id }, a.label || a.platform_id)));
            one.addEventListener("change", async () => {
              if (!one.value) return;
              try { await apiPost("/api/receipts/assign", { account_id: one.value, items: [{ sha256: r.sha256, platform: r.platform }] }); await refreshState(); }
              catch (e) { toast(errText(e), { kind: "error" }); one.value = ""; }
            });
            return h("div", { class: "acct-row" }, h("div", { class: "acct-id" }, h("strong", null, r.title || r.post_id),
              h("div", { class: "faint sm" }, `${r.status} · ${relTime(r.recorded_at)}`)), h("span", { class: "spacer" }),
            r.url && /^https?:\/\//.test(r.url) ? ext(r.url, "Open") : null, options.length ? one : null);
          })));
      }
    }
    acctBody.replaceChildren(...kids);
  });
  const importBrand = h("select", { class: "input", "aria-label": "Brand for imported shorts" });
  importBrand.addEventListener("change", async () => {
    try { await apiPost("/api/settings", { import_brand: importBrand.value }); toast("Saved.", { kind: "success" }); await refreshState(); }
    catch (e) { toast(errText(e), { kind: "error" }); }
  });
  let importSig = "";
  patchers.push(() => {
    const sig = JSON.stringify([S().brands, settings().import_brand]);
    if (sig === importSig) return;
    importSig = sig;
    importBrand.replaceChildren(h("option", { value: "", selected: !settings().import_brand }, "Decide per short"),
      ...(S().brands || []).map((b) => h("option", { value: b.id, selected: b.id === settings().import_brand }, b.name)));
  });
  root.append(h("section", { class: "card", id: "accounts" }, h("h2", null, "Brands & accounts"),
    h("p", { class: "muted" }, "Each brand has its own accounts. A short's brand suggests where it goes; you confirm the exact accounts in the editor, and an approved short keeps those accounts even if you change things here later."),
    h("div", { class: "form-actions" }, discoverBtn, h("span", { class: "faint sm" }, "Reads your Upload-Post profiles and the accounts connected to each. Nothing is changed there.")),
    linkOut, acctBody,
    h("div", { class: "form-grid acct-add" },
      field("Add a brand", h("div", { class: "inline-add" }, newBrand, addBrand)),
      field("Add an Upload-Post profile", h("div", { class: "inline-add" }, newProfile, addProfile), "One profile holds one account per platform, so a second TikTok needs its own profile."),
      field("Brand for shorts imported from the Shorts folder", importBrand, "Finished videos from your editor are routed by what's said in them; this covers plain folder imports."))));

  // ───────── setup panels ─────────
  // Upload-Post
  const upKey = secret("API key"), upUser = text("Profile name, e.g. whyaimatters", conn().upload_post?.user || "");
  const upSave = h("button", { class: "btn primary", type: "button" }, "Save & test");
  upSave.addEventListener("click", act(upSave, async () => {
    const body = { user: upUser.value.trim() }; if (upKey.value.trim()) body.api_key = upKey.value.trim();
    const r = await apiPost("/api/connect/upload_post", body);
    upKey.value = "";
    toast(r.detail || (r.ok ? "Upload-Post connected." : "Saved, but the check failed."), { kind: r.ok ? "success" : "error" }); await refreshState();
  }));
  patchers.push(() => { upKey.placeholder = conn().upload_post?.has_api_key ? "•••• saved (leave blank to keep)" : "API key"; });

  // Meta
  const mApp = text("App ID", conn().meta?.app_id || ""), mSecret = secret("App secret"), mTok = secret("Paste the user token");
  const mApp1 = h("button", { class: "btn", type: "button" }, "Save app");
  mApp1.addEventListener("click", act(mApp1, async () => {
    const b = { app_id: mApp.value.trim() }; if (mSecret.value.trim()) b.app_secret = mSecret.value.trim();
    await apiPost("/api/connect/meta/app", b); mSecret.value = ""; toast("Meta app saved.", { kind: "success" }); await refreshState();
  }));
  const pagesBox = h("div", { class: "pages" });
  const mFind = h("button", { class: "btn", type: "button" }, "Find my pages");
  mFind.addEventListener("click", act(mFind, async () => {
    const r = await apiPost("/api/connect/meta/token", { user_token: mTok.value.trim() });
    mTok.value = "";
    const pages = r.pages || [];
    if (!pages.length) { pagesBox.replaceChildren(h("div", { class: "callout warn" }, "No Pages found for that token. Check that you ticked the page permissions.")); return; }
    let pick = pages[0].id;
    const useBtn = h("button", { class: "btn primary", type: "button" }, "Use this page");
    useBtn.addEventListener("click", act(useBtn, async () => {
      const rr = await apiPost("/api/connect/meta/page", { page_id: pick });
      toast(rr.detail || "Page connected.", { kind: "success" }); pagesBox.replaceChildren(); await refreshState();
    }));
    pagesBox.replaceChildren(h("div", { class: "pop-title" }, "Pick the Page to post to"),
      ...pages.map((pg, i) => h("label", { class: "check radio" }, h("input", { type: "radio", name: "metapage", checked: i === 0, onchange: () => { pick = pg.id; } }),
        h("span", null, h("strong", null, pg.name), pg.ig_username ? h("span", { class: "faint" }, ` · Instagram @${pg.ig_username}`) : h("span", { class: "faint" }, " · no Instagram linked")))),
      useBtn);
  }));
  const metaState = h("div", { class: "pc-conn" });
  patchers.push(() => {
    mSecret.placeholder = conn().meta?.has_app_secret ? "•••• saved (leave blank to keep)" : "App secret";
    const m = conn().meta || {};
    metaState.className = `pc-conn ${m.signed_in ? "ok" : ""}`;
    metaState.textContent = m.signed_in ? `Connected to Page ${m.page_id || ""}${m.ig_user_id ? " + Instagram" : ""}.` : "";
  });

  // YouTube
  const yId = text("Client ID", conn().youtube?.client_id || ""), ySecret = secret("Client secret");
  const yState = h("div", { class: "pc-conn" });
  const ySave = h("button", { class: "btn", type: "button" }, "Save client");
  ySave.addEventListener("click", act(ySave, async () => {
    const b = { client_id: yId.value.trim() }; if (ySecret.value.trim()) b.client_secret = ySecret.value.trim();
    await apiPost("/api/connect/youtube/client", b); ySecret.value = ""; toast("YouTube client saved.", { kind: "success" }); await refreshState();
  }));
  const ySign = h("button", { class: "btn primary", type: "button" }, "Sign in with Google");
  ySign.addEventListener("click", act(ySign, async () => { await apiPost("/api/connect/youtube/start", {}); toast("Your browser opened — pick the channel and allow access.", { kind: "info" }); refreshState(); }));
  patchers.push(() => {
    ySecret.placeholder = conn().youtube?.has_client_secret ? "•••• saved (leave blank to keep)" : "Client secret";
    const chans = (S().accounts || []).filter((a) => a.conn === "direct" && a.platform === "youtube" && a.status === "verified");
    const cs = connLine(S().connect?.youtube) || (chans.length ? ["ok", `Signed in: ${chans.map((a) => a.name || a.label).join(", ")}. Sign in again to add another channel.`] : (conn().youtube?.signed_in ? ["ok", "Signed in."] : null));
    yState.className = `pc-conn ${cs ? cs[0] : ""}`; yState.textContent = cs ? cs[1] : "";
  });

  // TikTok
  const tMode = { inbox: h("input", { type: "radio", name: "tmode", value: "inbox" }), direct: h("input", { type: "radio", name: "tmode", value: "direct" }) };
  (tMode[conn().tiktok?.mode || settings().tiktok_mode] || tMode.inbox).checked = true;
  const tKey = text("Client key", conn().tiktok?.client_key || ""), tSecret = secret("Client secret");
  const tState = h("div", { class: "pc-conn" });
  const tSave = h("button", { class: "btn", type: "button" }, "Save app");
  tSave.addEventListener("click", act(tSave, async () => {
    const b = { client_key: tKey.value.trim(), mode: tMode.direct.checked ? "direct" : "inbox" }; if (tSecret.value.trim()) b.client_secret = tSecret.value.trim();
    await apiPost("/api/connect/tiktok/client", b); tSecret.value = ""; toast("TikTok app saved.", { kind: "success" }); await refreshState();
  }));
  const tSign = h("button", { class: "btn primary", type: "button" }, "Sign in with TikTok");
  tSign.addEventListener("click", act(tSign, async () => { await apiPost("/api/connect/tiktok/start", {}); toast("Your browser opened — sign in to TikTok.", { kind: "info" }); refreshState(); }));
  patchers.push(() => {
    tSecret.placeholder = conn().tiktok?.has_client_secret ? "•••• saved (leave blank to keep)" : "Client secret";
    const cs = connLine(S().connect?.tiktok) || (conn().tiktok?.signed_in ? ["ok", "Signed in."] : null);
    tState.className = `pc-conn ${cs ? cs[0] : ""}`; tState.textContent = cs ? cs[1] : "";
  });

  root.append(h("section", { class: "card" }, h("h2", null, "Set up a connection"), h("p", { class: "muted" }, "Secrets go straight into your Mac's Keychain. They're never shown again."),
    panel("Upload-Post", "easiest · one key for everything, best for TikTok", [
      ["Sign up at ", ext("https://upload-post.com", "upload-post.com"), " and create a profile (for example whyaimatters)."],
      "Connect TikTok (and any others you like) to that profile. Add one profile per brand under Brands & accounts above.",
      "Dashboard → API Keys → create a key.",
      "Paste the key and the profile name below, then Save & test.",
      "Back in Platforms above, switch the platforms you want to the Upload-Post route.",
    ], h("div", { class: "form-grid" }, field("API key", upKey), field("Profile name", upUser), h("div", { class: "form-actions" }, upSave))),
    panel("Meta — Instagram + Facebook", "free · one app covers both", [
      "Make sure Instagram is a Professional account (Creator or Business) linked to your Facebook Page.",
      ["At ", ext("https://developers.facebook.com", "developers.facebook.com"), " → My Apps → Create App → type Business."],
      "Add the products Facebook Login for Business and Instagram (API setup with Facebook login).",
      "App settings → Basic: copy the App ID and App Secret into the boxes below and save.",
      ["Open ", ext("https://developers.facebook.com/tools/explorer/", "Graph API Explorer"), ", pick your app, tick the listed page + Instagram permissions, generate a user token and paste it below."],
      "Pick your Page from the list. Done — it swaps in a long-lived Page token for you.",
    ], h("div", { class: "form-grid" }, field("App ID", mApp), field("App secret", mSecret), h("div", { class: "form-actions" }, mApp1),
      field("User token from Graph API Explorer", mTok), h("div", { class: "form-actions" }, mFind), pagesBox, metaState))),
  );
  root.lastChild.append(
    panel("YouTube", "free · uploads stay Private until Google audits your project", [
      ["Go to ", ext("https://console.cloud.google.com", "console.cloud.google.com"), " → New project (for example Social Command Center)."],
      "APIs & Services → Library → enable YouTube Data API v3.",
      "OAuth consent screen → External → add yourself as a Test user, then set it to In production so sign-in doesn't expire after 7 days.",
      "Credentials → Create → OAuth client ID → Desktop app. Copy the Client ID and secret below.",
      "Save, then Sign in with Google and pick a channel. Repeat for each channel: every one is saved separately, so a second sign-in never replaces the first.",
    ], h("div", { class: "form-grid" }, field("Client ID", yId), field("Client secret", ySecret), h("div", { class: "form-actions" }, ySave, ySign), yState)),
    panel("TikTok direct", "free inbox mode · you tap Post in the app", [
      ["Go to ", ext("https://developers.tiktok.com", "developers.tiktok.com"), " → Manage apps → Create. Add Login Kit and Content Posting API, and request the video.upload scope."],
      ["Add the redirect URI ", h("code", null, "http://localhost:8790/callback/"), " (desktop)."],
      "Copy the Client key and secret below and save.",
      "Sign in with TikTok. Then switch TikTok to the Direct route above.",
    ], h("div", { class: "form-grid" },
      h("div", { class: "callout info" }, h("strong", null, "TikTok's rule: "), "apps that aren't audited can only post privately, and TikTok doesn't accept personal upload tools for audit. Inbox mode sends each video to your TikTok app and you tap Post. For fully hands-off public TikTok posting, use Upload-Post instead."),
      h("div", { class: "field" }, h("label", null, "Mode"),
        h("label", { class: "check radio" }, tMode.inbox, h("span", null, h("strong", null, "Inbox"), h("span", { class: "faint" }, " — free, you tap Post in the app"))),
        h("label", { class: "check radio" }, tMode.direct, h("span", null, h("strong", null, "Direct"), h("span", { class: "faint" }, " — needs TikTok's audit")))),
      field("Client key", tKey), field("Client secret", tSecret), h("div", { class: "form-actions" }, tSave, tSign), tState)));

  // ───────── posting schedule ─────────
  const cur = settings();
  const sc = { slots: [...(cur.schedule?.slots || [])].sort(), days: [...(cur.schedule?.days || [])], tz: cur.timezone || tz() };
  const chips = h("div", { class: "time-chips" }), dayBtns = h("div", { class: "day-toggles" });
  const timeIn = h("input", { class: "input", type: "time", value: "12:00", "aria-label": "New time" });
  const zoneSel = h("select", { class: "input", "aria-label": "Timezone" });
  const zones = [...new Set([sc.tz, Intl.DateTimeFormat().resolvedOptions().timeZone, ...ZONES])];
  zoneSel.append(...zones.map((z) => h("option", { value: z, selected: z === sc.tz }, z.replace(/_/g, " "))));
  zoneSel.addEventListener("change", () => { sc.tz = zoneSel.value; });
  const paintSched = () => {
    chips.replaceChildren(...(sc.slots.length ? sc.slots.map((t) => h("span", { class: "tchip" }, fmt12(t), h("button", { type: "button", "aria-label": `Remove ${fmt12(t)}`, onclick: () => { sc.slots = sc.slots.filter((x) => x !== t); paintSched(); } }, icon("x", 12)))) : [h("span", { class: "faint" }, "No times yet — add one.")]));
    dayBtns.replaceChildren(...DAYS.map((d) => h("button", { class: `day-toggle${sc.days.includes(d) ? " on" : ""}`, type: "button", "aria-pressed": String(sc.days.includes(d)),
      onclick: () => { sc.days = sc.days.includes(d) ? sc.days.filter((x) => x !== d) : [...sc.days, d]; paintSched(); } }, DAY_NAMES[d])));
  };
  paintSched();
  const addTime = h("button", { class: "btn sm", type: "button", onclick: () => { if (timeIn.value && !sc.slots.includes(timeIn.value)) { sc.slots = [...sc.slots, timeIn.value].sort(); paintSched(); } } }, icon("plus", 14), "Add time");
  const saveSched = h("button", { class: "btn primary", type: "button" }, "Save schedule");
  saveSched.addEventListener("click", act(saveSched, async () => {
    if (!sc.slots.length || !sc.days.length) return toast("Pick at least one time and one day.", { kind: "info" });
    await apiPost("/api/settings", { timezone: sc.tz, schedule: { slots: sc.slots, days: DAYS.filter((d) => sc.days.includes(d)) } });
    toast("Posting schedule saved.", { kind: "success" }); await refreshState();
  }));
  const schedCard = h("section", { class: "card", id: "schedule" }, h("h2", null, "Posting schedule"),
    h("p", { class: "muted" }, "Auto-schedule fills these times, in order, on these days."),
    h("div", { class: "field" }, h("label", null, "Times each day"), chips, h("div", { class: "inline-add" }, timeIn, addTime)),
    h("div", { class: "field" }, h("label", null, "Days"), dayBtns),
    field("Timezone", zoneSel, "Every time in the dashboard is shown in this zone."),
    h("div", { class: "form-actions" }, saveSched));
  root.append(schedCard);

  // ───────── scheduler ─────────
  const schedState = h("div", { class: "pc-conn" });
  const inst = h("button", { class: "btn", type: "button" }, "Install background scheduler");
  inst.addEventListener("click", act(inst, async () => { const r = await apiPost("/api/scheduler/install", {}); toast(r.output ? "Background scheduler installed." : "Installed.", { kind: "success", details: r.output ? [String(r.output).slice(0, 300)] : [] }); await refreshState(); }));
  const uninst = h("button", { class: "btn ghost danger", type: "button" }, "Uninstall");
  uninst.addEventListener("click", act(uninst, async () => { await apiPost("/api/scheduler/uninstall", {}); toast("Background scheduler removed.", { kind: "success" }); await refreshState(); }));
  patchers.push(() => {
    const s = S().scheduler || {};
    schedState.className = `pc-conn ${s.launchd_installed ? "ok" : ""}`;
    schedState.textContent = `${s.in_process ? "Running while this page is open" : "Not running in the dashboard"} · background scheduler ${s.launchd_installed ? "installed" : "not installed"} · last run ${relTime(s.last_run)}${s.last_result ? " (" + s.last_result + ")" : ""}`;
    inst.disabled = !!s.launchd_installed; uninst.disabled = !s.launchd_installed;
  });
  root.append(h("section", { class: "card" }, h("h2", null, "Background scheduler"),
    h("p", { class: "muted" }, "Posts only go out while the scheduler runs. The dashboard runs it while open; install the background scheduler so posts go out even when this page is closed (your Mac must be awake)."),
    schedState, h("div", { class: "form-actions" }, inst, uninst)));

  // ───────── folder ─────────
  const folder = text("/path/to/Big Short Run Videos", cur.shorts_folder || "");
  const saveFolder = h("button", { class: "btn", type: "button" }, "Save folder");
  saveFolder.addEventListener("click", act(saveFolder, async () => { await apiPost("/api/settings", { shorts_folder: folder.value.trim() }); toast("Shorts folder saved.", { kind: "success" }); await refreshState(); }));
  root.append(h("section", { class: "card" }, h("h2", null, "Shorts folder"), field("Where your finished vertical MP4s live", folder, "“Import new shorts” in the Library reads from here."), h("div", { class: "form-actions" }, saveFolder)));

  function update() { if (S()) patchers.forEach((p) => p()); }
  update();
  if (section === "schedule") setTimeout(() => schedCard.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
  return { el: root, update, destroy() {} };
}
