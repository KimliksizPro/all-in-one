from __future__ import annotations

import pytest

from conftest import FakeValidator, make_secret
from aio.accounts import AccountConflict, AccountError, Accounts, AppRunningError


def new_accounts(tmp_path, fake_keyring, fake_app):
    fake_app.APP_STORAGE = tmp_path / "app_storage.json"
    return Accounts(fake_keyring, app_module=fake_app)


def test_bootstrap_captures_main(aio_home, fake_keyring, fake_app):
    fake_keyring.live_raw = make_secret("main@example.com")
    accounts = new_accounts(aio_home, fake_keyring, fake_app)
    state = accounts.bootstrap()
    assert state.active == "main" and state.primary == "main"
    assert state.profiles["main"].email == "main@example.com"
    assert fake_keyring.read_vault_raw("main") == fake_keyring.live_raw
    # Tekrar bootstrap token'ı gereksiz yazmaz.
    before = fake_keyring.vault["main"]
    state = accounts.bootstrap()
    assert fake_keyring.vault["main"] == before


def test_capture_second_and_duplicate(aio_home, fake_keyring, fake_app):
    fake_keyring.live_raw = make_secret("main@example.com", access="a1")
    accounts = new_accounts(aio_home, fake_keyring, fake_app)
    accounts.bootstrap()
    fake_keyring.live_raw = make_secret("second@example.com", access="a2")
    result = accounts.capture_current("second", label="İkinci")
    assert result.profile.email == "second@example.com"
    state = accounts.state()
    assert state.active == "second"
    assert state.primary == "main"
    assert state.profiles["second"].label == "İkinci"

    with pytest.raises(AccountConflict):
        accounts.capture_current("other")

    # Aynı hesabı overwrite ile tazeleme (ana hesap korunur).
    fake_keyring.live_raw = make_secret("main@example.com", access="a1-new")
    result = accounts.capture_current("main", overwrite=True)
    assert result.already is True
    assert accounts.state().primary == "main"
    assert accounts.state().profiles["main"].primary is True


def test_switch_running_requires_stop(aio_home, fake_keyring, fake_app):
    fake_keyring.live_raw = make_secret("main@example.com", access="a1")
    accounts = new_accounts(aio_home, fake_keyring, fake_app)
    accounts.bootstrap()
    fake_keyring.live_raw = make_secret("second@example.com", access="a2")
    accounts.capture_current("second")
    fake_app.running_flag = True
    with pytest.raises(AppRunningError):
        accounts.switch("main", stop=False, launch=False, validate=False)
    result = accounts.switch("main", stop=True, launch=False, validate=False)
    assert result.stopped is True
    assert fake_keyring.read_live().email == "main@example.com"
    assert accounts.state().active == "main"


def test_switch_validator_and_restore_on_failure(aio_home, fake_keyring, fake_app):
    fake_keyring.live_raw = make_secret("main@example.com", access="a1")
    accounts = new_accounts(aio_home, fake_keyring, fake_app)
    accounts.bootstrap()
    fake_keyring.live_raw = make_secret("second@example.com", access="a2")
    accounts.capture_current("second")
    accounts.validator = FakeValidator(email="main@example.com")
    result = accounts.switch("main", launch=False, validate=True)
    assert result.profile == "main"
    assert accounts.validator.calls == ["main"]

    before = fake_keyring.read_live_raw()
    accounts.validator = FakeValidator(ok=False)
    with pytest.raises(AccountError):
        accounts.switch("second", launch=False, validate=True)
    assert fake_keyring.read_live_raw() == before
    assert accounts.state().active == "main"


def test_rename_primary_and_delete_protection(aio_home, fake_keyring, fake_app):
    fake_keyring.live_raw = make_secret("main@example.com")
    accounts = new_accounts(aio_home, fake_keyring, fake_app)
    accounts.bootstrap()
    fake_keyring.live_raw = make_secret("second@example.com")
    accounts.capture_current("second")
    meta = accounts.set_primary("second")
    assert meta.primary and accounts.state().primary == "second"
    with pytest.raises(AccountError):
        accounts.delete("second")  # hem aktif hem ana
    accounts.set_primary("main")
    with pytest.raises(AccountError):
        accounts.delete("second")  # hâlâ aktif
    accounts.switch("main", launch=False, validate=False)
    accounts.delete("second")  # aktif değil, ana değil
    assert "second" not in accounts.state().profiles
    assert fake_keyring.read_vault_raw("second") is None

    meta = accounts.set_primary("main")
    with pytest.raises(AccountError):
        accounts.delete("main")
    renamed = accounts.rename("main", "ana")
    assert renamed.name == "ana"
    assert accounts.state().active == "ana"
    assert accounts.state().primary == "ana"
    assert "main" not in accounts.state().profiles
    assert fake_keyring.read_vault_raw("ana") is not None
    assert fake_keyring.read_vault_raw("main") is None
