from __future__ import annotations

import time

from conftest import FakeApp, make_secret
from aio.accounts import Accounts
from aio.login import LoginManager


def setup_accounts(tmp_path, fake_keyring):
    fake_app = FakeApp()
    fake_app.APP_STORAGE = tmp_path / "app_storage.json"
    fake_keyring.live_raw = make_secret("main@example.com", access="main")
    accounts = Accounts(fake_keyring, app_module=fake_app)
    accounts.bootstrap()
    return accounts, fake_app


def wait_login(manager: LoginManager, timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.state()
        if not state["running"]:
            return state
        time.sleep(0.03)
    raise AssertionError(f"login did not finish: {manager.state()}")


def test_login_auto_captures_new_account(aio_home, fake_keyring, tmp_path):
    accounts, fake_app = setup_accounts(tmp_path, fake_keyring)

    def login_fn(timeout):
        time.sleep(0.08)
        fake_keyring.live_raw = make_secret("side@example.com", access="side")
        return {"authResult": {"hasValidAuth": True}}

    manager = LoginManager(
        accounts,
        app_module=fake_app,
        login_fn=login_fn,
        wait_ls_fn=lambda timeout: object(),
        timeout=5,
        poll_interval=0.03,
    )
    state = manager.start()
    assert state["running"] is True
    final = wait_login(manager)
    assert final["status"] == "success"
    assert final["captured_profile"] == "side"
    assert fake_keyring.read_live().email == "side@example.com"
    state = accounts.state()
    assert state.active == "side"
    assert state.primary == "main"
    assert state.profiles["side"].email == "side@example.com"


def test_login_existing_account_updates_without_duplicate(aio_home, fake_keyring, tmp_path):
    accounts, fake_app = setup_accounts(tmp_path, fake_keyring)
    fake_keyring.live_raw = make_secret("side@example.com", access="side")
    accounts.capture_live_auto(label="Yan hesap")
    accounts.switch("main", launch=False, validate=False)

    def login_fn(timeout):
        time.sleep(0.08)
        fake_keyring.live_raw = make_secret("side@example.com", access="side-new")
        return {"authResult": {"hasValidAuth": True}}

    manager = LoginManager(
        accounts,
        app_module=fake_app,
        login_fn=login_fn,
        wait_ls_fn=lambda timeout: object(),
        timeout=5,
        poll_interval=0.03,
    )
    manager.start()
    final = wait_login(manager)
    assert final["status"] == "success"
    assert final["captured_profile"] == "side"
    assert accounts.state().active == "side"
    assert "side" in accounts.state().profiles
    assert fake_keyring.read_vault_raw("side")


def test_login_same_account_is_reported(aio_home, fake_keyring, tmp_path):
    accounts, fake_app = setup_accounts(tmp_path, fake_keyring)

    def login_fn(timeout):
        time.sleep(0.05)
        return {"authResult": {"hasValidAuth": True}}

    manager = LoginManager(
        accounts,
        app_module=fake_app,
        login_fn=login_fn,
        wait_ls_fn=lambda timeout: object(),
        timeout=5,
        poll_interval=0.03,
    )
    manager.start()
    final = wait_login(manager)
    assert final["status"] == "success_same"
    assert accounts.state().active == "main"


def test_login_rpc_error_is_reported(aio_home, fake_keyring, tmp_path):
    accounts, fake_app = setup_accounts(tmp_path, fake_keyring)

    def login_fn(timeout):
        raise RuntimeError("login rpc patladı")

    manager = LoginManager(
        accounts,
        app_module=fake_app,
        login_fn=login_fn,
        wait_ls_fn=lambda timeout: object(),
        timeout=5,
        poll_interval=0.03,
    )
    manager.start()
    final = wait_login(manager)
    assert final["status"] == "error"
    assert "login rpc" in (final["error"] or "")
