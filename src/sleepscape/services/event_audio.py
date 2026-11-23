"""Resolve candidate audio against its original event and the finished Garmin sleep window."""

import io
import math
import struct
import time
import wave
from datetime import date, datetime

from sleepscape.domain.environment_time import utc_to_us
from sleepscape.services.views import DashboardService
from sleepscape.storage import event_audio as clips
from sleepscape.storage.reader import DashboardReader


def _window(context, day, now):
    sleep = context.sleep(date.fromisoformat(day))
    if not sleep or context.stale(sleep["resource"], sleep["date"]):
        return None
    data = sleep["data"]
    if data.get("in_progress"):
        return None
    try:
        start, end = (utc_to_us(data[key]) for key in ("start_at", "end_at"))
    except (KeyError, ValueError, TypeError):
        return None
    return (start, end) if start < end <= now * 1000000 and end - start <= 86400_000000 else None


def _bounds(db, clip, window):
    if window is None:
        return None
    capture = clip["capture"] * 1000
    if capture + clips.MAX_SAMPLES * 125 + 2_000_000 < window[0] or capture - 2_000_000 > window[1]:
        return (0, 0)
    row = db.execute(
        """SELECT t.utc_start_us, r.mono_start_ms, r.mono_end_ms, t.time_uncertainty_ms
           FROM environment_time_index t JOIN environment_records r
             USING(device_id,boot_id,record_type,seq)
           JOIN environment_index_boots b USING(device_id,boot_id)
           WHERE t.device_id=? AND t.boot_id=? AND t.record_type='event' AND t.seq=?
             AND b.source_revision=b.indexed_revision
             AND b.anchor_revision=b.indexed_anchor_revision
             AND t.mapping_version=b.mapping_version AND t.time_quality IN ('synced','estimated')
             AND t.utc_start_us IS NOT NULL""",
        (clip["device"], clip["boot"], clip["seq"]),
    ).fetchone()
    if row is None:
        return None
    mono = clip["mono"]
    utc = row[0] + (mono - row[1]) * 1000
    # Device capture clock and independently indexed NTP anchor must agree.
    error = abs(utc - clip["capture"] * 1000)
    if error > 2_000_000:
        return (0, 0)
    margin = max(error, math.ceil((row[3] or 0) * 1000), 250_000)
    start = max((row[1] - mono) * 1000, window[0] + margin - utc)
    end = min((row[2] - mono) * 1000, window[1] - margin - utc)
    return max(clip["offset"], -(-start // 125)), min(clip["offset"] + clip["size"], end // 125)


_FIELDS = "ref,device,boot,seq,mono,capture,day,offset,published,length(samples) AS size"


def reconcile(settings, now=None):
    root = settings.data_dir
    if not clips.path_for(root).is_file():
        return
    clock = now.timestamp() if isinstance(now, datetime) else time.time()
    with clips.database(root, write=True) as db:
        clips.purge(db, clock)
        with DashboardReader(root) as reader:
            rows = db.execute(f"SELECT {_FIELDS} FROM clips ORDER BY day").fetchall()
            if not rows or not reader.present:
                return
            first, last = (date.fromisoformat(row["day"]) for row in (rows[0], rows[-1]))
            context = DashboardService(settings).readiness_context(reader, first, last)
            windows = {day: _window(context, day, clock) for day in {row["day"] for row in rows}}
            for row in rows:
                bounds = _bounds(reader.connection, row, windows[row["day"]])
                if bounds is None and not row["published"]:
                    continue
                if bounds is None or bounds[0] >= bounds[1]:
                    db.execute("DELETE FROM clips WHERE ref=?", (row["ref"],))
                    continue
                start, end = bounds
                if not row["published"] or start != row["offset"] or end - start != row["size"]:
                    db.execute(
                        "UPDATE clips SET samples=substr(samples,?,?),offset=?,published=1 "
                        "WHERE ref=?",
                        (start - row["offset"] + 1, end - start, start, row["ref"]),
                    )


def _linear(value):
    value ^= 255
    magnitude = (((value & 15) << 3) + 132) << ((value >> 4) & 7)
    return 132 - magnitude if value & 128 else magnitude - 132


_PCM = tuple(struct.pack("<h", _linear(i)) for i in range(256))


def playback(settings, ref):
    root = settings.data_dir
    if not clips.path_for(root).is_file():
        return None
    now = time.time()
    with clips.database(root) as db, DashboardReader(root) as reader:
        row = db.execute(
            f"SELECT {_FIELDS},samples FROM clips WHERE ref=? AND published=1 AND capture>?",
            (ref, (now - clips.RETENTION) * 1000),
        ).fetchone()
        if not row or not reader.present:
            return None
        day = date.fromisoformat(row["day"])
        if settings.dashboard_start_date and day < settings.dashboard_start_date:
            return None
        context = DashboardService(settings).readiness_context(reader, day, day)
        bounds = _bounds(reader.connection, row, _window(context, row["day"], now))
        if bounds is None or bounds[0] >= bounds[1]:
            return None
        samples = row["samples"][bounds[0] - row["offset"] : bounds[1] - row["offset"]]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setparams((1, 2, clips.RATE, 0, "NONE", "not compressed"))
        output.writeframes(b"".join(_PCM[i] for i in samples))
    return buffer.getvalue()
