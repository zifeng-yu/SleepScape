import io
import wave
from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient
from test_readiness_service import NOW
from test_readiness_service import data as source_data

from sleepscape.app import create_app
from sleepscape.domain.environment import EnvironmentBatch, environment_ref
from sleepscape.services.event_audio import playback, reconcile
from sleepscape.storage import environment
from sleepscape.storage import event_audio as clips
from sleepscape.web.device_security import AUDIO_PATH

data = source_data
BOOT = str(UUID(int=17))
START = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
TOKEN = "t" * 48


def body(slot, length):
    return f"{BOOT} {slot} {START * 1000 + slot}\n".encode() + bytes([143]) * length


def test_device_upload_then_garmin_crop_and_playback(data, monkeypatch):
    settings, batch = data
    device = batch["device_id"]
    environment.rotate_token(settings.data_dir, device, TOKEN)
    batch["boot_id"] = BOOT
    batch["records"] = [
        {
            "record_type": "event",
            "seq": 2,
            "measurement_profile_id": "synthetic-profile",
            "mono_start_ms": 118000,
            "mono_end_ms": 120000,
            "quality": [],
            "event_kind": "relative_sound",
            "active_duration_ms": 2000,
            "max_1s_rms_dbfs": -30.0,
            "baseline_dbfs": -59.0,
            "threshold_profile_id": "relative-sound-60s-10db-6db",
            "merged": False,
            "incomplete": False,
        }
    ]
    environment.ingest(settings.data_dir, device, EnvironmentBatch.model_validate(batch))
    settings.environment_ingest_enabled = True
    monkeypatch.setattr(clips.time, "time", lambda: NOW.timestamp())
    ref = environment_ref("record", device, BOOT, "event", "2")
    payload = body(118000, 16000)
    with TestClient(
        create_app(settings), base_url="https://localhost", client=("127.0.0.1", 1)
    ) as client:
        headers = {"Content-Type": "application/octet-stream"}
        assert client.post(AUDIO_PATH, content=payload, headers=headers).status_code == 401
        headers["Authorization"] = f"Bearer {TOKEN}"
        for _ in range(2):
            assert client.post(AUDIO_PATH, content=payload, headers=headers).status_code == 200
        url = "/api/v1/event-audio/" + ref
        assert client.get(url).status_code == 404
        # Synthetic sleep ends partway through this event.
        import sqlite3

        with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as db:
            db.execute(
                """UPDATE normalized_records SET data_json=json_set(data_json,'$.end_at',?)
                       WHERE kind='sleep'""",
                ("2026-01-01T00:01:59.500Z",),
            )
        reconcile(settings, NOW)
        reconcile(settings, NOW)
        response = client.get(url)
        assert response.status_code == 200
        with wave.open(io.BytesIO(response.content)) as audio:
            assert audio.getframerate() == 8000 and audio.getnframes() == 10000
        with clips.database(settings.data_dir) as db:
            assert db.execute("SELECT length(samples) FROM clips").fetchone()[0] == 10000
        with sqlite3.connect(settings.data_dir / "sleepscape.sqlite3") as db:
            db.execute(
                "UPDATE normalized_records SET data_json=json_set(data_json,'$.end_at',?) "
                "WHERE kind='sleep'",
                ("2026-01-01T00:01:00Z",),
            )
        assert playback(settings, ref) is None
        reconcile(settings, NOW)
        with clips.database(settings.data_dir) as db:
            assert db.execute("SELECT count(*) FROM clips").fetchone()[0] == 0


def test_expiry_uses_capture_even_without_garmin_and_deletes_at_deadline(tmp_path, monkeypatch):
    root = tmp_path / "data"
    clips.stage(root, "device", body(0, 8000), START + clips.TTL - 1)
    with clips.database(root) as db:
        assert db.execute("SELECT published FROM clips").fetchone()[0] == 0
    monkeypatch.setattr(clips.time, "time", lambda: START + clips.TTL)
    clips.expire(root)
    with clips.database(root) as db:
        assert db.execute("SELECT count(*) FROM clips").fetchone()[0] == 0


def test_native_firmware_encodes_one_contiguous_clip_per_minute(tmp_path):
    import subprocess
    from pathlib import Path

    firmware = Path(__file__).resolve().parents[1] / "firmware" / "main"
    (tmp_path / "esp_err.h").write_text("typedef int esp_err_t;")
    (tmp_path / "esp_heap_caps.h").write_text(
        "#define MALLOC_CAP_8BIT 0\nunsigned heap_caps_get_free_size(int);"
    )
    (tmp_path / "esp_timer.h").write_text("#include <stdint.h>\nint64_t esp_timer_get_time(void);")
    program = tmp_path / "exercise.c"
    program.write_text("""
#include "sc_clip.h"
#include "sc_network.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
unsigned heap_caps_get_free_size(int cap) { return 1000000; }
int64_t esp_timer_get_time(void) { return 0; }
bool sc_network_ready(void) { return false; }
void sc_network_audio(const uint8_t *p, size_t n) { fwrite(p,1,n,stdout); }
int main(void) {
    const char *boot = "00000000-0000-0000-0000-000000000011";
    assert(sc_clip_encode(0)==255 && sc_clip_encode(32767)==128 && sc_clip_encode(-32768)==0);
    for (unsigned i=0; i<16; ++i) {
        uint8_t *p=sc_clip_second();
        memset(p, sc_clip_encode(1234), SC_CLIP_RATE);
        sc_clip_accept(boot,i*1000,1767225600000LL+i*1000,i!=1,p);
        sc_clip_upload();
    }
    sc_clip_accept(boot,16000,0,false,NULL);
    sc_clip_upload();
}
""")
    binary = tmp_path / "exercise"
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-I",
            str(tmp_path),
            "-I",
            str(firmware),
            str(program),
            str(firmware / "sc_clip.c"),
            "-o",
            str(binary),
        ],
        check=True,
    )
    packet = subprocess.check_output([binary])
    header, samples = packet.split(b"\n", 1)
    assert header == f"{BOOT} 0 {START * 1000}".encode()
    assert len(samples) == 8000
    clips.stage(tmp_path / "data", "device", packet, START + 2)
