"""Antigravity'nin public OAuth istemcisiyle doğrudan hesap ekleme.

Antigravity'nin language_server callback sunucusu uygulama kapanınca ölüyor.
Bu modül kendi localhost callback sunucusunu çalıştırır; Google'dan dönen
authorization code'u token'a çevirir ve Antigravity keyring formatında profile
yazar. Mevcut canlı hesap değişmez.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import httpx

from . import storage

AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


SCOPES = " ".join(
    [
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/cloud-platform",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/cclog",
        "https://www.googleapis.com/auth/experimentsandconfigs",
        "https://www.googleapis.com/auth/aicode",
    ]
)


class OAuthError(RuntimeError):
    pass


def _oauth_client() -> tuple[str, str]:
    try:
        data = json.loads((storage.root_dir() / "oauth-client.local.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise OAuthError(
            "OAuth istemcisi eksik; veri dizininde oauth-client.local.json oluşturun "
            "ve client_id/client_secret alanlarını tanımlayın."
        ) from None
    except (OSError, ValueError):
        raise OAuthError("oauth-client.local.json okunamadı veya geçerli JSON değil.") from None
    if not isinstance(data, dict):
        raise OAuthError("oauth-client.local.json bir JSON nesnesi olmalı.")
    client_id = data.get("client_id")
    client_secret = data.get("client_secret")
    if not isinstance(client_id, str) or not client_id.strip():
        raise OAuthError("oauth-client.local.json içinde client_id eksik veya geçersiz.")
    if not isinstance(client_secret, str) or not client_secret.strip():
        raise OAuthError("oauth-client.local.json içinde client_secret eksik veya geçersiz.")
    return client_id.strip(), client_secret.strip()


@dataclass
class OAuthResult:
    email: str
    raw_secret: str
    access_token: str
    refresh_token: str
    expires_in: int


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_secret(*, access_token: str, refresh_token: str, expires_in: int,
                 email: str, now: datetime | None = None) -> str:
    """Antigravity keyring secret formatını üretir."""
    import json

    now = now or datetime.now(timezone.utc)
    expiry = now + timedelta(seconds=max(1, int(expires_in)))
    payload = {
        "token": {
            "access_token": access_token,
            "token_type": "Bearer",
            "refresh_token": refresh_token,
            "expiry": expiry.isoformat(),
            "expires_in": int(expires_in),
        },
        "auth_method": "consumer",
        "id_token_email": email,
        "writer_id": secrets.token_urlsafe(16),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _decode_id_token(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        import json

        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class _CallbackState:
    def __init__(self, expected_state: str):
        self.expected_state = expected_state
        self.done = threading.Event()
        self.code: str | None = None
        self.error: str | None = None
        self.result: OAuthResult | None = None
        self.exchange_error: str | None = None


class GoogleOAuthFlow:
    def __init__(self, *, timeout: float = 300.0, opener: Callable[[str], object] | None = None):
        self._client_id, self._client_secret = _oauth_client()
        self.timeout = timeout
        self.opener = opener or webbrowser.open
        self._verifier, self._challenge = _pkce_pair()
        self._state = secrets.token_urlsafe(24)
        self._callback = _CallbackState(self._state)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.auth_url: str | None = None
        self.redirect_uri: str | None = None

    def start(self) -> str:
        flow = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return
                if parsed.path != "/auth/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                params = urllib.parse.parse_qs(parsed.query)
                state = (params.get("state") or [""])[0]
                if state != flow._callback.expected_state:
                    flow._callback.error = "OAuth state doğrulanamadı."
                    flow._callback.done.set()
                    self._respond(400, False, "Güvenlik doğrulaması başarısız. Girişi tekrar başlatın.")
                    return
                error = (params.get("error") or [""])[0]
                if error:
                    flow._callback.error = f"Google girişi iptal edildi: {error}"
                    flow._callback.done.set()
                    self._respond(400, False, "Giriş iptal edildi. Bu sekmeyi kapatabilirsiniz.")
                    return
                code = (params.get("code") or [""])[0]
                if not code:
                    flow._callback.error = "Google callback'inde authorization code yok."
                    flow._callback.done.set()
                    self._respond(400, False, "Authorization code alınamadı.")
                    return
                try:
                    result = flow._exchange(code)
                    flow._callback.result = result
                    self._respond(200, True, f"Giriş tamamlandı: {result.email}")
                except OAuthError as exc:
                    flow._callback.exchange_error = str(exc)
                    self._respond(400, False, "Token alınamadı. Terminal/UI durumunu kontrol edin.")
                finally:
                    flow._callback.done.set()
                    if flow._server is not None:
                        threading.Timer(0.3, flow._server.shutdown).start()

            def _send_html(self, status: int, text: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                body = f"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
                <meta name="viewport" content="width=device-width,initial-scale=1">
                <title>all-in-one · giriş</title>
                <style>
                :root{{color-scheme:dark}}body{{margin:0;min-height:100vh;display:grid;place-items:center;
                background:#0b111a;color:#e9eff8;font-family:system-ui,sans-serif}}
                .card{{max-width:520px;padding:28px 30px;border:1px solid #26364b;border-radius:18px;
                background:#111a25;box-shadow:0 20px 60px rgba(0,0,0,.3)}}
                h1{{font-size:1.2rem;margin:0 0 8px}}p{{color:#93a5bd;line-height:1.6;margin:0}}
                .ok{{color:#4ade80}}.bad{{color:#fb7185}}
                </style></head><body><div class="card"><h1 class="{'ok' if text.startswith('Giriş tamamlandı') else 'bad'}">
                {'✓ ' if text.startswith('Giriş tamamlandı') else '! '}all-in-one</h1><p>{html.escape(text)}</p>
                <p style="margin-top:12px">Bu sekmeyi kapatabilirsiniz.</p></div></body></html>"""
                try:
                    self.wfile.write(body.encode("utf-8"))
                except OSError:
                    pass

            def _respond(self, status: int, _ok: bool, text: str) -> None:
                self._send_html(status, text)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = int(self._server.server_address[1])
        self.redirect_uri = f"http://127.0.0.1:{port}/auth/callback"
        self.auth_url = AUTH_URL + "?" + urllib.parse.urlencode(
            {
                "access_type": "offline",
                "client_id": self._client_id,
                "code_challenge": self._challenge,
                "code_challenge_method": "S256",
                "prompt": "consent",
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": SCOPES,
                "state": self._state,
                "include_granted_scopes": "true",
            }
        )
        self._thread = threading.Thread(target=self._server.serve_forever, name="aio-oauth", daemon=True)
        self._thread.start()
        try:
            self.opener(self.auth_url)
        except Exception:
            pass
        return self.auth_url

    def _exchange(self, code: str) -> OAuthResult:
        try:
            resp = httpx.post(
                TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                    "code_verifier": self._verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": self.redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise OAuthError(f"Google token isteği başarısız: {exc}") from exc
        if resp.status_code != 200:
            raise OAuthError(f"Google token HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise OAuthError("Google token yanıtı geçersiz JSON.") from exc
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        if not isinstance(access, str) or not access:
            raise OAuthError("Google access_token döndürmedi.")
        if not isinstance(refresh, str) or not refresh:
            raise OAuthError("Google refresh_token döndürmedi; girişi tekrar deneyin.")
        email = None
        id_token = data.get("id_token")
        if isinstance(id_token, str):
            claims = _decode_id_token(id_token)
            if isinstance(claims.get("email"), str):
                email = claims["email"]
        if not email:
            try:
                info = httpx.get(
                    USERINFO_URL,
                    headers={"Authorization": f"Bearer {access}"},
                    timeout=15.0,
                )
                if info.status_code == 200:
                    payload = info.json()
                    if isinstance(payload.get("email"), str):
                        email = payload["email"]
            except (httpx.HTTPError, ValueError):
                pass
        if not email:
            raise OAuthError("Google hesabının e-postası alınamadı.")
        try:
            expires_in = int(data.get("expires_in") or 3599)
        except (TypeError, ValueError):
            expires_in = 3599
        raw = build_secret(
            access_token=access,
            refresh_token=refresh,
            expires_in=expires_in,
            email=email,
        )
        return OAuthResult(
            email=email,
            raw_secret=raw,
            access_token=access,
            refresh_token=refresh,
            expires_in=expires_in,
        )

    def wait(self) -> OAuthResult:
        if not self._callback.done.wait(self.timeout):
            raise OAuthError("Giriş zaman aşımına uğradı.")
        if self._callback.error:
            raise OAuthError(self._callback.error)
        if self._callback.exchange_error:
            raise OAuthError(self._callback.exchange_error)
        if not self._callback.result:
            raise OAuthError("OAuth sonucu alınamadı.")
        return self._callback.result

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
            try:
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None
