"""Küçük, bağımlılıksız yardımcılar."""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any


SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


def parse_iso(value: Any) -> _dt.datetime | None:
    """ISO-8601'i güvenli biçimde parse eder. 'Z' ve nanosaniye desteklenir."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Python 3.10 fromisoformat 9 haneli kesirleri kabul etmez; 6'ya indir.
    m = re.match(r"^(.*\.)(\d{7,})(.*)$", text)
    if m:
        text = m.group(1) + m.group(2)[:6] + m.group(3)
    try:
        dt = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def epoch_iso(seconds: float) -> str:
    return _dt.datetime.fromtimestamp(seconds, _dt.timezone.utc).isoformat(timespec="seconds")


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def safe_name(value: str, kind: str = "isim") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{kind} boş olamaz.")
    value = value.strip()
    if len(value) > 64:
        raise ValueError(f"{kind} çok uzun (max 64).")
    if value in (".", ".."):
        raise ValueError(f"{kind} '.' veya '..' olamaz.")
    if not SAFE_NAME_RE.match(value):
        raise ValueError(f"{kind} sadece harf, rakam, '.', '_' ve '-' içerebilir.")
    return value


def human_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError):
        return "—"
    if s < 60:
        return f"{s} sn"
    minutes = s // 60
    if minutes < 60:
        return f"{minutes} dk"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} sa {minutes} dk" if minutes else f"{hours} sa"
    days, hours = divmod(hours, 24)
    return f"{days} gün {hours} sa" if hours else f"{days} gün"


def human_percent(value: float | None) -> str:
    if value is None:
        return "—"
    value = clamp(float(value), 0.0, 100.0)
    if value == 100:
        return "100%"
    if value == 0:
        return "0%"
    precise = f"{value:.4f}"
    if float(precise) == 100:
        return ">99.9999%"
    if float(precise) == 0:
        return "<0.0001%"
    return f"{precise.rstrip('0').rstrip('.')}%"


def human_count(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}".replace(",", ".")


def redact(text: Any, limit: int = 400) -> str:
    """Hata mesajlarında token benzeri uzun dizileri maskele."""
    s = str(text)
    s = re.sub(r"(ya29\.[A-Za-z0-9_\-./]+)", "ya29.<redacted>", s)
    s = re.sub(r"(1//[A-Za-z0-9_\-./]{10,})", "1//<redacted>", s)
    s = re.sub(r"(GOCSPX-[A-Za-z0-9_\-]+)", "GOCSPX-<redacted>", s)
    s = re.sub(r"(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)", "<jwt-redacted>", s)
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path: Path, data: Any, mode: int = 0o600) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    atomic_write_text(path, text, mode=mode)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def file_sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def make_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def b64url_json(segment: str) -> dict | None:
    try:
        padded = segment + "=" * (-len(segment) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def free_port(host: str = "127.0.0.1") -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
