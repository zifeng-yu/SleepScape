"""Local-only access by default; optional password hashes for a private deployment."""

import base64
import binascii
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import stat
import time
from collections import OrderedDict
from pathlib import Path
from threading import Lock

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from sleepscape.config import Settings

ITERATIONS = 600_000
AUTH_REALM = 'Basic realm="SleepScape", charset="UTF-8"'
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
}


def loopback(value: str | None) -> bool:
    try:
        return ipaddress.ip_address(value or "").is_loopback
    except ValueError:
        return False


def password_record(username: str, password: str) -> dict:
    if not username or ":" in username or len(username.encode()) > 128:
        raise ValueError("invalid_username")
    if not 12 <= len(password) <= 1024:
        raise ValueError("password_requires_12_to_1024_characters")
    salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return {
        "algorithm": "pbkdf2-sha256",
        "iterations": ITERATIONS,
        "username": username,
        "salt": salt.hex(),
        "digest": digest.hex(),
    }


def save_password(path: Path, username: str, password: str) -> None:
    record = password_record(username, password)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("credential_symlink_not_allowed")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(record, stream)
    os.replace(temporary, path)
    path.chmod(0o600)


def configure_password(settings: Settings) -> None:
    path = settings.web_auth_file or Path(".secrets/dashboard-auth.json")
    username = input("看板用户名（留空使用 sleepscape）：").strip() or "sleepscape"
    password = getpass.getpass("看板密码（至少12个字符，不会显示）：")
    confirmation = getpass.getpass("再次输入密码：")
    if not hmac.compare_digest(password.encode(), confirmation.encode()):
        raise ValueError("password_confirmation_mismatch")
    save_password(path, username, password)
    if settings.web_auth_file is None:
        print("请在 .env 设置 SLEEPSCAPE_WEB_AUTH_FILE=.secrets/dashboard-auth.json 后重启。")


class AccessGate:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cache_key = secrets.token_bytes(32)
        self.cache: OrderedDict[bytes, tuple[float, tuple[int, int]]] = OrderedDict()
        self.cache_lock = Lock()
        if settings.web_auth_file is None and settings.host not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("network_binding_requires_web_auth_file")
        if settings.web_auth_file is not None:
            self._load()

    def _load(self) -> tuple[dict, tuple[int, int]]:
        path = self.settings.web_auth_file
        try:
            if path is None or path.is_symlink() or path.parent.is_symlink():
                raise ValueError
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError
            if stat.S_IMODE(path.parent.stat().st_mode) & 0o077 or info.st_size > 4096:
                raise ValueError
            value = json.loads(path.read_text())
            if (
                value["algorithm"] != "pbkdf2-sha256"
                or value["iterations"] != ITERATIONS
                or not isinstance(value["username"], str)
                or ":" in value["username"]
                or len(bytes.fromhex(value["salt"])) != 32
                or len(bytes.fromhex(value["digest"])) != 32
            ):
                raise ValueError
            return value, (info.st_mtime_ns, info.st_size)
        except (OSError, ValueError, TypeError, KeyError):
            raise ValueError("web_auth_configuration_invalid") from None

    def _verify(self, header: str) -> bool:
        if len(header) > 8192:
            return False
        try:
            scheme, encoded = header.split(" ", 1)
            if scheme.lower() != "basic":
                return False
            username, password = (
                base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
            )
        except (ValueError, UnicodeError, binascii.Error):
            return False
        record, version = self._load()
        cache_id = hmac.digest(self.cache_key, header.encode(), "sha256")
        now = time.monotonic()
        with self.cache_lock:
            cached = self.cache.get(cache_id)
            if cached and cached[0] > now and cached[1] == version:
                self.cache.move_to_end(cache_id)
                return True
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(record["salt"]), ITERATIONS
        )
        valid = hmac.compare_digest(candidate, bytes.fromhex(record["digest"]))
        valid &= hmac.compare_digest(username.encode(), record["username"].encode())
        if valid:
            with self.cache_lock:
                self.cache[cache_id] = (now + 300, version)
                self.cache.move_to_end(cache_id)
                while len(self.cache) > 32:
                    self.cache.popitem(last=False)
        return valid

    async def check(self, request):
        if request.method not in {"GET", "HEAD"}:
            return JSONResponse({"error": "read_only_service"}, status_code=405)
        host = request.url.hostname
        if host not in self.settings.web_allowed_hosts and not (
            host == "::1" and "[::1]" in self.settings.web_allowed_hosts
        ):
            return JSONResponse({"error": "host_not_allowed"}, status_code=400)
        top_level_entry = (
            request.url.path == "/" and request.headers.get("sec-fetch-mode") == "navigate"
        )
        if request.headers.get("sec-fetch-site") == "cross-site" and not top_level_entry:
            return JSONResponse({"error": "cross_site_request_blocked"}, status_code=403)
        if request.url.path == "/health":
            return None
        forwarded = any(
            name in request.headers
            for name in (
                "forwarded",
                "x-forwarded-for",
                "x-forwarded-host",
                "x-forwarded-proto",
            )
        )
        direct_local = loopback(request.client.host if request.client else None) and not forwarded
        if self.settings.web_auth_file is None:
            if not direct_local:
                return JSONResponse({"error": "local_access_only"}, status_code=403)
            return None
        # Trust the ASGI scheme, never an arbitrary forwarded header. Uvicorn must
        # trust only the loopback HTTPS proxy (the deployment template's default).
        if not direct_local and request.url.scheme != "https":
            return JSONResponse({"error": "https_required"}, status_code=403)
        try:
            verified = await run_in_threadpool(
                self._verify, request.headers.get("authorization", "")
            )
        except ValueError:
            return JSONResponse({"error": "web_auth_unavailable"}, status_code=503)
        if not verified:
            return JSONResponse(
                {"error": "authentication_required"},
                status_code=401,
                headers={"WWW-Authenticate": AUTH_REALM},
            )
        return None
