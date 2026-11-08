from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from conftest import FakeApp, FakeKeyring, make_secret
from aio.accounts import Accounts
from aio.probe import QuotaGroup, UserQuota
from aio.quota import QuotaService
from aio.server import create_app
from aio.service import Service


class FakeProbe:
    def __init__(self):
        self.fail = False

    def probe(self):
        if self.fail:
            raise RuntimeError("probe hatası")
        reset = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        group = QuotaGroup(
            id="model:gemini-flash",
            family="gemini",
            name="Gemini Flash",
            window="bilinmiyor",
            remaining=0.5,
            remaining_percent=50.0,
            reset_at=reset,
            reset_in_seconds=7200,
            exhausted=False,
            models=["Gemini"],
            model_id="gemini-flash",
        )
        return UserQuota(ok=True, email="main@example.com", plan_name="Pro", groups=[group], worst_remaining=0.5)


def make_service(tmp_path):
    fake_app = FakeApp()
    fake_app.APP_STORAGE = tmp_path / "app_storage.json"
    keyring = FakeKeyring()
    keyring.live_raw = make_secret("main@example.com")
    accounts = Accounts(keyring, app_module=fake_app)
    probe = FakeProbe()
    quota = QuotaService(accounts, keyring, probe=probe, app_module=fake_app)
    return Service(keyring=keyring, accounts=accounts, quota=quota), fake_app, keyring


def test_api_state_and_capture(tmp_path, aio_home):
    service, fake_app, keyring = make_service(tmp_path)
    app = create_app(service, start_poller=False)
    with TestClient(app) as client:
        response = client.get("/api/state")
        assert response.status_code == 200
        data = response.json()
        assert data["active"] == "main"
        assert data["accounts"][0]["email"] == "main@example.com"
        assert "access_token" not in response.text

        keyring.live_raw = make_secret("second@example.com")
        response = client.post("/api/accounts/capture", json={"name": "second"})
        assert response.status_code == 200, response.text
        assert response.json()["state"]["active"] == "second"
        names = [a["name"] for a in response.json()["state"]["accounts"]]
        assert "second" in names


def test_api_switch_and_security(tmp_path, aio_home):
    service, fake_app, keyring = make_service(tmp_path)
    app = create_app(service, start_poller=False)
    with TestClient(app) as client:
        assert client.get("/api/state").status_code == 200
        # Yerel olmayan Origin reddedilir.
        response = client.post(
            "/api/accounts/main/switch",
            json={"launch": False, "verify": False},
            headers={"origin": "https://evil.example"},
        )
        assert response.status_code == 403
        response = client.post(
            "/api/accounts/main/switch",
            json={"launch": False, "verify": False},
        )
        assert response.status_code == 200, response.text
        assert response.json()["state"]["active"] == "main"
        # Yerel olmayan Host reddedilir.
        response = client.get("/api/state", headers={"host": "evil.example"})
        assert response.status_code == 403


def test_api_settings(tmp_path, aio_home):
    service, fake_app, keyring = make_service(tmp_path)
    app = create_app(service, start_poller=False)
    with TestClient(app) as client:
        response = client.put("/api/settings", json={"warn_percent": 30, "critical_percent": 8})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["settings"]["warn_percent"] == 30
        assert data["settings"]["critical_percent"] == 8
