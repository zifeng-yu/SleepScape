"""Service-account-only report transcripts. The public ledger carries their request IDs."""

import json
import os
import re
import stat
import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from sleepscape.models.base import ModelError

_FILE = re.compile(
    r"(\d{4}-\d\d-\d\d)\.(nightly|weekly)\.\d{4}-\d\d-\d\d\.[a-f0-9]{32}"
    r"\.(request|response|validation)\.json"
)


def logged_request(request, directory, run_day, kind, target, request_id):
    """Called under the existing model lock, before the supervised worker starts."""
    root = Path(directory).absolute() / "llm-exchanges"
    name = f"{run_day.isoformat()}.{kind}.{target.isoformat()}.{request_id}"
    if not _FILE.fullmatch(name + ".request.json"):
        raise ModelError("model_operation_failed")
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ModelError("model_operation_failed")
        cutoff = (run_day - timedelta(days=29)).isoformat()
        for file in root.iterdir():
            match = _FILE.fullmatch(file.name) or _FILE.fullmatch(file.name.rsplit(".", 1)[0])
            if match and match[1] < cutoff and stat.S_ISREG(file.lstat().st_mode):
                file.unlink()
    except OSError:
        raise ModelError("model_operation_failed") from None
    return replace(request, exchange_path=root / name)


def write_event(request, event, data):
    base = request.exchange_path
    if base is None:
        return
    target = base.with_name(f"{base.name}.{event}.json")
    try:
        with tempfile.NamedTemporaryFile(
            dir=base.parent, prefix=target.name + ".", delete=False
        ) as file:
            temp = Path(file.name)
            try:
                file.write(json.dumps(data, ensure_ascii=True, allow_nan=False).encode())
                file.flush()
                os.fsync(file.fileno())
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
    except OSError:
        raise ModelError("model_operation_failed") from None


def validation_event(request, error=None):
    data = {"state": "rejected" if error else "accepted", "code": error.code if error else None}
    if error:
        data["http_status"] = error.http_status
        # Only our validator's static location, never exceptions or frame locals.
        cause = error.__context__ or error
        tb = cause.__traceback__
        while tb:
            module = tb.tb_frame.f_globals.get("__name__", "")
            if module.startswith(("sleepscape.domain.", "sleepscape.resources")):
                data["rule_location"] = f"{module}:{tb.tb_frame.f_code.co_name}:{tb.tb_lineno}"
            tb = tb.tb_next
    write_event(request, "validation", data)
