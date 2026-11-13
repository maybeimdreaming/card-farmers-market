"""Steam sign-in by QR code (approved in the Steam mobile app), so Card Farm never sees a password.

The refresh token lasts months; access tokens last about a day and are renewed from it.
"""
import base64
import json
import os
import secrets
import threading
import time
import urllib.parse

from . import files
from .http import post_for_cookies, post_json

API = "https://api.steampowered.com/IAuthenticationService"
LOGIN = "https://login.steampowered.com"
COMMUNITY = "https://steamcommunity.com"
STORE = "https://store.steampowered.com"


def _claims(jwt):
    part = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


NO_RENEWAL = "wouldn't renew the sign-in"  # in the error when Steam refuses a new access token

class Session:
    def __init__(self, path):
        self.path = path
        self.steamid = self.account = self.refresh_token = self.access_token = None
        self.store_access = None  # the store signs in with a token of its own (the Discovery Queue)
        self.sessionid = secrets.token_hex(12)
        self.qr = None  # pending sign-in: {client_id, request_id, url, interval, expires}
        self._renewing = threading.Lock()  # the farm, market lookups and background jobs all ask for cookies
        saved = files.read_json(path)  # a damaged one: signed out, sign in again
        if isinstance(saved, dict):
            for k in ("steamid", "account", "refresh_token", "access_token", "store_access", "sessionid"):
                setattr(self, k, saved.get(k) or getattr(self, k))

    @property
    def signed_in(self):
        return bool(self.refresh_token)

    def save(self):
        files.write_json(self.path, {k: getattr(self, k) for k in
                                     ("steamid", "account", "refresh_token", "access_token", "store_access", "sessionid")}, private=True)

    def sign_out(self):
        self.steamid = self.account = self.refresh_token = self.access_token = self.store_access = None
        self.qr = None
        if os.path.exists(self.path):
            os.remove(self.path)

    # --- QR sign-in -------------------------------------------------------------------
    def start_qr(self):
        r = post_json(f"{API}/BeginAuthSessionViaQR/v1/",
                      {"device_friendly_name": "Card Farmer's Market", "platform_type": 2, "website_id": "Community"})["response"]
        self.qr = {"client_id": r["client_id"], "request_id": r["request_id"], "url": r["challenge_url"],
                   "interval": float(r.get("interval", 5)), "expires": time.time() + 600}
        return self.qr

    def poll_qr(self):
        """True once approved on the phone. Steam rotates the code now and then; follow it."""
        if not self.qr:
            return False
        if time.time() > self.qr["expires"]:
            self.qr = None
            return False
        r = post_json(f"{API}/PollAuthSessionStatus/v1/",
                      {"client_id": self.qr["client_id"], "request_id": self.qr["request_id"]}).get("response", {})
        if r.get("new_challenge_url"):
            self.qr["url"] = r["new_challenge_url"]
            self.qr["client_id"] = r.get("new_client_id", self.qr["client_id"])
        if not r.get("refresh_token"):
            return False
        self.refresh_token, self.access_token = r["refresh_token"], r.get("access_token")
        self.account = r.get("account_name")
        self.steamid = _claims(self.refresh_token)["sub"]
        self.qr = None
        if not self.access_token:
            self.renew()
        self.save()
        return True

    # --- tokens -----------------------------------------------------------------------
    def renew(self):
        """A new access token from the refresh token. Steam won't renew a web sign-in (the kind the
        QR code makes) through its API, so that one is renewed the way a browser does it: the
        refresh token buys a one-time pass at the login site, which steamcommunity.com trades
        for a new steamLoginSecure cookie (holding the access token)."""
        r = post_json(f"{API}/GenerateAccessTokenForApp/v1/",
                      {"refresh_token": self.refresh_token, "steamid": self.steamid}).get("response", {})
        token = r.get("access_token") or self._renew_like_a_browser()
        if not token:
            raise RuntimeError(f"Steam {NO_RENEWAL}; sign in again")
        self.access_token = token
        self.refresh_token = r.get("refresh_token") or self.refresh_token
        self.save()

    def _renew_like_a_browser(self, site=COMMUNITY):
        """The sign-in cookie's token for one Steam site (each has its own)."""
        r = post_json(f"{LOGIN}/jwt/finalizelogin",
                      {"nonce": self.refresh_token, "sessionid": self.sessionid, "redir": f"{COMMUNITY}/login/home/?goto="},
                      headers={"Origin": COMMUNITY, "Referer": f"{COMMUNITY}/"})
        hop = next((t for t in r.get("transfer_info", []) if t.get("url", "").startswith(f"{site}/")), None)
        if not hop:
            return None
        _, _, set_cookies = post_for_cookies(hop["url"], dict(hop["params"], steamID=r.get("steamID", self.steamid)))
        value = urllib.parse.unquote(set_cookies.get("steamLoginSecure", ""))
        steamid, _, token = value.partition("||")
        return token if token and steamid == self.steamid else None

    def _stale(self, token=None):
        token = self.access_token if token is None else token
        return not token or _claims(token).get("exp", 0) < time.time() + 3600

    def store_token(self):
        """A token the Steam store takes (its sign-in cookie and the sale's sticker), renewed like
        the community's when it's due."""
        if self._stale(self.store_access or ""):
            with self._renewing:
                if self._stale(self.store_access or ""):
                    token = self._renew_like_a_browser(STORE)
                    if not token:
                        raise RuntimeError(f"Steam {NO_RENEWAL} for the store; sign in again")
                    self.store_access = token
                    self.save()
        return self.store_access

    def store_cookies(self):
        return {"steamLoginSecure": f"{self.steamid}%7C%7C{self.store_token()}", "sessionid": self.sessionid,
                "Steam_Language": "english"}

    def cookies(self):
        if self._stale():
            with self._renewing:  # one renewal at a time; whoever waited finds a fresh token
                if self._stale():
                    self.renew()
        return {"steamLoginSecure": f"{self.steamid}%7C%7C{self.access_token}", "sessionid": self.sessionid,
                "Steam_Language": "english"}
