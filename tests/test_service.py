from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from aio import antigravity
from aio.accounts import Accounts
from aio.keyring import KEYRING_LOCK
from aio.service import Service
from conftest import FakeApp, FakeKeyring, make_secret


class CountingKeyring(FakeKeyring):
    def __init__(self):
        super().__init__()
        self.live_reads = 0

    def read_live(self, **kwargs):
        self.live_reads += 1
        return super().read_live(**kwargs)


@pytest.fixture()
def observed_service(aio_home, monkeypatch):
    keyring = CountingKeyring()
    keyring.live_raw = make_secret("main@example.com")
    accounts = Accounts(keyring, app_module=FakeApp())
    service = Service(keyring=keyring, accounts=accounts)
    service.bootstrap()
    info = antigravity.AppInfo(True, "/fake/app", "/fake/ls", "test", False, [], "/fake/config", "/fake/data")
    monkeypatch.setattr(antigravity, "app_info", lambda: info)
    return service, keyring, info


def test_warm_snapshot_does_not_wait_for_keyring(observed_service):
    service, keyring, _info = observed_service
    reads = keyring.live_reads
    with KEYRING_LOCK:
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(service.snapshot).result(timeout=1)
    assert result["live"]["email"] == "main@example.com"
    assert result["active"] == "main"
    assert keyring.live_reads == reads


def test_observation_cannot_block_cached_requests(observed_service, monkeypatch):
    service, keyring, _info = observed_service
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_read = keyring.read_live

    def blocked_read(**kwargs):
        started.set()
        try:
            assert release.wait(3)
            return original_read(**kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(keyring, "read_live", blocked_read)
    service._live_observed = 0.0
    try:
        first = service.snapshot()
        assert started.wait(1)
        with ThreadPoolExecutor(max_workers=1) as executor:
            second = executor.submit(service.snapshot).result(timeout=1)
        assert first["live"]["email"] == second["live"]["email"] == "main@example.com"
        assert second["keyring_busy"] is True
    finally:
        release.set()
        assert finished.wait(1)


def test_capture_response_matches_new_live_account(observed_service):
    service, keyring, _info = observed_service
    keyring.live_raw = make_secret("other@example.com")
    service.capture("other")
    snapshot = service.snapshot()
    assert snapshot["active"] == "other"
    assert snapshot["live"]["email"] == "other@example.com"
    assert snapshot["live"]["matches_active"] is True


def test_auth_cache_is_bound_to_account_and_process(observed_service, monkeypatch):
    service, _keyring, info = observed_service
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    running = replace(info, running=True, pids=[100])

    def old_auth(**kwargs):
        entered.set()
        try:
            assert release.wait(3)
            return {"authResult": {"ineligible": {"verificationUrl": "https://accounts.google.com/verify"}}}
        finally:
            finished.set()

    monkeypatch.setattr(antigravity, "fetch_auth_status", old_auth)
    assert service._refresh_auth_status("old@example.com", running) is None
    assert entered.wait(1)
    # Neither an account change nor a process restart may retain the previous rejection.
    assert service._refresh_auth_status("new@example.com", replace(running, pids=[200])) is None
    release.set()
    assert finished.wait(1)
    with service._auth_lock:
        assert service._auth_data is None


def test_unknown_auth_is_not_reported_as_eligible():
    assert Service._summarize_auth({"authResult": {}})["eligible"] is None
    assert Service._summarize_auth({"authResult": {"hasValidAuth": False}})["eligible"] is None
    assert Service._summarize_auth({"authResult": {"hasValidAuth": True}})["eligible"] is True
    rejected = Service._summarize_auth({"authResult": {"ineligible": {}, "uiMessage": "Verify account"}})
    assert rejected["eligible"] is False
    assert rejected["message"] == "Verify account"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://evil.example/verify", "https://accounts.google.com@evil.example/"])
def test_verification_action_rejects_untrusted_destination(url):
    auth = Service._summarize_auth({"authResult": {"ineligible": {"verificationUrl": url}}})
    assert auth["verification_url"] is None
