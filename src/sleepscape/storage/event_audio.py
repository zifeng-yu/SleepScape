"""Private, expiring G.711 event clips, isolated from observation backups."""

import hashlib
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sleepscape.domain.environment import environment_ref
from sleepscape.storage.usage import require_capacity

RATE = 8000
MAX_SAMPLES = 12 * RATE
TTL = 48 * 3600
RETENTION = 30 * 86400
MAX_BODY = MAX_SAMPLES + 100
HEADER = re.compile(rb"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}) ([0-9]{1,13}) ([0-9]{13})")
ZONE = ZoneInfo("Asia/Shanghai")


def path_for(root):
    return Path(root) / "event-audio" / "clips.sqlite3"


@contextmanager
def database(root, *, create=False, write=False):
    path = path_for(root)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("unsafe_audio_path")
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = "rwc" if create else "rw" if write else "ro"
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=" + mode, uri=True)) as db:
        db.row_factory = sqlite3.Row
        if create and not db.execute("PRAGMA page_count").fetchone()[0]:
            db.execute("PRAGMA auto_vacuum=FULL")
        with db:
            if write or create:
                db.execute("PRAGMA secure_delete=ON")
                db.execute("BEGIN IMMEDIATE")
            if create:
                db.execute("""CREATE TABLE IF NOT EXISTS clips (
                    ref TEXT PRIMARY KEY, device TEXT NOT NULL, boot TEXT NOT NULL,
                    seq INTEGER NOT NULL, mono INTEGER NOT NULL, capture INTEGER NOT NULL,
                    day TEXT NOT NULL, digest TEXT NOT NULL, samples BLOB NOT NULL,
                    offset INTEGER NOT NULL DEFAULT 0, published INTEGER NOT NULL DEFAULT 0)""")
                db.execute("CREATE INDEX IF NOT EXISTS clip_day ON clips(day)")
            yield db


def purge(db, now):
    db.execute(
        "DELETE FROM clips WHERE capture <= ? OR (published=0 AND capture <= ?)",
        ((now - RETENTION) * 1000, (now - TTL) * 1000),
    )


def expire(root):
    if path_for(root).is_file():
        with database(root, write=True) as db:
            purge(db, time.time())


def stage(root, device, body, now):
    header, delimiter, samples = body.partition(b"\n")
    match = HEADER.fullmatch(header)
    if not delimiter or not match or not 0 < len(samples) <= MAX_SAMPLES:
        raise ValueError("invalid_audio")
    boot = match[1].decode()
    mono, capture = int(match[2]), int(match[3])
    if mono % 1000 or mono % 60000 * 8 + len(samples) > 60000 * 8:
        raise ValueError("invalid_audio_interval")
    if not (now - TTL) * 1000 < capture <= now * 1000 + 1000:
        raise ValueError("invalid_audio_time")
    local = datetime.fromtimestamp(capture / 1000, UTC).astimezone(ZONE)
    if 12 <= local.hour < 18:
        raise ValueError("outside_audio_hours")
    day = (local + timedelta(hours=6)).date().isoformat()
    if local.hour < 12:
        noon = local.replace(hour=12, minute=0, second=0, microsecond=0)
        samples = samples[: int((noon.timestamp() * 1000 - capture) * 8)]
    seq = mono // 60000 + 1
    ref = environment_ref("record", device, boot, "event", str(seq))
    digest = hashlib.sha256(body).hexdigest()
    require_capacity(root, "write", len(body) * 2)
    with database(root, create=True) as db:
        purge(db, now)
        previous = db.execute("SELECT digest FROM clips WHERE ref=?", (ref,)).fetchone()
        if previous:
            if previous[0] != digest:
                raise ValueError("audio_conflict")
            return
        if db.execute("SELECT count(*) FROM clips WHERE day=?", (day,)).fetchone()[0] >= 60:
            raise ValueError("audio_night_limit")
        # File pages are reused after secure deletion. Bound the physical database too.
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        db.execute(f"PRAGMA max_page_count={200 * 1024**2 // page_size}")
        db.execute(
            "INSERT INTO clips(ref,device,boot,seq,mono,capture,day,digest,samples) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (ref, device, boot, seq, mono, capture, day, digest, samples),
        )


def available(root, refs, now):
    if not refs or not path_for(root).is_file():
        return set()
    with database(root) as db:
        marks = ",".join("?" for _ in refs)
        return {
            row[0]
            for row in db.execute(
                f"SELECT ref FROM clips WHERE published=1 AND capture>? AND ref IN ({marks})",
                ((now - RETENTION) * 1000, *refs),
            )
        }
