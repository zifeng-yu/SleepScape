"""Report persistence uses private synthetic files and preserves prior reports on failure."""

import json
import stat

import pytest

from sleepscape.storage.reports import read_private_json, write_private_json


def test_failed_report_serialization_preserves_existing_report_and_cleans_temporary(tmp_path):
    path = tmp_path / "reports" / "latest.json"
    original = {"status": "synthetic-passed"}
    write_private_json(path, original)
    with pytest.raises(ValueError):
        write_private_json(path, {"invalid_number": float("nan")})
    assert read_private_json(path) == original
    assert set(path.parent.iterdir()) == {path}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_report_reader_rejects_oversized_nonobject_and_symlinked_files(tmp_path):
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps({"synthetic": "x" * 4096}))
    with pytest.raises(ValueError, match="report_size_exceeded"):
        read_private_json(path)
    path.write_text("[]")
    with pytest.raises(ValueError, match="report_requires_object"):
        read_private_json(path)
    path.write_text('{"synthetic": true}')
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_private_json(link)
