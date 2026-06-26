"""
TikTok OAuth (Login Kit) — one-time authorization + token refresh.

Credentials come from the environment (.env locally, Railway vars in cloud):
    TIKTOK_CLIENT_KEY
    TIKTOK_CLIENT_SECRET
    TIKTOK_REDIRECT_URI     (the verified https redirect, e.g. the GitHub
                             Pages /callback URL)

Authorize once (opens a TikTok consent link, then you paste the redirected
URL back):
    python3 -m upload.tiktok_auth

This saves tiktok_token.json (access + refresh tokens). After that, the
upload code calls get_access_token(), which auto-refreshes when expired.
"""

import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from dotenv import load_dotenv

from agentdrop_common import setup_logging

log = setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = PROJECT_ROOT / "tiktok_token.json"

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"

# Scopes enabled on the app. (video.publish is added later, after audit.)
SCOPES = [
    "user.info.basic",
    "user.info.profile",
    "user.info.stats",
    "video.list",
    "video.upload",
]


def _creds() -> tuple[str, str, str]:
    load_dotenv()
    key = os.getenv("TIKTOK_CLIENT_KEY")
    secret = os.getenv("TIKTOK_CLIENT_SECRET")
    redirect = os.getenv("TIKTOK_REDIRECT_URI")
    if not (key and secret and redirect):
        raise RuntimeError(
            "Missing TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / "
            "TIKTOK_REDIRECT_URI in the environment."
        )
    return key, secret, redirect


def build_authorize_url(state: str = "agentdrop") -> str:
    """The TikTok consent URL the user opens to grant access."""
    key, _, redirect = _creds()
    params = {
        "client_key": key,
        "scope": ",".join(SCOPES),
        "response_type": "code",
        "redirect_uri": redirect,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _save_token(data: dict) -> None:
    import json
    # Stamp an absolute expiry so we know when to refresh.
    data["expires_at"] = int(time.time()) + int(data.get("expires_in", 0))
    TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("Saved TikTok token -> %s", TOKEN_FILE.name)


def exchange_code(code: str) -> dict:
    """Trade the authorization code for access + refresh tokens."""
    key, secret, redirect = _creds()
    resp = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": key,
            "client_secret": secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
        },
        timeout=30,
    )
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"TikTok token exchange failed: {data}")
    _save_token(data)
    return data


def _refresh(refresh_token: str) -> dict:
    key, secret, _ = _creds()
    resp = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": key,
            "client_secret": secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"TikTok token refresh failed: {data}")
    _save_token(data)
    return data


def get_access_token() -> str:
    """Return a valid access token, refreshing it if it's expired/expiring."""
    import json
    if not TOKEN_FILE.exists():
        raise RuntimeError(
            "Not authorized with TikTok yet. Run: python3 -m upload.tiktok_auth"
        )
    token = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    # Refresh a minute early to avoid edge-of-expiry failures.
    if int(time.time()) >= int(token.get("expires_at", 0)) - 60:
        log.info("TikTok access token expired — refreshing.")
        token = _refresh(token["refresh_token"])
    return token["access_token"]


def _extract_code(pasted: str) -> str:
    """Accept either a raw code or the full redirected URL and return the code."""
    pasted = pasted.strip()
    if pasted.startswith("http"):
        qs = parse_qs(urlparse(pasted).query)
        if "code" in qs:
            return qs["code"][0]
    return pasted


if __name__ == "__main__":
    print("\n1) Open this URL in your browser and approve:\n")
    print("   " + build_authorize_url())
    print(
        "\n2) Your browser will redirect to a page that looks broken — that's "
        "fine.\n   Copy the FULL address-bar URL (it contains ?code=...) and "
        "paste it here.\n"
    )
    pasted = input("Paste the redirected URL (or just the code) > ").strip()
    code = _extract_code(pasted)
    exchange_code(code)
    print("\n✅ Authorized! tiktok_token.json saved. You can now post to TikTok.")
