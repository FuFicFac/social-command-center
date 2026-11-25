import shutil

import pytest

from scc import config, db, fini, media


@pytest.fixture
def intake(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    incoming = tmp_path / "Fini Vids"
    incoming.mkdir()
    state = tmp_path / "state"
    con = db.connect(tmp_path / "test.sqlite")
    cfg = config.load(tmp_path / "empty.toml")
    return con, incoming, state, cfg


def packaging(path, kind, state):
    return {
        "brand": "wam",
        "title": "Topic",
        "description": "Explanation.",
        "instagram": "Instagram caption",
        "facebook": "Facebook caption",
        "tiktok": "TikTok caption",
    }


def probe_short(path):
    return media.Probe(49, 1080, 1920, path.stat().st_size, "h264", True)


def test_baseline_copies_settling_and_unique_post(intake, monkeypatch):
    con, folder, _state, _cfg = intake
    old = folder / "old.mp4"
    old.write_bytes(b"existing")
    fini.tick(*intake, now=0)
    old.rename(folder / "renamed.mp4")
    shutil.copyfile(folder / "renamed.mp4", folder / "copy.mp4")
    new = folder / "new.mp4"
    new.write_bytes(b"fresh")
    monkeypatch.setattr(media, "probe", probe_short)
    for t in (1, 2, 92, 93, 94, 95):
        fini.tick(*intake, now=t, make_package=packaging)
    # Renaming the baseline preserves its signature. A new file with different bytes is staged.
    assert (
        con.execute(
            "select count(*) from fini_items where path=?", (str(folder / "renamed.mp4"),)
        ).fetchone()[0]
        == 0
    )
    assert len(db.list_posts(con)) == 1
    newpost = next(p for p in db.list_posts(con) if p["sha256"] == media.sha256(new))
    assert newpost["approved_at"] is None
    assert set(newpost["deliveries"]) == set(config.PLATFORMS)
    assert set(newpost["overrides"]) == {"facebook", "instagram", "tiktok"}
    assert all(
        d["status"] == "pending" and d["attempts"] == 0 for d in newpost["deliveries"].values()
    )
    new.write_bytes(b"fresh revised edit")
    for t in (100, 200, 201):
        fini.tick(*intake, now=t, make_package=packaging)
    assert any(p["sha256"] == media.sha256(new) for p in db.list_posts(con))


def test_copy_with_new_name_matches_existing_baseline(intake):
    con, folder, _state, _cfg = intake
    old = folder / "old.mp4"
    old.write_bytes(b"existing")
    fini.tick(*intake, now=0)
    shutil.copyfile(old, folder / "new name.mp4")
    for t in (1, 92):
        fini.tick(*intake, now=t, make_package=lambda *_: pytest.fail("duplicate was packaged"))
    assert not db.list_posts(con)
    assert con.execute("select phase from fini_items").fetchone()[0] == "duplicate"


def test_archive_selection_has_three_review_destinations(intake, monkeypatch):
    from scc.inventory import prepare_for_social

    con, folder, _state, _cfg = intake
    _cfg.set("fini", "auto_approve_new_shorts", True)
    source = folder / "archive.mp4"
    source.write_bytes(b"old file")
    fini.tick(*intake, now=0)
    checksum = media.sha256(source)
    con.execute(
        "insert into shorts_inventory values (?,?,?,?)", (checksum, str(source), 30, "1080x1920")
    )
    prepare_for_social(con, checksum)
    prepare_for_social(con, checksum)
    monkeypatch.setattr(media, "probe", probe_short)
    for t in (100, 101):
        fini.tick(*intake, now=t, make_package=packaging)
    posts = db.list_posts(con)
    assert len(posts) == 1 and posts[0]["approved_at"] is None
    assert set(posts[0]["deliveries"]) == {"instagram", "facebook", "tiktok"}


def test_main_never_enters_social_dispatch_or_retries_ambiguous_upload(intake, monkeypatch):
    con, folder, _state, _cfg = intake
    fini.tick(*intake, now=0)
    (folder / "main.mp4").write_bytes(b"long")
    monkeypatch.setattr(media, "probe", lambda p: media.Probe(360, 1920, 1080, 4, "h264", True))
    transactions = []

    def fail_after_submission(path, title, desc, brand, checkpoint):
        transactions.append(path)
        checkpoint("submitting", {})
        raise RuntimeError("Interrupted after chooser confirmed")

    for t in (1, 92, 93, 900):
        fini.tick(*intake, now=t, make_package=packaging, stage_main=fail_after_submission)
    assert len(transactions) == 1 and not db.list_posts(con)
    assert con.execute("select phase from fini_items").fetchone()[0] == "studio_review"


def test_youtube_pause_keeps_long_form_local(intake, monkeypatch):
    con, folder, _state, cfg = intake
    cfg.set("youtube", "uploads_paused", True)
    fini.tick(*intake, now=0)
    (folder / "main.mp4").write_bytes(b"episode")
    monkeypatch.setattr(media, "probe", lambda p: media.Probe(420, 1920, 1080, 7, "h264", True))
    for t in (1, 92, 93, 900):
        fini.tick(
            *intake,
            now=t,
            make_package=packaging,
            stage_main=lambda *_: pytest.fail("Paused uploader ran"),
        )
    assert con.execute("SELECT phase FROM fini_items").fetchone()[0] == "youtube_paused"
