from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone

import httpx
import pytest

from aio.google_oauth import GoogleOAuthFlow, OAuthError, OAuthResult, build_secret
from aio.keyring import decode_token


@pytest.fixture()
def oauth_client(aio_home):
    path = aio_home / "oauth-client.local.json"
    path.write_text(json.dumps({"client_id": "test-client", "client_secret": "test-secret"}), encoding="utf-8")
    return path


def test_build_secret_antigravity_format():
    raw = build_secret(
        access_token="access",
        refresh_token="refresh",
        expires_in=3599,
        email="test@example.com",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    data = json.loads(raw)
    assert data["auth_method"] == "consumer"
    assert data["id_token_email"] == "test@example.com"
    assert data["writer_id"]
    assert data["token"]["access_token"] == "access"
    assert data["token"]["refresh_token"] == "refresh"
    assert data["token"]["expiry"].startswith("2026-01-01T00:59")
    parsed = decode_token(raw)
    assert parsed.email == "test@example.com"


def test_oauth_callback_server_and_state(monkeypatch, oauth_client):
    opened: list[str] = []
    flow = GoogleOAuthFlow(timeout=5, opener=lambda url: opened.append(url))
    result_holder = OAuthResult(
        email="new@example.com",
        raw_secret=build_secret(access_token="a", refresh_token="r", expires_in=100, email="new@example.com"),
        access_token="a",
        refresh_token="r",
        expires_in=100,
    )
    monkeypatch.setattr(flow, "_exchange", lambda code: result_holder)
    auth_url = flow.start()
    try:
        assert opened == [auth_url]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(auth_url).query)
        assert query["redirect_uri"][0].startswith("http://127.0.0.1:")
        state = query["state"][0]
        redirect = query["redirect_uri"][0]
        response = httpx.get(redirect, params={"state": state, "code": "fake"}, timeout=5)
        assert response.status_code == 200
        result = flow.wait()
        assert result.email == "new@example.com"
    finally:
        flow.stop()


def test_oauth_callback_rejects_bad_state(monkeypatch, oauth_client):
    flow = GoogleOAuthFlow(timeout=5, opener=lambda url: None)
    monkeypatch.setattr(flow, "_exchange", lambda code: None)
    auth_url = flow.start()
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(auth_url).query)
        redirect = query["redirect_uri"][0]
        response = httpx.get(redirect, params={"state": "wrong", "code": "fake"}, timeout=5)
        assert response.status_code == 400
        try:
            flow.wait()
        except Exception as exc:
            assert "state" in str(exc).lower()
        else:
            raise AssertionError("bad state accepted")
    finally:
        flow.stop()


def test_missing_oauth_client_does_not_open_browser(aio_home):
    opened = []
    with pytest.raises(OAuthError, match="oauth-client.local.json"):
        GoogleOAuthFlow(opener=opened.append).start()
    assert opened == []


@pytest.mark.parametrize("contents", [
    "not-json",
    "[]",
    json.dumps({"client_id": "test-client"}),
    json.dumps({"client_id": " ", "client_secret": "test-secret"}),
    json.dumps({"client_id": "test-client", "client_secret": 123}),
])
def test_invalid_oauth_client_is_not_exposed(aio_home, contents):
    (aio_home / "oauth-client.local.json").write_text(contents, encoding="utf-8")
    with pytest.raises(OAuthError) as caught:
        GoogleOAuthFlow(opener=lambda url: None)
    assert contents not in str(caught.value)
    assert str(aio_home) not in str(caught.value)
