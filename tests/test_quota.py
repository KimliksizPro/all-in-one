from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from conftest import make_secret
from aio import quota as quota_module, storage
from aio.accounts import Accounts
from aio.keyring import decode_token
from aio.probe import ProbeError, QuotaGroup, UserQuota
from aio.quota import QuotaError, QuotaService, RefreshJob


def make_quota(email: str, pct: float = 90.0) -> UserQuota:
    reset = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    group = QuotaGroup(
        id="model:gemini-3.8-flash",
        family="gemini",
        name="Gemini 3.8 Flash",
        window="bilinmiyor",
        remaining=pct / 100.0,
        remaining_percent=pct,
        reset_at=reset,
        reset_in_seconds=7200,
        exhausted=pct <= 0.001,
        models=["Gemini 3.8 Flash"],
        model_id="gemini-3.8-flash",
    )
    return UserQuota(
        ok=True,
        email=email,
        plan_name="Pro",
        groups=[group],
        worst_remaining=pct / 100.0,
        source="probe",
    )


class FakeProbe:
    def __init__(self, keyring, quotas):
        self.keyring = keyring
        self.quotas = quotas
        self.calls = 0
        self.fail = False

    def probe(self):
        self.calls += 1
        if self.fail:
            raise ProbeError("test probe hatası")
        live = decode_token(self.keyring.read_live_raw())
        return self.quotas[live.email]


def setup(aio_home, fake_keyring, fake_app, tmp_path):
    fake_app.APP_STORAGE = tmp_path / "app_storage.json"
    fake_keyring.live_raw = make_secret("main@example.com", access="a1")
    accounts = Accounts(fake_keyring, app_module=fake_app)
    accounts.bootstrap()
    fake_keyring.live_raw = make_secret("second@example.com", access="a2")
    accounts.capture_current("second")
    accounts.switch("main", launch=False, validate=False)
    return accounts


def test_refresh_active_and_inactive_swap(aio_home, fake_keyring, fake_app, tmp_path):
    accounts = setup(aio_home, fake_keyring, fake_app, tmp_path)
    probe = FakeProbe(fake_keyring, {
        "main@example.com": make_quota("main@example.com", 90),
        "second@example.com": make_quota("second@example.com", 40),
    })
    service = QuotaService(accounts, fake_keyring, probe=probe, app_module=fake_app)

    active = service.refresh_one("main", force=True)
    assert active.ok and active.email == "main@example.com"
    assert fake_keyring.read_live().email == "main@example.com"

    inactive = service.refresh_one("second", force=True)
    assert inactive.ok and inactive.email == "second@example.com"
    assert inactive.worst_remaining == 0.4
    # Geçici keyring değişimi geri alındı.
    assert fake_keyring.read_live().email == "main@example.com"
    assert service.get_cached("second").email == "second@example.com"


def test_refresh_error_keeps_last_good_quota(aio_home, fake_keyring, fake_app, tmp_path):
    accounts = setup(aio_home, fake_keyring, fake_app, tmp_path)
    probe = FakeProbe(fake_keyring, {"second@example.com": make_quota("second@example.com", 40)})
    service = QuotaService(accounts, fake_keyring, probe=probe, app_module=fake_app)
    first = service.refresh_one("second", force=True)
    assert first.ok
    probe.fail = True
    second = service.refresh_one("second", force=True)
    assert second.ok is False
    assert second.groups and second.groups[0].remaining_percent == 40.0
    assert second.stale is True
    assert "test probe" in (second.error or "")
    assert first.ok is True
    assert first.error is None
    assert service.get_cached("second").stale is True


def test_refresh_inactive_while_app_running_needs_stop(aio_home, fake_keyring, fake_app, tmp_path):
    accounts = setup(aio_home, fake_keyring, fake_app, tmp_path)
    fake_app.running_flag = True
    probe = FakeProbe(fake_keyring, {"main@example.com": make_quota("main@example.com", 90)})
    service = QuotaService(accounts, fake_keyring, probe=probe, app_module=fake_app)
    with pytest.raises(QuotaError) as exc:
        service.refresh_one("second", force=True)
    assert exc.value.needs_stop is True


def test_refresh_all_requires_closed_app_with_multiple_profiles(aio_home, fake_keyring, fake_app, tmp_path):
    accounts = setup(aio_home, fake_keyring, fake_app, tmp_path)
    fake_app.running_flag = True
    probe = FakeProbe(fake_keyring, {})
    service = QuotaService(accounts, fake_keyring, probe=probe, app_module=fake_app)
    with pytest.raises(QuotaError) as exc:
        service.refresh_all(force=True)
    assert exc.value.needs_stop is True


@pytest.fixture
def cache_service(aio_home, fake_app):
    profiles = [SimpleNamespace(name="main"), SimpleNamespace(name="second")]
    state = SimpleNamespace(
        profiles={profile.name: profile for profile in profiles},
        ordered_profiles=lambda: profiles,
    )
    accounts = SimpleNamespace(keyring=object(), state=lambda: state)
    return QuotaService(accounts, app_module=fake_app)


def test_cached_consumer_cannot_mutate_nested_retained_entries(cache_service):
    original = make_quota("main@example.com")
    cache_service._save_cache("main", original)
    original.groups[0].models.append("producer mutation")
    original.groups[0].remaining_percent = 1
    first = cache_service.get_cached("main")
    first.groups[0].models.append("consumer mutation")
    first.groups[0].remaining_percent = 2
    first.groups.clear()
    first.email = "changed@example.com"
    serialized = cache_service.get_cached("main").to_dict()
    serialized["groups"][0]["models"].append("serialized mutation")
    second = cache_service.get_cached("main")
    assert second.email == "main@example.com"
    assert second.groups[0].remaining_percent == 90
    assert second.groups[0].models == ["Gemini 3.8 Flash"]


def test_cached_remaining_fraction_preserves_precision(cache_service, monkeypatch):
    monkeypatch.setattr(storage, "save_quota_cache", lambda *args: None)
    original = make_quota("main@example.com")
    original.groups[0].remaining = 0.123456789
    cache_service._save_cache("main", original)
    cached = cache_service.get_cached("main")
    assert cached.groups[0].remaining == 0.123456789
    cached.groups[0].models.clear()
    assert cache_service.get_cached("main").groups[0].models == ["Gemini 3.8 Flash"]


def test_cache_observes_external_atomic_updates_and_deletion(cache_service, monkeypatch):
    loads = []
    load = storage.load_quota_cache

    def counted(name):
        loads.append(name)
        return load(name)

    monkeypatch.setattr(storage, "load_quota_cache", counted)
    cache_service._save_cache("main", make_quota("main@example.com", 90))
    assert cache_service.get_cached("main").worst_remaining == 0.9
    assert cache_service.get_cached("main").worst_remaining == 0.9
    assert len(loads) == 1
    storage.save_quota_cache("main", {"version": quota_module.CACHE_VERSION, "quota": make_quota("main@example.com", 40).to_dict()})
    assert cache_service.get_cached("main").worst_remaining == 0.4
    assert len(loads) == 2
    storage.clear_quota_cache("main")
    assert cache_service.get_cached("main") is None
    storage.save_quota_cache("main", {"version": quota_module.CACHE_VERSION, "quota": make_quota("main@example.com", 20).to_dict()})
    assert cache_service.cached_map()["main"].worst_remaining == 0.2


def test_cache_observes_deletion_before_first_read(cache_service):
    cache_service._save_cache("main", make_quota("main@example.com"))
    storage.clear_quota_cache("main")
    assert cache_service.get_cached("main") is None


def test_cache_observes_external_in_place_updates(cache_service):
    cache_service._save_cache("main", make_quota("main@example.com", 90))
    assert cache_service.get_cached("main").groups[0].remaining_percent == 90
    path = storage.quota_cache_path("main")
    # Same inode and byte length: content must not be keyed by size alone.
    raw = path.read_text()
    updated = raw.replace('"remaining": 0.9', '"remaining": 0.4')
    assert len(raw) == len(updated) and raw != updated
    old = path.stat()
    path.write_text(updated)
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000))
    assert cache_service.get_cached("main").groups[0].remaining_percent == 40


def test_error_uses_latest_external_good_quota_without_mutating_it(cache_service):
    cache_service._save_cache("main", make_quota("main@example.com", 90))
    storage.save_quota_cache("main", {"version": quota_module.CACHE_VERSION, "quota": make_quota("main@example.com", 40).to_dict()})
    failed = cache_service._error_quota("main", "main@example.com", "probe failed", needs_login=True)
    assert not failed.ok and failed.stale and failed.needs_login
    assert failed.groups[0].remaining_percent == 40
    assert cache_service.get_cached("main").ok
    cache_service._save_cache("main", failed)
    cached = cache_service.get_cached("main")
    assert not cached.ok and cached.stale and cached.needs_login
    assert cached.error == "probe failed"
    assert cached.groups[0].remaining_percent == 40


@pytest.mark.parametrize("ok,ttl", [(True, 300), (False, 60)])
def test_cache_ttl_recomputed_without_clearing_explicit_stale(cache_service, monkeypatch, ok, ttl):
    clock = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(quota_module, "utc_now", lambda: clock)
    quota = make_quota("main@example.com")
    quota.ok = ok
    quota.fetched_at = clock.isoformat()
    cache_service._save_cache("main", quota)
    assert cache_service.get_cached("main").stale is False
    clock += timedelta(seconds=ttl + 1)
    assert cache_service.get_cached("main").stale is True
    quota.stale = True
    quota.fetched_at = clock.isoformat()
    cache_service._save_cache("main", quota)
    assert cache_service.get_cached("main").stale is True


@pytest.mark.parametrize("payload", ["not json", '{"version": 2, "quota": {"raw_model_count": "invalid"}}'])
def test_external_malformed_cache_does_not_return_retained_data(cache_service, payload):
    cache_service._save_cache("main", make_quota("main@example.com"))
    assert cache_service.get_cached("main") is not None
    storage.quota_cache_path("main").write_text(payload)
    assert cache_service.get_cached("main") is None


def test_refresh_job_records_failed_results_and_continues(cache_service, monkeypatch):
    calls = []

    def refresh(name, **kwargs):
        calls.append(name)
        return UserQuota(ok=False, error="test failure") if name == "main" else make_quota("second@example.com")

    monkeypatch.setattr(cache_service, "refresh_one", refresh)
    cache_service._job = RefreshJob(running=True)
    cache_service._run_job()
    job = cache_service.job_state()
    assert calls == ["main", "second"]
    assert job["errors"] == ["main: test failure"]
    assert job["done"] == job["total"] == 2
    assert not job["running"] and job["current"] is None and job["finished_at"]
    assert job["error"] is None


def test_refresh_job_needs_stop_records_attempt_before_break(cache_service, monkeypatch):
    calls = []

    def refresh(name, **kwargs):
        calls.append(name)
        raise QuotaError("stop required", needs_stop=True)

    monkeypatch.setattr(cache_service, "refresh_one", refresh)
    cache_service._job = RefreshJob(running=True)
    cache_service._run_job()
    job = cache_service.job_state()
    assert calls == ["main"]
    assert job["errors"] == ["main: stop required"]
    assert job["done"] == 1 and job["total"] == 2
    assert not job["running"]


def test_lossy_legacy_cache_requires_fresh_model_read(cache_service):
    storage.save_quota_cache("main", {"version": 1, "quota": make_quota("main@example.com").to_dict()})
    assert cache_service.get_cached("main") is None
    assert cache_service.cached_map() == {}


def test_unknown_model_quota_survives_cache_without_becoming_exhausted(cache_service):
    quota = make_quota("main@example.com")
    quota.groups[0].remaining = None
    quota.groups[0].remaining_percent = None
    quota.groups[0].exhausted = False
    quota.worst_remaining = None
    cache_service._save_cache("main", quota)
    cached = cache_service.get_cached("main")
    assert cached.groups[0].remaining is None
    assert cached.groups[0].remaining_percent is None
    assert not cached.groups[0].exhausted
    assert cached.worst_remaining is None


def test_failed_probe_backs_off_for_automatic_refresh(aio_home, fake_keyring, fake_app, tmp_path):
    accounts = setup(aio_home, fake_keyring, fake_app, tmp_path)
    probe = FakeProbe(fake_keyring, {"second@example.com": make_quota("second@example.com", 40)})
    service = QuotaService(accounts, fake_keyring, probe=probe, app_module=fake_app)
    service.refresh_one("second", force=True)
    probe.fail = True
    service.refresh_one("second", force=True)
    calls = probe.calls
    # Otomatik yenileme geri çekilme süresinde sunucuya tekrar gitmez.
    service.refresh_one("second", force=False)
    assert probe.calls == calls
    # Kullanıcı açıkça isterse geri çekilme aşılır.
    service.refresh_one("second", force=True)
    assert probe.calls == calls + 1


def test_rate_limit_error_uses_long_cooldown(aio_home, fake_keyring, fake_app, tmp_path):
    accounts = setup(aio_home, fake_keyring, fake_app, tmp_path)
    service = QuotaService(accounts, fake_keyring, probe=FakeProbe(fake_keyring, {}), app_module=fake_app)
    service._record_failure("second", "HTTP 429 RESOURCE_EXHAUSTED")
    remaining = service._cooldown_until["second"] - quota_module.time.monotonic()
    assert remaining >= quota_module.RATE_LIMIT_COOLDOWN_SECONDS * 0.79


def test_forced_refresh_within_min_interval_reuses_cache(aio_home, fake_keyring, fake_app, tmp_path, monkeypatch):
    monkeypatch.setattr(quota_module, "MIN_FETCH_INTERVAL_SECONDS", 20)
    accounts = setup(aio_home, fake_keyring, fake_app, tmp_path)
    probe = FakeProbe(fake_keyring, {"second@example.com": make_quota("second@example.com", 40)})
    service = QuotaService(accounts, fake_keyring, probe=probe, app_module=fake_app)
    service.refresh_one("second", force=True)
    service.refresh_one("second", force=True)
    assert probe.calls == 1
