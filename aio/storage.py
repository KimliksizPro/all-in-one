"""Yerel durum, ayarlar ve önbellek yolları.

Token'lar bu dosyada tutulmaz; Secret Service (KDE Wallet / GNOME Keyring)
içinde saklanır. Buradaki state yalnızca profil metadatası, aktif seçim,
ayarlar ve kota önbelleğidir.

Eski `all-in-one` sürümünün `state.json` / `profiles/codex` verileri
korunur; bu sürüm ayrı `state.antigravity.json` dosyasını kullanır.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .util import atomic_write_json, now_iso, safe_name

APP_NAME = "all-in-one"
IDE_KEY = "antigravity"
STATE_VERSION = 1


class StorageError(RuntimeError):
    pass


class UnsafePathError(StorageError):
    pass


def root_dir() -> Path:
    override = os.environ.get("AIO_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".all-in-one").resolve()


def state_path() -> Path:
    return root_dir() / "state.antigravity.json"


def alerts_path() -> Path:
    return root_dir() / "alerts.antigravity.json"


def quota_cache_dir() -> Path:
    return root_dir() / "quota_cache" / IDE_KEY


def quota_cache_path(profile: str) -> Path:
    safe_name(profile, "profil adı")
    return quota_cache_dir() / f"{profile}.json"


def backups_dir() -> Path:
    return root_dir() / "backups"


def profile_dir(profile: str) -> Path:
    safe_name(profile, "profil adı")
    base = (root_dir() / "profiles" / IDE_KEY).resolve()
    target = (base / profile).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise UnsafePathError("Profil yolu veri dizini dışına çıkıyor.") from exc
    return target


def ensure_dirs() -> None:
    for path in (root_dir(), root_dir() / "profiles" / IDE_KEY, quota_cache_dir(), backups_dir()):
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass


def backup_file(path: Path, tag: str, data: bytes | None = None) -> Path | None:
    """Bir dosyayı zaman damgalı yedeğe kopyalar. Dosya yoksa None."""
    if data is None:
        if not path.is_file():
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
    ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backups_dir() / f"{tag}-{stamp}{path.suffix or '.bak'}"
    n = 1
    while target.exists():
        target = backups_dir() / f"{tag}-{stamp}-{n}{path.suffix or '.bak'}"
        n += 1
    target.write_bytes(data)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


@dataclass
class Settings:
    warn_percent: float = 20.0
    critical_percent: float = 5.0
    poll_seconds: int = 300
    desktop_notifications: bool = True
    launch_after_switch: bool = True
    auto_refresh_all: bool = False
    all_poll_seconds: int = 900

    def normalized(self) -> "Settings":
        def num(value: Any, default: float, low: float, high: float) -> float:
            try:
                return max(low, min(high, float(value)))
            except (TypeError, ValueError):
                return default

        self.warn_percent = num(self.warn_percent, 20.0, 1.0, 95.0)
        self.critical_percent = num(self.critical_percent, 5.0, 0.0, 90.0)
        if self.critical_percent > self.warn_percent:
            self.critical_percent = self.warn_percent
        self.poll_seconds = int(num(self.poll_seconds, 300, 30, 86400))
        self.all_poll_seconds = int(num(self.all_poll_seconds, 900, 60, 86400))
        self.desktop_notifications = bool(self.desktop_notifications)
        self.launch_after_switch = bool(self.launch_after_switch)
        self.auto_refresh_all = bool(self.auto_refresh_all)
        return self

    @classmethod
    def from_dict(cls, data: dict | None) -> "Settings":
        data = data if isinstance(data, dict) else {}
        defaults = cls()
        return cls(
            warn_percent=data.get("warn_percent", defaults.warn_percent),
            critical_percent=data.get("critical_percent", defaults.critical_percent),
            poll_seconds=data.get("poll_seconds", defaults.poll_seconds),
            desktop_notifications=data.get("desktop_notifications", defaults.desktop_notifications),
            launch_after_switch=data.get("launch_after_switch", defaults.launch_after_switch),
            auto_refresh_all=data.get("auto_refresh_all", defaults.auto_refresh_all),
            all_poll_seconds=data.get("all_poll_seconds", defaults.all_poll_seconds),
        ).normalized()


@dataclass
class ProfileMeta:
    name: str
    label: str
    email: str
    created_at: str
    last_used: str | None = None
    primary: bool = False
    account_name: str | None = None
    plan_name: str | None = None
    needs_login: bool = False
    note: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "ProfileMeta":
        data = data if isinstance(data, dict) else {}
        return cls(
            name=name,
            label=str(data.get("label") or name),
            email=str(data.get("email") or ""),
            created_at=str(data.get("created_at") or now_iso()),
            last_used=data.get("last_used"),
            primary=bool(data.get("primary")),
            account_name=data.get("account_name"),
            plan_name=data.get("plan_name"),
            needs_login=bool(data.get("needs_login")),
            note=str(data.get("note") or ""),
        )


@dataclass
class State:
    version: int = STATE_VERSION
    active: str | None = None
    primary: str | None = None
    settings: Settings = field(default_factory=Settings)
    profiles: dict[str, ProfileMeta] = field(default_factory=dict)

    def profile(self, name: str) -> ProfileMeta | None:
        return self.profiles.get(name)

    def ordered_profiles(self) -> list[ProfileMeta]:
        def key(p: ProfileMeta):
            return (0 if p.primary else 1, 0 if p.name == self.active else 1, p.created_at, p.name)

        return sorted(self.profiles.values(), key=key)

    def to_dict(self) -> dict:
        return {
            "version": STATE_VERSION,
            "active": self.active,
            "primary": self.primary,
            "settings": asdict(self.settings),
            "profiles": {name: asdict(meta) for name, meta in self.profiles.items()},
        }


def _backup_bad_state(reason: str, data: bytes | None = None) -> None:
    try:
        if data is None and state_path().is_file():
            data = state_path().read_bytes()
        if data:
            backup_file(state_path(), "state-bad", data)
    except OSError:
        pass


def load_state() -> State:
    path = state_path()
    if not path.exists():
        return State()
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        _backup_bad_state("unreadable")
        return State()
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        _backup_bad_state("version")
        return State()
    state = State(
        version=STATE_VERSION,
        active=data.get("active") if isinstance(data.get("active"), str) else None,
        primary=data.get("primary") if isinstance(data.get("primary"), str) else None,
        settings=Settings.from_dict(data.get("settings")),
    )
    profiles = data.get("profiles")
    if isinstance(profiles, dict):
        for name, meta in profiles.items():
            try:
                safe_name(name, "profil adı")
            except ValueError:
                continue
            state.profiles[name] = ProfileMeta.from_dict(name, meta)
    if state.active and state.active not in state.profiles:
        state.active = None
    if state.primary and state.primary not in state.profiles:
        state.primary = None
    if state.primary:
        for p in state.profiles.values():
            p.primary = p.name == state.primary
    elif state.profiles:
        state.primary = state.active or next(iter(state.profiles))
        state.profiles[state.primary].primary = True
    return state


def save_state(state: State) -> None:
    ensure_dirs()
    state.version = STATE_VERSION
    state.settings.normalized()
    if state.primary and state.primary in state.profiles:
        for p in state.profiles.values():
            p.primary = p.name == state.primary
    if state.active and state.active not in state.profiles:
        state.active = None
    atomic_write_json(state_path(), state.to_dict(), mode=0o600)


def load_quota_cache(profile: str) -> dict | None:
    try:
        data = json.loads(quota_cache_path(profile).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError, UnsafePathError):
        return None


def save_quota_cache(profile: str, payload: dict) -> None:
    try:
        atomic_write_json(quota_cache_path(profile), payload, mode=0o600)
    except (OSError, UnsafePathError):
        pass


def clear_quota_cache(profile: str) -> None:
    try:
        quota_cache_path(profile).unlink(missing_ok=True)
    except (OSError, UnsafePathError):
        pass


def remove_profile_dir(profile: str) -> None:
    try:
        p = profile_dir(profile)
    except UnsafePathError:
        return
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)


def write_profile_marker(profile: str, payload: dict) -> None:
    """Profil dizininde token içermeyen bir metadata işareti bırak."""
    try:
        p = profile_dir(profile)
        p.mkdir(parents=True, exist_ok=True)
        try:
            p.chmod(0o700)
        except OSError:
            pass
        atomic_write_json(p / "profile.json", payload, mode=0o600)
    except (OSError, UnsafePathError):
        pass
