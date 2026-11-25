"""Brands and the accounts that receive posts.

An account is one platform identity reached through one connection: an Upload-Post profile
(one account per platform per profile) or a direct YouTube sign-in. Its `platform_id` is the
platform's own stable ID, which survives renames and reconnects, so it — not the profile
name or the handle — is what a delivery and an upload receipt are tied to.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any

from . import config, db

HANDLE_URL = {
    "tiktok": re.compile(r"tiktok\.com/@([^/?#]+)", re.IGNORECASE),
    "x": re.compile(r"(?:x|twitter)\.com/([^/?#]+)/status", re.IGNORECASE),
    "youtube": re.compile(r"youtube\.com/(@[^/?#]+|channel/[^/?#]+)", re.IGNORECASE),
    "instagram": re.compile(r"instagram\.com/([^/?#]+)/(?:reel|p)/", re.IGNORECASE),
}


FALLBACK = ("handle:", "name:")  # identities used only when no platform ID is available


def _norm(handle: str | None) -> str:
    return (handle or "").strip().lstrip("@").lower()


# ------------------------------------------------------------------ brands --
def brands(con: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute("SELECT id,name FROM brands ORDER BY created_at, name")]


def save_brand(con: sqlite3.Connection, name: str, brand_id: str | None = None) -> str:
    name = " ".join(name.split())
    if not name or len(name) > 60:
        raise ValueError("Give the brand a name of up to 60 characters.")
    if brand_id:
        if not con.execute("UPDATE brands SET name=? WHERE id=?", (name, brand_id)).rowcount:
            raise ValueError("no such brand")
        return brand_id
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "brand"
    brand_id, n = base, 2
    while con.execute("SELECT 1 FROM brands WHERE id=?", (brand_id,)).fetchone():
        brand_id, n = f"{base}-{n}", n + 1
    con.execute("INSERT INTO brands(id,name,created_at) VALUES (?,?,?)", (brand_id, name, db.now()))
    return brand_id


# ---------------------------------------------------------------- accounts --
def list_accounts(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT a.*, b.name AS brand_name FROM accounts a LEFT JOIN brands b ON b.id=a.brand_id "
        "ORDER BY a.conn DESC, a.profile, a.platform, a.created_at")
    return [{**dict(r), "label": db.account_label(r)} for r in rows]


def get(con: sqlite3.Connection, account_id: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    return {**dict(row), "label": db.account_label(row)} if row else None


def upsert(con: sqlite3.Connection, *, conn: str, profile: str, platform: str, platform_id: str,
           handle: str | None, name: str | None, status: str = "verified",
           detail: str | None = None) -> str:
    """Record a verified account. The same (connection, platform, platform ID) is one row
    forever; a different platform ID on the same profile is a different account."""
    row = con.execute(
        "SELECT id FROM accounts WHERE conn=? AND profile=? AND platform=? AND platform_id=?",
        (conn, profile, platform, platform_id)).fetchone()
    if row is None and not platform_id.startswith(FALLBACK):
        row = _upgrade_fallback(con, conn, profile, platform, platform_id, handle, name)
    if row:
        con.execute(
            "UPDATE accounts SET handle=coalesce(?,handle), name=coalesce(?,name), status=?, "
            "detail=?, verified_at=? WHERE id=?",
            (handle, name, status, detail, db.now(), row["id"]))
        return row["id"]
    account_id = uuid.uuid4().hex[:10]
    con.execute(
        "INSERT INTO accounts(id,platform,platform_id,handle,name,conn,profile,status,detail,"
        "verified_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, platform, platform_id, handle, name, conn, profile, status, detail,
         db.now(), db.now()))
    return account_id


def _upgrade_fallback(con: sqlite3.Connection, conn: str, profile: str, platform: str,
                      platform_id: str, handle: str | None, name: str | None):
    """An account first recorded by name (no ID was available) and now seen with its real
    platform ID is the same account: give the row, its deliveries and its receipts the ID."""
    names = {f"handle:{_norm(handle)}", f"name:{(name or '').strip().lower()}"}
    row = con.execute(
        "SELECT id, platform_id FROM accounts WHERE conn=? AND profile=? AND platform=? AND "
        f"platform_id IN ({','.join('?' * len(names))})", (conn, profile, platform, *names)).fetchone()
    if row is None:
        return None
    con.execute("UPDATE accounts SET platform_id=? WHERE id=?", (platform_id, row["id"]))
    # Receipts first: the delivery trigger then finds the row under its new ID.
    con.execute("UPDATE upload_receipts SET account_ref=? WHERE platform=? AND account_ref=? "
                "AND account_id=?", (platform_id, platform, row["platform_id"], row["id"]))
    con.execute("UPDATE deliveries SET account_ref=? WHERE account_id=? AND account_ref=?",
                (platform_id, row["id"], row["platform_id"]))
    return row


def read_social(platform: str, info: Any) -> dict[str, Any] | None:
    """Normalise one `social_accounts` value from Upload-Post. Returns None when the platform
    is not connected. Upload-Post's `username` is the platform's own ID for the account."""
    if not isinstance(info, dict) or not info:
        return None
    handle = info.get("handle") or None
    name = info.get("display_name") or None
    pid = info.get("username") or info.get("user_id") or info.get("id")
    detail = None
    if not pid:  # older responses carry no ID for some platforms; a name is the best we have
        pid = f"handle:{_norm(handle)}" if handle else f"name:{(name or '').strip().lower()}"
        detail = "Upload-Post returned no platform ID; identified by name."
        if pid in ("handle:", "name:"):
            return None
    return {"platform_id": str(pid), "handle": handle, "name": name,
            "reauth": bool(info.get("reauth_required")), "detail": detail}


def sync_profile(con: sqlite3.Connection, profile: str, display_name: str | None,
                 social: dict[str, Any]) -> list[str]:
    """Store what one Upload-Post profile is connected to right now. Accounts that are no
    longer on the profile are kept (deliveries point at them) but marked missing."""
    con.execute(
        "INSERT INTO profiles(name,display_name,present,seen_at) VALUES (?,?,1,?) "
        "ON CONFLICT(name) DO UPDATE SET display_name=excluded.display_name, present=1, "
        "seen_at=excluded.seen_at", (profile, display_name, db.now()))
    seen: list[str] = []
    for platform in config.PLATFORMS:
        acct = read_social(platform, (social or {}).get(platform))
        if acct is None:
            continue
        seen.append(upsert(
            con, conn="upload_post", profile=profile, platform=platform,
            platform_id=acct["platform_id"], handle=acct["handle"], name=acct["name"],
            status="expired" if acct["reauth"] else "verified",
            detail="Sign-in expired — reconnect it in Upload-Post." if acct["reauth"]
            else acct["detail"]))
    marks = ",".join("?" * len(seen))
    con.execute(
        f"UPDATE accounts SET status='missing', detail=?, is_default=0 WHERE conn='upload_post' "
        f"AND profile=? AND id NOT IN ({marks})",
        (f"No longer connected to Upload-Post profile '{profile}'.", profile, *seen))
    return seen


def sync_profiles(con: sqlite3.Connection, found: list[dict[str, Any]]) -> None:
    names = []
    for p in found:
        name = str(p.get("username") or "")
        if not name:
            continue
        names.append(name)
        sync_profile(con, name, p.get("display_name"), p.get("social_accounts") or {})
    marks = ",".join("?" * len(names))
    con.execute(f"UPDATE profiles SET present=0 WHERE name NOT IN ({marks})", names)
    con.execute(
        f"UPDATE accounts SET status='missing', is_default=0, detail='Its Upload-Post profile "
        f"no longer exists.' WHERE conn='upload_post' AND profile NOT IN ({marks})", names)
    prove_receipts(con)


def map_account(con: sqlite3.Connection, account_id: str, brand_id: str | None,
                default: bool | None = None) -> None:
    """Assign an account to a brand. The brand's first account on a platform becomes the one
    it suggests; `default=True` makes this account the suggestion instead."""
    acct = get(con, account_id)
    if not acct:
        raise ValueError("no such account")
    if brand_id and not con.execute("SELECT 1 FROM brands WHERE id=?", (brand_id,)).fetchone():
        raise ValueError("no such brand")
    if acct["status"] != "verified" and brand_id:
        raise ValueError("Reconnect this account before assigning it to a brand.")
    con.execute("UPDATE accounts SET brand_id=?, is_default=0 WHERE id=?", (brand_id, account_id))
    if acct["brand_id"] and acct["brand_id"] != brand_id and acct["is_default"]:
        # The brand it left still needs a suggested account for that platform.
        con.execute(
            "UPDATE accounts SET is_default=1 WHERE id=(SELECT id FROM accounts WHERE brand_id=? "
            "AND platform=? AND status='verified' ORDER BY created_at LIMIT 1)",
            (acct["brand_id"], acct["platform"]))
    if not brand_id:
        return
    has_default = con.execute(
        "SELECT 1 FROM accounts WHERE brand_id=? AND platform=? AND is_default=1 AND "
        "status='verified'", (brand_id, acct["platform"])).fetchone()
    if default or not has_default:
        con.execute("UPDATE accounts SET is_default=0 WHERE brand_id=? AND platform=?",
                    (brand_id, acct["platform"]))
        con.execute("UPDATE accounts SET is_default=1 WHERE id=?", (account_id,))
    # Only deliveries with no account yet take the suggestion; chosen ones never move.
    db.fill_brand_defaults(con, brand_id)


# ------------------------------------------------------ delivery ⇄ account --
def describe(d: dict[str, Any]) -> str:
    """'TikTok @handle' — how a delivery is named in logs and messages."""
    label = db.account_label(d)
    return f"{d['platform']} {label}".strip()


def problem(d: dict[str, Any], acct: dict[str, Any] | None) -> str | None:
    """Why a delivery's selected account can't be used, from what is stored locally."""
    who = describe(d)
    if acct is None:
        return f"The account selected for this post ({who}) is no longer connected."
    if acct["platform_id"] != d["account_ref"]:
        return (f"This connection now points at {acct['label'] or 'another account'}, not the "
                f"approved account ({who}).")
    if acct["status"] == "missing":
        return f"{who} is no longer connected. {acct['detail'] or ''}".strip()
    if acct["status"] == "expired":
        return f"{who} needs to be reconnected — its sign-in expired."
    return None


def live_problem(d: dict[str, Any], current: dict[str, Any] | None, where: str) -> str | None:
    """Compare the approved account with what the connection holds right now."""
    who = describe(d)
    if current is None:
        return f"{where} has no {d['platform']} account connected now; this post was approved for {who}."
    if str(current["platform_id"]) != d["account_ref"]:
        now_label = db.account_label({"account_handle": current.get("handle"),
                                      "account_name": current.get("name")}) or "another account"
        return (f"{where} is now connected to {now_label}, not {who}. "
                "Reselect the destination or reconnect the original account.")
    if current.get("reauth"):
        return f"{who} needs to be reconnected — its sign-in expired."
    return None


# ---------------------------------------------------------------- receipts --
def review_receipts(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Uploads whose receiving account has not been confirmed."""
    rows = con.execute(
        "SELECT r.sha256, r.platform, r.post_id, r.status, r.url, r.remote_id, r.recorded_at, "
        "p.title, p.brand FROM upload_receipts r LEFT JOIN posts p ON p.id=r.post_id "
        "WHERE r.account_ref='' AND r.review=1 ORDER BY r.recorded_at DESC")
    return [dict(r) for r in rows]


def _attach(con: sqlite3.Connection, receipt: sqlite3.Row, acct: dict[str, Any],
            evidence: str) -> bool:
    """Tie an unconfirmed receipt (and the delivery it came from) to an account."""
    sha, platform = receipt["sha256"], receipt["platform"]
    if con.execute("SELECT 1 FROM upload_receipts WHERE sha256=? AND platform=? AND account_ref=?",
                   (sha, platform, acct["platform_id"])).fetchone():
        return False  # that account already has its own receipt for this file
    con.execute(
        "UPDATE upload_receipts SET account_ref=?, account_id=?, account_handle=?, review=0, "
        "evidence=? WHERE sha256=? AND platform=? AND account_ref=''",
        (acct["platform_id"], acct["id"], acct["label"] or None, evidence, sha, platform))
    taken = con.execute("SELECT 1 FROM deliveries WHERE post_id=? AND platform=? AND account_id=?",
                        (receipt["post_id"], platform, acct["id"])).fetchone()
    if not taken:
        con.execute(
            "UPDATE deliveries SET account_id=?, account_ref=?, account_handle=?, account_name=?, "
            "brand_id=?, profile=? WHERE post_id=? AND platform=? AND account_id=''",
            (acct["id"], acct["platform_id"], acct["handle"], acct["name"], acct["brand_id"],
             acct["profile"], receipt["post_id"], platform))
    return True


def prove_receipts(con: sqlite3.Connection) -> dict[str, int]:
    """Attach unconfirmed receipts to accounts when stored evidence settles the destination.

    Evidence accepted, strongest first:
      1. the permalink names the account's handle (TikTok, X, YouTube);
      2. the delivery recorded the Upload-Post profile it was sent through, and that profile
         holds exactly one account for the platform, with no handle that contradicts the link.
    Anything else stays flagged for review.
    """
    accts = list_accounts(con)
    proven = 0
    rows = con.execute(
        "SELECT r.*, d.meta AS dmeta, d.route AS droute FROM upload_receipts r "
        "LEFT JOIN deliveries d ON d.post_id=r.post_id AND d.platform=r.platform "
        "AND d.account_id='' WHERE r.account_ref=''").fetchall()
    for r in rows:
        mine = [a for a in accts if a["platform"] == r["platform"]]
        m = HANDLE_URL.get(r["platform"])
        named = m.search(r["url"] or "") if m else None
        link = _norm(named.group(1).removeprefix("channel/")) if named else ""
        if link in ("reel", "p", "shorts"):
            link = ""
        hits = {a["platform_id"]: a for a in mine
                if link and link in (_norm(a["handle"]), a["platform_id"].lower())}
        if len(hits) == 1:
            acct = next(iter(hits.values()))
            if _attach(con, r, acct, f"The permalink names {acct['label'] or link}."):
                proven += 1
            continue
        meta = json.loads(r["dmeta"] or "{}")
        profile = meta.get("profile")
        if r["droute"] == "upload_post" and profile:
            on_profile = {a["platform_id"]: a for a in mine
                          if a["conn"] == "upload_post" and a["profile"] == profile
                          and a["status"] != "missing"}
            if len(on_profile) == 1:
                acct = next(iter(on_profile.values()))
                if link and _norm(acct["handle"]) and _norm(acct["handle"]) != link:
                    continue  # the link names someone else; leave it for a person
                if _attach(con, r, acct,
                           f"Sent through Upload-Post profile '{profile}' (recorded on the "
                           f"delivery); that profile's {r['platform']} account is "
                           f"{acct['label'] or acct['platform_id']}."):
                    proven += 1
    left = con.execute(
        "SELECT count(*) FROM upload_receipts WHERE account_ref='' AND review=1").fetchone()[0]
    return {"proven": proven, "review": left}


def assign_receipt(con: sqlite3.Connection, sha256: str, platform: str, account_id: str) -> None:
    """You confirmed which account an earlier upload went to."""
    acct = get(con, account_id)
    receipt = con.execute(
        "SELECT * FROM upload_receipts WHERE sha256=? AND platform=? AND account_ref=''",
        (sha256, platform)).fetchone()
    if receipt is None:
        raise ValueError("That upload is not waiting for an account.")
    if acct is None or acct["platform"] != platform:
        raise ValueError(f"Choose a {platform} account.")
    if not _attach(con, receipt, acct, f"Confirmed in the dashboard on {db.now()[:10]}."):
        raise ValueError(f"{acct['label'] or 'That account'} already has a receipt for this file.")
    db.log(con, "info", "upload history: destination confirmed", receipt["post_id"], platform,
           acct["label"])
