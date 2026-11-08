from __future__ import annotations

import pytest

from aio import storage
from aio.storage import ProfileMeta, State, UnsafePathError


def test_state_roundtrip(aio_home):
    state = State(active="main", primary="main")
    state.profiles["main"] = ProfileMeta(name="main", label="Ana", email="a@example.com", created_at="2026-01-01T00:00:00+00:00")
    state.settings.warn_percent = 25
    storage.save_state(state)
    loaded = storage.load_state()
    assert loaded.active == "main"
    assert loaded.primary == "main"
    assert loaded.profiles["main"].email == "a@example.com"
    assert loaded.settings.warn_percent == 25


def test_unsafe_profile_names(aio_home):
    with pytest.raises((UnsafePathError, ValueError)):
        storage.profile_dir("../evil")
    with pytest.raises((UnsafePathError, ValueError)):
        storage.quota_cache_path("a/b")
    assert storage.profile_dir("main").is_relative_to(storage.root_dir())


def test_quota_cache_roundtrip(aio_home):
    storage.save_quota_cache("main", {"quota": {"ok": True}})
    assert storage.load_quota_cache("main") == {"quota": {"ok": True}}
    storage.clear_quota_cache("main")
    assert storage.load_quota_cache("main") is None
