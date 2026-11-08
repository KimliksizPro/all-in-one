"""Antigravity `language_server` RPC'sinden gerçek kota okuma.

Neden doğrudan Cloud Code API değil?
  Antigravity'nin kota/plan bilgisi `GetUserStatus` RPC'sinde, model bazlı
  `quotaInfo.remainingFraction` ve `resetTime` alanlarıyla gelir. Cloud Code
  REST endpoint'i bu hesap türünde 403 SUBSCRIPTION_REQUIRED dönebiliyor.

Probe, uygulamanın kullandığı binary'yi **izole bir HOME** ile headless
çalıştırır; böylece kullanıcının `~/.gemini/config` (skills/MCP/settings)
dizinine dokunmaz. OAuth token'ı yine OS keyring'inden okunur.
"""
from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from . import antigravity
from .util import free_port, make_token, now_iso, parse_iso, redact, utc_now

PROBE_TIMEOUT = 50.0
STARTUP_TIMEOUT = 40.0
DEFAULT_PLAN = "Bilinmiyor"


class ProbeError(RuntimeError):
    pass


@dataclass
class QuotaGroup:
    id: str
    family: str
    name: str
    window: str
    remaining: float | None
    remaining_percent: float | None
    reset_at: str | None
    reset_in_seconds: int | None
    exhausted: bool
    models: list[str]
    model_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "family": self.family,
            "name": self.name,
            "window": self.window,
            "remaining": self.remaining,
            "remaining_percent": self.remaining_percent,
            "reset_at": self.reset_at,
            "reset_in_seconds": self.reset_in_seconds,
            "exhausted": self.exhausted,
            "models": self.models,
            "model_id": self.model_id,
        }


@dataclass
class UserQuota:
    ok: bool
    email: str | None = None
    account_name: str | None = None
    plan_name: str | None = None
    plan_id: str | None = None
    teams_tier: str | None = None
    credits_prompt: int | None = None
    credits_flow: int | None = None
    monthly_prompt: int | None = None
    monthly_flow: int | None = None
    groups: list[QuotaGroup] = field(default_factory=list)
    worst_remaining: float | None = None
    fetched_at: str = field(default_factory=now_iso)
    error: str | None = None
    needs_login: bool = False
    stale: bool = False
    source: str = "live"
    raw_model_count: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "email": self.email,
            "account_name": self.account_name,
            "plan_name": self.plan_name,
            "plan_id": self.plan_id,
            "teams_tier": self.teams_tier,
            "credits_prompt": self.credits_prompt,
            "credits_flow": self.credits_flow,
            "monthly_prompt": self.monthly_prompt,
            "monthly_flow": self.monthly_flow,
            "groups": [g.to_dict() for g in self.groups],
            "worst_remaining": self.worst_remaining,
            "fetched_at": self.fetched_at,
            "error": self.error,
            "needs_login": self.needs_login,
            "stale": self.stale,
            "source": self.source,
            "raw_model_count": self.raw_model_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UserQuota":
        groups: list[QuotaGroup] = []
        for g in data.get("groups") or []:
            if not isinstance(g, dict):
                continue
            try:
                remaining = _fraction_or_none(g.get("remaining"))
                groups.append(
                    QuotaGroup(
                        id=str(g.get("id") or ""),
                        family=str(g.get("family") or "other"),
                        name=str(g.get("name") or "Diğer"),
                        window=str(g.get("window") or "bilinmiyor"),
                        remaining=remaining,
                        remaining_percent=remaining * 100.0 if remaining is not None else None,
                        reset_at=g.get("reset_at"),
                        reset_in_seconds=int(g["reset_in_seconds"]) if g.get("reset_in_seconds") is not None else None,
                        exhausted=remaining == 0.0,
                        models=[str(x) for x in (g.get("models") or [])],
                        model_id=g.get("model_id") if isinstance(g.get("model_id"), str) else None,
                    )
                )
            except (TypeError, ValueError):
                continue
        return cls(
            ok=bool(data.get("ok")),
            email=data.get("email"),
            account_name=data.get("account_name"),
            plan_name=data.get("plan_name"),
            plan_id=data.get("plan_id"),
            teams_tier=data.get("teams_tier"),
            credits_prompt=_int_or_none(data.get("credits_prompt")),
            credits_flow=_int_or_none(data.get("credits_flow")),
            monthly_prompt=_int_or_none(data.get("monthly_prompt")),
            monthly_flow=_int_or_none(data.get("monthly_flow")),
            groups=groups,
            worst_remaining=min((g.remaining for g in groups if g.remaining is not None), default=None),
            fetched_at=str(data.get("fetched_at") or now_iso()),
            error=data.get("error"),
            needs_login=bool(data.get("needs_login")),
            stale=bool(data.get("stale")),
            source=str(data.get("source") or "cache"),
            raw_model_count=int(data.get("raw_model_count") or 0),
        )


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _fraction_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        fraction = float(value)
    except (ValueError, OverflowError):
        return None
    return fraction if math.isfinite(fraction) and 0.0 <= fraction <= 1.0 else None


def _model_identity(model: dict) -> str | None:
    model_or_alias = model.get("modelOrAlias")
    if not isinstance(model_or_alias, dict):
        model_or_alias = {}
    for value in (
        model.get("modelId"), model_or_alias.get("model"), model_or_alias.get("alias"),
        model.get("model"), model.get("label"),
    ):
        if isinstance(value, str) and value:
            return value
    return None


def _family(model_id: str, label: str) -> str:
    text = f"{model_id} {label}".lower()
    for family in ("argon", "gemini", "claude", "gpt"):
        if family in text:
            return family
    return "other"


def _parse_reset(value: Any) -> datetime | None:
    return parse_iso(value)


def parse_user_status(data: dict, *, now: datetime | None = None, source: str = "live") -> UserQuota:
    now = now or utc_now()
    if not isinstance(data, dict):
        return UserQuota(ok=False, error="RPC yanıtı JSON nesnesi değil.", fetched_at=now_iso(), source=source)
    us = data.get("userStatus")
    if not isinstance(us, dict):
        return UserQuota(ok=False, error="RPC yanıtında userStatus yok.", fetched_at=now_iso(), source=source)

    email = us.get("email") if isinstance(us.get("email"), str) else None
    account_name = us.get("name") if isinstance(us.get("name"), str) else None
    plan_status = us.get("planStatus") if isinstance(us.get("planStatus"), dict) else {}
    plan_info = plan_status.get("planInfo") if isinstance(plan_status.get("planInfo"), dict) else {}
    user_tier = us.get("userTier") if isinstance(us.get("userTier"), dict) else {}
    cmcd = us.get("cascadeModelConfigData") if isinstance(us.get("cascadeModelConfigData"), dict) else {}
    configs = cmcd.get("clientModelConfigs") if isinstance(cmcd.get("clientModelConfigs"), list) else []

    plan_name = plan_info.get("planName") if isinstance(plan_info.get("planName"), str) else None
    if not plan_name and isinstance(user_tier.get("name"), str):
        plan_name = user_tier["name"]
    plan_id = None
    if isinstance(user_tier.get("id"), str):
        plan_id = user_tier["id"]
    elif isinstance(plan_info.get("teamsTier"), str):
        plan_id = plan_info["teamsTier"]
    teams_tier = plan_info.get("teamsTier") if isinstance(plan_info.get("teamsTier"), str) else None

    groups: list[QuotaGroup] = []
    identity_counts: dict[str, int] = {}
    for index, model in enumerate(configs):
        if not isinstance(model, dict):
            continue
        quota = model.get("quotaInfo")
        if not isinstance(quota, dict):
            quota = {}
        model_id = _model_identity(model)
        label = model.get("label")
        if not isinstance(label, str) or not label:
            label = model_id or "Kimliği bildirilmemiş model"
        remaining = _fraction_or_none(quota.get("remainingFraction"))
        reset_at = _parse_reset(quota.get("resetTime"))
        base_id = f"model:{model_id}" if model_id else f"unknown:{index}"
        occurrence = identity_counts.get(base_id, 0) + 1
        identity_counts[base_id] = occurrence
        row_id = base_id if occurrence == 1 else f"{base_id}:record:{occurrence}"
        groups.append(
            QuotaGroup(
                id=row_id,
                family=_family(model_id or "", label),
                name=label,
                window="bilinmiyor",
                remaining=remaining,
                remaining_percent=remaining * 100.0 if remaining is not None else None,
                reset_at=reset_at.isoformat() if reset_at else None,
                reset_in_seconds=int(max(0, (reset_at - now).total_seconds())) if reset_at else None,
                exhausted=remaining == 0.0,
                models=[label],
                model_id=model_id,
            )
        )
    # Bilinen en dar kota önce; bilinmeyen değerler en sonda.
    order = {"argon": 0, "gemini": 1, "claude": 2, "gpt": 3, "other": 4}
    groups.sort(key=lambda g: (
        g.remaining is None, g.remaining if g.remaining is not None else 0.0,
        order.get(g.family, 9), g.name,
    ))

    worst = min((g.remaining for g in groups if g.remaining is not None), default=None)
    return UserQuota(
        ok=bool(email),
        email=email,
        account_name=account_name,
        plan_name=plan_name,
        plan_id=plan_id,
        teams_tier=teams_tier,
        credits_prompt=_int_or_none(plan_status.get("availablePromptCredits")),
        credits_flow=_int_or_none(plan_status.get("availableFlowCredits")),
        monthly_prompt=_int_or_none(plan_info.get("monthlyPromptCredits")),
        monthly_flow=_int_or_none(plan_info.get("monthlyFlowCredits")),
        groups=groups,
        worst_remaining=worst,
        fetched_at=now_iso(),
        error=None if email else "RPC yanıtında hesap e-postası yok.",
        source=source,
        raw_model_count=len(groups),
    )


class LanguageServerProbe:
    """İzole HOME ile Antigravity language_server başlatıp kota okur."""

    def __init__(
        self,
        ls_binary: Path | str | None = None,
        *,
        version: str | None = None,
        timeout: float = PROBE_TIMEOUT,
        startup_timeout: float = STARTUP_TIMEOUT,
        env: dict[str, str] | None = None,
    ):
        self.ls_binary = Path(ls_binary) if ls_binary else antigravity.installed_ls_binary()
        self.version = version or antigravity.version()
        self.timeout = timeout
        self.startup_timeout = startup_timeout
        self.extra_env = env or {}

    def _args(self, http_port: int, https_port: int, csrf: str, home: Path) -> list[str]:
        return [
            str(self.ls_binary),
            "--standalone",
            "--override_ide_name",
            "antigravity",
            "--subclient_type",
            "hub",
            "--override_ide_version",
            self.version,
            "--override_user_agent_name",
            "antigravity",
            "--http_server_port",
            str(http_port),
            "--https_server_port",
            str(https_port),
            "--csrf_token",
            csrf,
            "--app_data_dir",
            "antigravity",
            "--gemini_dir",
            str(home / ".gemini"),
            "--api_server_url",
            "https://generativelanguage.googleapis.com",
            "--cloud_code_endpoint",
            "https://daily-cloudcode-pa.googleapis.com",
            "--disable_telemetry",
        ]

    def _env(self, home: Path) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.extra_env)
        env.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_STATE_HOME": str(home / ".local" / "state"),
            }
        )
        return env

    @staticmethod
    def _status_request(base_url: str, csrf: str, timeout: float = 15.0) -> dict | None:
        headers = {
            "X-Codeium-Csrf-Token": csrf,
            "Connect-Protocol-Version": "1",
            "Content-Type": "application/json",
        }
        body = {"metadata": {"ideName": "antigravity", "extensionName": "antigravity", "locale": "en"}}
        try:
            resp = httpx.post(
                base_url + antigravity.USER_STATUS_PATH,
                json=body,
                headers=headers,
                timeout=timeout,
                verify=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and isinstance(data.get("userStatus"), dict):
                    return data
        except (httpx.HTTPError, ValueError, OSError):
            return None
        return None

    def probe(self) -> UserQuota:
        if not self.ls_binary or not Path(self.ls_binary).is_file():
            raise ProbeError("Antigravity language_server binary'si bulunamadı.")

        last_error = ""
        for attempt in range(2):
            home = Path(tempfile.mkdtemp(prefix="aio-probe-"))
            (home / ".gemini" / "antigravity").mkdir(parents=True, exist_ok=True)
            (home / ".config").mkdir(parents=True, exist_ok=True)
            (home / ".cache").mkdir(parents=True, exist_ok=True)
            (home / ".local" / "share").mkdir(parents=True, exist_ok=True)
            (home / ".local" / "state").mkdir(parents=True, exist_ok=True)
            log_path = home / "language_server.log"
            http_port = free_port()
            https_port = free_port()
            while https_port == http_port:
                https_port = free_port()
            csrf = make_token(24)
            proc: subprocess.Popen | None = None
            log_fh = None
            try:
                log_fh = open(log_path, "wb")
                proc = subprocess.Popen(
                    self._args(http_port, https_port, csrf, home),
                    stdin=subprocess.DEVNULL,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    env=self._env(home),
                    cwd=str(home),
                    start_new_session=True,
                    close_fds=True,
                )
                deadline = time.monotonic() + self.startup_timeout
                bases = [f"http://127.0.0.1:{http_port}", f"https://127.0.0.1:{https_port}"]
                data = None
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        break
                    for base in bases:
                        data = self._status_request(base, csrf, timeout=8.0)
                        if data:
                            break
                    if data:
                        break
                    time.sleep(0.35)
                if data:
                    quota = parse_user_status(data, source="probe")
                    if quota.ok:
                        return quota
                    last_error = quota.error or "RPC yanıtı boş."
                else:
                    log = antigravity.log_tail(log_path, limit=25) if log_path.exists() else ""
                    if "address already in use" in log.lower() and attempt == 0:
                        last_error = "Port çakışması."
                        continue
                    if "not logged in" in log.lower() or "unauthenticated" in log.lower():
                        raise ProbeError(
                            "Antigravity oturumu geçersiz. Bu hesap için yeniden giriş yapılıp yakalanması gerekiyor."
                        )
                    last_error = redact(log) or "language_server kota yanıtı vermedi."
            except ProbeError:
                raise
            except OSError as exc:
                last_error = redact(exc)
            finally:
                if proc is not None and proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                    try:
                        proc.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except (ProcessLookupError, PermissionError, OSError):
                            pass
                if log_fh is not None:
                    try:
                        log_fh.close()
                    except OSError:
                        pass
                shutil.rmtree(home, ignore_errors=True)
            if attempt == 0 and last_error == "Port çakışması.":
                continue
            break
        raise ProbeError(last_error or "Kota okunamadı.")
