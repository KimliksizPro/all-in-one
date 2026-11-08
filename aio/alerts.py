"""Kota eşikleri, kalıcı uyarı durumu ve masaüstü bildirimleri."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Callable

from . import storage
from .probe import QuotaGroup, UserQuota
from .util import atomic_write_json, human_duration, human_percent, now_iso, parse_iso, utc_now

LEVEL_RANK = {"ok": 0, "warn": 1, "critical": 2, "empty": 3}
LEVEL_LABEL = {
    "ok": "Normal",
    "warn": "Azalıyor",
    "critical": "Kritik",
    "empty": "Doldu",
}


@dataclass
class Alert:
    level: str
    account: str
    label: str
    group_id: str
    group_name: str
    remaining: float
    reset_at: str | None
    message: str
    notified: bool = False

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "level_label": LEVEL_LABEL.get(self.level, self.level),
            "account": self.account,
            "label": self.label,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "remaining": self.remaining,
            "remaining_label": human_percent(self.remaining),
            "reset_at": self.reset_at,
            "message": self.message,
            "notified": self.notified,
        }


def group_level(remaining_percent: float | None, warn_percent: float, critical_percent: float) -> str:
    if remaining_percent is None:
        return "ok"
    if remaining_percent <= 0:
        return "empty"
    if remaining_percent <= critical_percent:
        return "critical"
    if remaining_percent <= warn_percent:
        return "warn"
    return "ok"


def _reset_text(reset_at: str | None, reset_in_seconds: int | None = None) -> str:
    if reset_in_seconds is None and reset_at:
        dt = parse_iso(reset_at)
        if dt:
            reset_in_seconds = int(max(0, (dt - utc_now()).total_seconds()))
    if reset_in_seconds is None:
        return "sıfırlanma bilinmiyor"
    return f"sıfırlanma {human_duration(reset_in_seconds)} sonra"


class AlertNotifier:
    def __init__(self, command: str | None = None):
        self.command = command or shutil.which("notify-send") or "notify-send"

    def available(self) -> bool:
        return bool(shutil.which(self.command))

    def send(self, title: str, body: str, *, urgency: str = "normal") -> bool:
        if not self.available():
            return False
        try:
            proc = subprocess.run(
                [
                    self.command,
                    "-a",
                    "all-in-one",
                    "-u",
                    urgency if urgency in {"low", "normal", "critical"} else "normal",
                    "-t",
                    "12000",
                    title,
                    body,
                ],
                capture_output=True,
                timeout=8,
            )
            return proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


class AlertManager:
    def __init__(
        self,
        path: Path | None = None,
        notifier: AlertNotifier | None = None,
        *,
        notifier_enabled: Callable[[], bool] | None = None,
    ):
        self.path = path or storage.alerts_path()
        self.notifier = notifier or AlertNotifier()
        self.notifier_enabled = notifier_enabled or (lambda: True)
        self._lock = threading.RLock()
        self._seen: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        data = storage.read_json(self.path, {}) if hasattr(storage, "read_json") else None
        if data is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        entries = data.get("entries") if isinstance(data, dict) else {}
        self._seen = entries if isinstance(entries, dict) else {}

    def save(self) -> None:
        try:
            atomic_write_json(
                self.path,
                {"version": 1, "updated_at": now_iso(), "entries": self._seen},
                mode=0o600,
            )
        except OSError:
            pass

    def _entry_key(self, account: str, group: QuotaGroup) -> str:
        return f"{account}|{group.id}"

    def _build_alert(self, account: str, label: str, group: QuotaGroup, level: str) -> Alert:
        remaining = group.remaining_percent
        message = (
            f"{label} ({account}) — {group.name}: {human_percent(remaining)} kaldı, "
            f"{_reset_text(group.reset_at, group.reset_in_seconds)}."
        )
        return Alert(
            level=level,
            account=account,
            label=label,
            group_id=group.id,
            group_name=group.name,
            remaining=remaining,
            reset_at=group.reset_at,
            message=message,
        )

    def current_alerts(
        self,
        accounts: Iterable[tuple[str, str, UserQuota]],
        settings: storage.Settings,
    ) -> list[Alert]:
        out: list[Alert] = []
        for name, label, quota in accounts:
            if not quota.ok:
                continue
            for group in quota.groups:
                level = group_level(group.remaining_percent, settings.warn_percent, settings.critical_percent)
                if level != "ok":
                    out.append(self._build_alert(name, label, group, level))
        out.sort(key=lambda a: (-LEVEL_RANK.get(a.level, 0), a.remaining, a.account))
        return out

    def evaluate(
        self,
        accounts: Iterable[tuple[str, str, UserQuota]],
        settings: storage.Settings,
        *,
        notify: bool = True,
    ) -> tuple[list[Alert], list[Alert]]:
        """Güncel uyarıları ve (gönderilmiş) bildirimleri döndür."""
        with self._lock:
            accounts = list(accounts)
            current: list[Alert] = []
            to_notify: list[Alert] = []
            active_keys: set[str] = set()
            now = now_iso()
            for name, label, quota in accounts:
                if not quota.ok:
                    continue
                for group in quota.groups:
                    level = group_level(group.remaining_percent, settings.warn_percent, settings.critical_percent)
                    key = self._entry_key(name, group)
                    active_keys.add(key)
                    if level == "ok":
                        self._seen.pop(key, None)
                        continue
                    alert = self._build_alert(name, label, group, level)
                    current.append(alert)
                    prev = self._seen.get(key) or {}
                    prev_rank = LEVEL_RANK.get(str(prev.get("level") or "ok"), 0)
                    rank = LEVEL_RANK[level]
                    reset_changed = bool(prev) and prev.get("reset_at") != group.reset_at
                    if rank > prev_rank or (prev_rank >= 1 and reset_changed) or (prev_rank >= 1 and not prev.get("notified_at")):
                        to_notify.append(alert)
                        self._seen[key] = {
                            "level": level,
                            "rank": rank,
                            "remaining": group.remaining_percent,
                            "reset_at": group.reset_at,
                            "notified_at": prev.get("notified_at"),
                        }
                    else:
                        # Seviye aynı kaldı; reset/remaining bilgisini tazele.
                        self._seen[key] = {
                            **prev,
                            "level": level,
                            "rank": rank,
                            "remaining": group.remaining_percent,
                            "reset_at": group.reset_at,
                        }
            # Artık var olmayan profillerin kayıtlarını temizle.
            live_profiles = {name for name, _label, _quota in accounts}
            for key in list(self._seen):
                if key.split("|", 1)[0] not in live_profiles:
                    self._seen.pop(key, None)
            self.save()

            sent: list[Alert] = []
            if notify and to_notify and self.notifier_enabled():
                by_account: dict[str, list[Alert]] = {}
                for alert in to_notify:
                    by_account.setdefault(alert.account, []).append(alert)
                for account, alerts in by_account.items():
                    label = alerts[0].label
                    urgency = "critical" if any(a.level in {"critical", "empty"} for a in alerts) else "normal"
                    lines = []
                    for a in alerts:
                        lines.append(f"• {a.group_name}: {human_percent(a.remaining)} — {_reset_text(a.reset_at)}")
                    body = "\n".join(lines)
                    ok = self.notifier.send(
                        f"Antigravity kotası: {label}",
                        body,
                        urgency=urgency,
                    )
                    if ok:
                        for a in alerts:
                            a.notified = True
                            sent.append(a)
                        for a in alerts:
                            key = f"{a.account}|{a.group_id}"
                            if key in self._seen:
                                self._seen[key]["notified_at"] = now
                        self.save()
            current.sort(key=lambda a: (-LEVEL_RANK.get(a.level, 0), a.remaining, a.account))
            return current, sent
