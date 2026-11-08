from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from aio import storage
from aio.alerts import AlertManager
from aio.probe import QuotaGroup, UserQuota


class FakeNotifier:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.sent: list[tuple[str, str, str]] = []

    def available(self) -> bool:
        return True

    def send(self, title: str, body: str, *, urgency: str = "normal") -> bool:
        self.sent.append((title, body, urgency))
        return self.ok


FIXED_RESET = "2026-11-05T19:00:00+00:00"


def make_quota(remaining_percent: float, *, reset_at: str | None = None) -> UserQuota:
    reset = reset_at or FIXED_RESET
    group = QuotaGroup(
        id="model:gemini-3.8-flash",
        family="gemini",
        name="Gemini 3.8 Flash",
        window="bilinmiyor",
        remaining=remaining_percent / 100.0,
        remaining_percent=remaining_percent,
        reset_at=reset,
        reset_in_seconds=7200,
        exhausted=remaining_percent <= 0.001,
        models=["Gemini 3.8 Flash"],
        model_id="gemini-3.8-flash",
    )
    return UserQuota(ok=True, email="test@example.com", groups=[group], worst_remaining=remaining_percent / 100.0)


def test_alert_threshold_transitions_and_dedup(tmp_path):
    notifier = FakeNotifier()
    manager = AlertManager(tmp_path / "alerts.json", notifier=notifier)
    settings = storage.Settings(warn_percent=20, critical_percent=5)

    alerts, sent = manager.evaluate([("main", "Ana", make_quota(15))], settings)
    assert len(alerts) == 1 and alerts[0].level == "warn"
    assert len(sent) == 1 and notifier.sent

    alerts, sent = manager.evaluate([("main", "Ana", make_quota(15))], settings)
    assert len(alerts) == 1 and sent == []

    alerts, sent = manager.evaluate([("main", "Ana", make_quota(3))], settings)
    assert alerts[0].level == "critical"
    assert len(sent) == 1

    alerts, sent = manager.evaluate([("main", "Ana", make_quota(0))], settings)
    assert alerts[0].level == "empty"
    assert len(sent) == 1

    alerts, sent = manager.evaluate([("main", "Ana", make_quota(75))], settings)
    assert alerts == [] and sent == []


def test_alert_reset_change_renotifies(tmp_path):
    notifier = FakeNotifier()
    manager = AlertManager(tmp_path / "alerts.json", notifier=notifier)
    settings = storage.Settings(warn_percent=20, critical_percent=5)
    reset_a = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    reset_b = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    _, sent = manager.evaluate([("main", "Ana", make_quota(10, reset_at=reset_a))], settings)
    assert len(sent) == 1
    _, sent = manager.evaluate([("main", "Ana", make_quota(10, reset_at=reset_b))], settings)
    assert len(sent) == 1


def test_notification_failure_retries(tmp_path):
    notifier = FakeNotifier(ok=False)
    manager = AlertManager(tmp_path / "alerts.json", notifier=notifier)
    settings = storage.Settings(warn_percent=20, critical_percent=5)
    _, sent = manager.evaluate([("main", "Ana", make_quota(10))], settings)
    assert sent == [] and len(notifier.sent) == 1
    notifier.ok = True
    _, sent = manager.evaluate([("main", "Ana", make_quota(10))], settings)
    assert len(sent) == 1
    assert len(notifier.sent) == 2


def test_notify_false_does_not_send(tmp_path):
    notifier = FakeNotifier()
    manager = AlertManager(tmp_path / "alerts.json", notifier=notifier)
    settings = storage.Settings(warn_percent=20, critical_percent=5)
    alerts, sent = manager.evaluate([("main", "Ana", make_quota(10))], settings, notify=False)
    assert alerts and sent == [] and notifier.sent == []


def test_unknown_model_is_not_empty_and_distinct_models_alert_independently(tmp_path):
    manager = AlertManager(tmp_path / "alerts.json", notifier=FakeNotifier())
    settings = storage.Settings(warn_percent=20, critical_percent=5)
    quota = make_quota(0)
    empty = quota.groups[0]
    unknown = replace(empty, id="model:argon-high", name="Argon High", family="argon", remaining=None, remaining_percent=None, exhausted=False)
    low = replace(empty, id="model:claude-low", name="Claude Low", family="claude", remaining=0.1, remaining_percent=10, exhausted=False)
    quota.groups = [empty, unknown, low]
    current = manager.current_alerts([("main", "Ana", quota)], settings)
    evaluated, sent = manager.evaluate([("main", "Ana", quota)], settings)
    assert [(a.group_id, a.level) for a in current] == [(empty.id, "empty"), (low.id, "warn")]
    assert {(a.group_id, a.level) for a in evaluated} == {(empty.id, "empty"), (low.id, "warn")}
    assert {a.group_id for a in sent} == {empty.id, low.id}
