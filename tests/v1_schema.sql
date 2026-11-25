-- The database shape before destination accounts (schema version 1).
CREATE TABLE IF NOT EXISTS fini_items (
    id INTEGER PRIMARY KEY, path TEXT NOT NULL, stamp TEXT NOT NULL,
    observed REAL NOT NULL, phase TEXT NOT NULL DEFAULT 'settling', hash TEXT,
    kind TEXT, brand TEXT, package TEXT, post_id TEXT, receipt TEXT,
    origin TEXT NOT NULL DEFAULT 'drop', retry_after REAL NOT NULL DEFAULT 0,
    message TEXT, UNIQUE(path,stamp)
);
CREATE INDEX IF NOT EXISTS fini_hash ON fini_items(hash);
CREATE INDEX IF NOT EXISTS fini_post ON fini_items(post_id);
CREATE TABLE IF NOT EXISTS shorts_inventory (
    hash TEXT NOT NULL, path TEXT NOT NULL UNIQUE, seconds REAL NOT NULL, dimensions TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS inventory_hash ON shorts_inventory(hash);
CREATE TABLE IF NOT EXISTS posts (
    id            TEXT PRIMARY KEY,
    video_path    TEXT NOT NULL,
    sha256        TEXT,
    title         TEXT NOT NULL DEFAULT '',
    caption       TEXT NOT NULL DEFAULT '',
    overrides     TEXT NOT NULL DEFAULT '{}',
    scheduled_at  TEXT,
    approved_at   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS posts_video ON posts(video_path);
CREATE TABLE IF NOT EXISTS deliveries (
    id          TEXT PRIMARY KEY,
    post_id     TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    platform    TEXT NOT NULL,
    route       TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    remote_id   TEXT,
    url         TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    meta        TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT NOT NULL,
    UNIQUE(post_id, platform)
);
CREATE TABLE IF NOT EXISTS upload_receipts (
    sha256 TEXT NOT NULL, platform TEXT NOT NULL, post_id TEXT NOT NULL,
    status TEXT NOT NULL, remote_id TEXT, url TEXT, recorded_at TEXT NOT NULL,
    PRIMARY KEY(sha256, platform)
);
CREATE TRIGGER IF NOT EXISTS record_upload_receipt AFTER UPDATE ON deliveries
WHEN (NEW.status IN ('published','private','inbox','processing') OR
      (NEW.status='skipped' AND json_extract(NEW.meta,'$.previously_uploaded')=1)) AND
     coalesce(NEW.route,'') != 'dryrun' AND coalesce(NEW.remote_id,'') != 'dryrun'
BEGIN
    INSERT INTO upload_receipts(sha256,platform,post_id,status,remote_id,url,recorded_at)
    SELECT sha256,NEW.platform,NEW.post_id,NEW.status,NEW.remote_id,NEW.url,NEW.updated_at
    FROM posts WHERE id=NEW.post_id AND sha256 IS NOT NULL
    ON CONFLICT(sha256,platform) DO UPDATE SET
        status=excluded.status, remote_id=coalesce(excluded.remote_id,upload_receipts.remote_id),
        url=coalesce(excluded.url,upload_receipts.url)
    WHERE upload_receipts.post_id=excluded.post_id;
END;
INSERT OR IGNORE INTO upload_receipts(sha256,platform,post_id,status,remote_id,url,recorded_at)
SELECT p.sha256,d.platform,p.id,d.status,d.remote_id,d.url,d.updated_at FROM deliveries d
JOIN posts p ON p.id=d.post_id
WHERE p.sha256 IS NOT NULL AND d.status IN ('published','private','inbox','processing')
AND coalesce(d.route,'') != 'dryrun' AND coalesce(d.remote_id,'') != 'dryrun';
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    post_id    TEXT,
    platform   TEXT,
    level      TEXT NOT NULL,
    message    TEXT NOT NULL
);
ALTER TABLE posts ADD COLUMN duration REAL;
