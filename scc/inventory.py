"""Browse existing local Shorts. Preparing a catalog item never approves it."""

import json
from pathlib import Path

from . import media
from .fini import signature


def listing(con):
    rows = con.execute("""
        SELECT i.*,p.id AS post_id,p.title,d.status,d.url,d.remote_id,d.route,d.error,
               (SELECT phase FROM fini_items f WHERE f.path=i.path AND f.origin='inventory'
                ORDER BY f.id DESC LIMIT 1) AS phase
        FROM shorts_inventory i LEFT JOIN posts p ON p.sha256=i.hash
        LEFT JOIN deliveries d ON d.post_id=p.id AND d.platform='youtube'
        ORDER BY i.path
    """)
    items = {}
    for row in rows:
        entry = items.setdefault(
            row["hash"],
            {
                "hash": row["hash"],
                "paths": [],
                "seconds": row["seconds"],
                "title": row["title"],
                "post_id": row["post_id"],
                "youtube": row["status"],
                "url": row["url"],
                "source": row["route"],
                "note": row["error"],
                "phase": row["phase"],
            },
        )
        entry["paths"].append(row["path"])
        if row["phase"]:
            entry["phase"] = row["phase"]
    return list(items.values())


def prepare_for_social(con, checksum):
    row = con.execute(
        "SELECT path FROM shorts_inventory WHERE hash=? ORDER BY length(path),path LIMIT 1",
        (checksum,),
    ).fetchone()
    if not row:
        raise ValueError("Unknown inventory item")
    path = Path(row["path"])
    if not path.is_file() or media.sha256(path) != checksum:
        raise ValueError("File moved or changed since inventory")
    if post := con.execute("SELECT id FROM posts WHERE sha256=?", (checksum,)).fetchone():
        return {"post_id": post["id"]}
    con.execute(
        "INSERT OR IGNORE INTO fini_items(path,stamp,observed,origin) VALUES(?,?,0,'inventory')",
        (str(path), json.dumps(signature(path))),
    )
    return {"queued": True}
