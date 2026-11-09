from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from aio import storage
from aio.keyring import TokenBundle, decode_token


def make_secret(email: str = "main@example.com", *, access: str = "access-main",
                refresh: str = "refresh-main", expired: bool = False) -> str:
    expiry = datetime.now(timezone.utc) + (timedelta(hours=-1) if expired else timedelta(hours=1))
    return json.dumps(
        {
            "token": {
                "access_token": access,
                "token_type": "Bearer",
                "refresh_token": refresh,
                "expiry": expiry.isoformat(),
                "expires_in": 3599,
            },
            "auth_method": "consumer",
            "id_token_email": email,
            "writer_id": "test-writer",
        }
    )


class FakeKeyring:
    def __init__(self):
        self.live_raw: str | None = None
        self.vault: dict[str, str] = {}

    def available(self) -> bool:
        return True

    def read_live_raw(self) -> str | None:
        return self.live_raw

    def write_live_raw(self, raw: str) -> None:
        self.live_raw = raw

    def read_live(self, *, resolve_email: bool = False) -> TokenBundle | None:
        if not self.live_raw:
            return None
        return decode_token(self.live_raw, resolve_email=resolve_email)

    def read_vault_raw(self, profile: str) -> str | None:
        return self.vault.get(profile)

    def write_vault_raw(self, profile: str, raw: str) -> None:
        self.vault[profile] = raw

    def read_vault(self, profile: str, *, resolve_email: bool = False) -> TokenBundle | None:
        raw = self.vault.get(profile)
        return decode_token(raw, resolve_email=resolve_email) if raw else None

    def delete_vault_raw(self, profile: str) -> None:
        self.vault.pop(profile, None)

    def clear(self, service: str, username: str) -> None:
        if service == "gemini" and username == "antigravity":
            self.live_raw = None


class FakeApp:
    APP_STORAGE = Path("/tmp/aio-test-app-storage.json")

    def __init__(self):
        self.running_flag = False
        self.stopped: list[int] = []
        self.started: list[int] = []
        self.last_login: str | None = None

    def running(self) -> bool:
        return self.running_flag

    def stop(self, timeout: float = 12.0, force: bool = False):
        self.stopped.append(1)
        self.running_flag = False
        return SimpleNamespace(stopped=[1], remaining=[], forced=force)

    def start(self) -> int:
        self.running_flag = True
        self.started.append(123)
        return 123

    def update_last_login(self, email: str):
        self.last_login = email
        return None


class FakeValidator:
    def __init__(self, email: str = "main@example.com", ok: bool = True):
        self.email = email
        self.ok = ok
        self.calls: list[str] = []

    def __call__(self, name: str):
        self.calls.append(name)
        return SimpleNamespace(ok=self.ok, email=self.email, error=None if self.ok else "test hatası")


@pytest.fixture()
def aio_home(tmp_path, monkeypatch):
    home = tmp_path / "aio-home"
    monkeypatch.setenv("AIO_HOME", str(home))
    storage.ensure_dirs()
    return home


@pytest.fixture()
def fake_keyring():
    return FakeKeyring()


@pytest.fixture()
def fake_app():
    return FakeApp()


@pytest.fixture(autouse=True)
def _no_fetch_pauses(monkeypatch):
    from aio import quota

    monkeypatch.setattr(quota, "ACCOUNT_PAUSE_SECONDS", (0.0, 0.0))
    monkeypatch.setattr(quota, "MIN_FETCH_INTERVAL_SECONDS", 0)
