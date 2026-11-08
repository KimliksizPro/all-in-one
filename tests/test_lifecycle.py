from __future__ import annotations

from fastapi.testclient import TestClient

from aio import antigravity
from aio.server import create_app
from test_server import make_service


def test_start_does_not_launch_second_app(aio_home, tmp_path, monkeypatch):
    service, _app, _keyring = make_service(tmp_path)
    monkeypatch.setattr(antigravity, "running", lambda: True)

    def unexpected_start():
        raise AssertionError("A second Antigravity instance must not be launched")

    monkeypatch.setattr(antigravity, "start", unexpected_start)
    with TestClient(create_app(service, start_poller=False)) as client:
        response = client.post("/api/app/start", json={})
    assert response.status_code == 200
    assert response.json()["already_running"] is True


def test_stop_failure_is_not_reported_as_success(aio_home, tmp_path, monkeypatch):
    service, _app, _keyring = make_service(tmp_path)
    monkeypatch.setattr(antigravity, "stop", lambda *_args: antigravity.StopResult([10], [11]))
    with TestClient(create_app(service, start_poller=False)) as client:
        response = client.post("/api/app/stop", json={"force": False})
    assert response.status_code == 409
    assert response.json()["needs_stop"] is True
    assert "11" in response.json()["detail"]
