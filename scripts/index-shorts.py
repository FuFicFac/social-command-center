"""Refresh the local review inventory from finished-media folders without queuing uploads."""

import argparse
import csv
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from scc import config, db, media
from scc.fini import EXTENSIONS, folder

EXCLUDED = {
    "courses",
    "amy auditions",
    "opus 5.5 review splash reference",
    "node_modules",
    "work",
    "cache",
}


def videos(folders):
    for parent in folders:
        if not parent.is_dir():
            raise ValueError(f"Missing inventory folder: {parent}")
        for directory, children, names in os.walk(parent):
            children[:] = [
                d
                for d in children
                if not d.startswith((".", "_")) and d.strip().lower() not in EXCLUDED
            ]
            for name in sorted(names):
                p = Path(directory) / name
                if (
                    not name.startswith((".", "~"))
                    and p.suffix.lower() in EXTENSIONS
                    and not p.is_symlink()
                ):
                    yield p


def inspect(path):
    probe = media.probe(path)
    if probe.vertical and 0 < probe.duration <= 180:
        return (media.sha256(path), str(path), probe.duration, f"{probe.width}x{probe.height}")
    return None


def refresh(folders):
    files = sorted(set(videos(folders)))
    with ThreadPoolExecutor(4) as pool:
        found = [r for r in pool.map(inspect, files) if r]
    con = db.connect()
    try:
        posts = {r["sha256"]: r["id"] for r in con.execute("select sha256,id from posts")}
        con.execute("BEGIN IMMEDIATE")
        # Preserve rows from inventories outside the requested folders.
        for folder in folders:
            prefix = str(folder) + os.sep
            con.execute(
                "DELETE FROM shorts_inventory WHERE substr(path,1,?)=?", (len(prefix), prefix)
            )
        con.executemany(
            "INSERT OR REPLACE INTO shorts_inventory(hash,path,seconds,dimensions) VALUES(?,?,?,?)",
            found,
        )
        con.commit()
    finally:
        con.close()
    destination = config.DATA_DIR / f"Local Shorts inventory - {date.today()}.csv"
    temp = destination.with_suffix(".tmp")
    with temp.open("w", newline="") as handle:
        out = csv.writer(handle)
        out.writerow(["File", "Seconds", "Dimensions", "SHA-256", "Existing Command Center post"])
        out.writerows(
            (path, f"{seconds:.2f}", dims, sha, posts.get(sha, ""))
            for sha, path, seconds, dims in found
        )
    temp.replace(destination)
    counts = Counter(row[0] for row in found)
    print(f"{len(files)} files checked, {len(found)} vertical Shorts, {len(counts)} distinct files")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders", nargs="*", type=Path)
    args = parser.parse_args()
    refresh(
        [p.resolve() for p in args.folders]
        or [Path(config.load().get("general", "shorts_folder")), folder(config.load())]
    )
