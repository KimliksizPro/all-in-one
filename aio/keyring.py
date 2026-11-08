"""Antigravity oturumunun Secret Service (KDE Wallet / GNOME Keyring) erişimi.

Antigravity, OAuth token'ını `service=gemini`, `username=antigravity`
nitelikleriyle OS keyring'ine yazar. Linux'ta bu, libsecret / Secret Service
üzerinden `secret-tool` ile okunup yazılabilir.

Profil token'ları canlı kaydın üzerine yazılmaz; her hesap ayrı bir
`service=aio-antigravity`, `username=<profil>` kaydında saklanır. Geçiş
sırasında yalnızca canlı Antigravity kaydı değiştirilir.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .util import parse_iso, redact, utc_now

LIVE_SERVICE = "gemini"
LIVE_USERNAME = "antigravity"
LIVE_LABEL = "Password for 'antigravity' on 'gemini'"

VAULT_SERVICE = "aio-antigravity"
DEFAULT_TIMEOUT = 20.0

# Canlı Antigravity token'ı ile profil kasası arasındaki tüm okuma/yazma
# işlemleri bu kilit ile serileştirilir. Özellikle toplu kota taramasında
# keyring geçici olarak başka bir profile yazılırken UI'nin ara durumu
# "aktif hesap" sanmasını engeller.
KEYRING_LOCK = threading.RLock()


class KeyringError(RuntimeError):
    """Secret Service erişimi başarısız."""


class TokenFormatError(ValueError):
    """Keyring kaydı Antigravity OAuth token formatında değil."""


@dataclass
class TokenBundle:
    raw: str
    data: dict[str, Any]
    token: dict[str, Any]
    email: str | None
    access_token: str | None
    refresh_token: str | None
    expiry: datetime | None
    writer_id: str | None
    auth_method: str | None
    account_id: str | None = None

    def is_expired(self, now: datetime | None = None, skew_seconds: int = 30) -> bool | None:
        if self.expiry is None:
            return None
        now = now or utc_now()
        return self.expiry <= now + timedelta(seconds=skew_seconds)

    def public_summary(self) -> dict[str, Any]:
        """Token içermeyen, UI/CLI için güvenli özet."""
        return {
            "email": self.email,
            "expiry": self.expiry.isoformat(timespec="seconds") if self.expiry else None,
            "expired": self.is_expired(),
            "has_refresh_token": bool(self.refresh_token),
            "auth_method": self.auth_method,
            "writer_id_present": bool(self.writer_id),
        }


def _clean_secret(raw: str) -> str:
    # secret-tool çıktısının sonuna newline ekler; saklarken tek newline'ı at.
    if raw.endswith("\r\n"):
        return raw[:-2]
    if raw.endswith("\n"):
        return raw[:-1]
    return raw


def _decode_token(raw: str, *, resolve_email: bool = False) -> TokenBundle:
    text = _clean_secret(raw)
    if not text.strip():
        raise TokenFormatError("Keyring kaydı boş.")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise TokenFormatError("Keyring kaydı geçerli JSON değil.") from exc
    if not isinstance(data, dict):
        raise TokenFormatError("Keyring kaydı JSON nesnesi değil.")

    token = data.get("token")
    if not isinstance(token, dict):
        # Bazı sürümler token alanlarını doğrudan kökte tutabilir.
        token = data if data.get("access_token") else {}
    if not isinstance(token, dict) or not token.get("access_token"):
        raise TokenFormatError("Keyring kaydında access_token bulunamadı.")

    email = data.get("id_token_email")
    if not isinstance(email, str) or not email:
        email = None
        id_token = token.get("id_token")
        if isinstance(id_token, str) and id_token.count(".") == 2:
            import base64

            try:
                p = id_token.split(".")[1]
                p += "=" * (-len(p) % 4)
                claims = json.loads(base64.urlsafe_b64decode(p.encode("ascii")))
                if isinstance(claims, dict) and isinstance(claims.get("email"), str):
                    email = claims["email"]
            except Exception:
                pass
    if not email and resolve_email:
        email = tokeninfo_email(token.get("access_token"))

    expiry = parse_iso(token.get("expiry"))
    if expiry is None and token.get("expiry_date"):
        try:
            # Gemini CLI formatı: milisaniye epoch
            val = float(token["expiry_date"])
            expiry = datetime.fromtimestamp(val / 1000.0, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass

    access = token.get("access_token")
    refresh = token.get("refresh_token")
    return TokenBundle(
        raw=text,
        data=data,
        token=token,
        email=email,
        access_token=access if isinstance(access, str) else None,
        refresh_token=refresh if isinstance(refresh, str) else None,
        expiry=expiry,
        writer_id=data.get("writer_id") if isinstance(data.get("writer_id"), str) else None,
        auth_method=data.get("auth_method") if isinstance(data.get("auth_method"), str) else None,
        account_id=data.get("account_id") if isinstance(data.get("account_id"), str) else None,
    )


def tokeninfo_email(access_token: str | None) -> str | None:
    """Access token'dan e-posta öğren (keyring kaydında yoksa)."""
    if not access_token:
        return None
    url = "https://oauth2.googleapis.com/tokeninfo?access_token=" + urllib.parse.quote(access_token)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        email = data.get("email")
        return email if isinstance(email, str) and email else None
    except (urllib.error.URLError, ValueError, OSError):
        return None


class SecretService:
    """`secret-tool` CLI üzerinden Secret Service erişimi."""

    def __init__(self, command: str | None = None, timeout: float = DEFAULT_TIMEOUT):
        self.command = command or os.environ.get("AIO_SECRET_TOOL") or "secret-tool"
        self.timeout = timeout

    def available(self) -> bool:
        return bool(shutil.which(self.command))

    def _run(self, args: list[str], *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
        if not self.available():
            raise KeyringError(
                "`secret-tool` bulunamadı. KDE Wallet / GNOME Keyring (libsecret) kurulu olmalı."
            )
        try:
            return subprocess.run(
                [self.command, *args],
                input=input_bytes,
                capture_output=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise KeyringError(
                "Keyring yanıt vermedi (kilitli olabilir). KDE Wallet / GNOME Keyring'i açıp tekrar deneyin."
            ) from exc
        except OSError as exc:
            raise KeyringError(f"Keyring komutu çalıştırılamadı: {redact(exc)}") from exc

    def get(self, service: str, username: str) -> str | None:
        proc = self._run(["lookup", "service", service, "username", username])
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            # secret-tool bulunamadığında sessizce 1 döner.
            if not err:
                return None
            raise KeyringError(f"Keyring okunamadı: {redact(err)}")
        raw = proc.stdout.decode("utf-8", "replace")
        raw = _clean_secret(raw)
        return raw or None

    def set(self, service: str, username: str, secret: str, *, label: str | None = None) -> None:
        secret = _clean_secret(secret)
        label = label or f"all-in-one: {username}"
        proc = self._run(
            ["store", "--label", label, "service", service, "username", username],
            input_bytes=secret.encode("utf-8"),
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise KeyringError(f"Keyring'e yazılamadı: {redact(err)}")
        current = self.get(service, username)
        if current != secret:
            # Eski kayıt aynı niteliklerle kalmış olabilir; temizleyip bir kez daha dene.
            self.clear(service, username)
            proc = self._run(
                ["store", "--label", label, "service", service, "username", username],
                input_bytes=secret.encode("utf-8"),
            )
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", "replace").strip()
                raise KeyringError(f"Keyring'e yazılamadı: {redact(err)}")
            current = self.get(service, username)
            if current != secret:
                raise KeyringError("Keyring yazımı doğrulanamadı.")

    def clear(self, service: str, username: str) -> None:
        self._run(["clear", "service", service, "username", username])
        # Kayıt yoksa da başarı say.

    # ---- canlı Antigravity oturumu ----

    def read_live_raw(self) -> str | None:
        return self.get(LIVE_SERVICE, LIVE_USERNAME)

    def write_live_raw(self, raw: str) -> None:
        self.set(LIVE_SERVICE, LIVE_USERNAME, raw, label=LIVE_LABEL)

    def read_live(self, *, resolve_email: bool = False) -> TokenBundle | None:
        raw = self.read_live_raw()
        if not raw:
            return None
        return _decode_token(raw, resolve_email=resolve_email)

    # ---- profil kasası ----

    def read_vault_raw(self, profile: str) -> str | None:
        return self.get(VAULT_SERVICE, profile)

    def write_vault_raw(self, profile: str, raw: str) -> None:
        self.set(VAULT_SERVICE, profile, raw, label=f"all-in-one: {profile}")

    def delete_vault_raw(self, profile: str) -> None:
        self.clear(VAULT_SERVICE, profile)

    def read_vault(self, profile: str, *, resolve_email: bool = False) -> TokenBundle | None:
        raw = self.read_vault_raw(profile)
        if not raw:
            return None
        return _decode_token(raw, resolve_email=resolve_email)


def decode_token(raw: str, *, resolve_email: bool = False) -> TokenBundle:
    return _decode_token(raw, resolve_email=resolve_email)
