"""Humble Bundle: signing in (in a window of the app, on Humble's own pages) and keeping that
sign-in, readable only by you, in humble.json in the data folder; and reading the library for its
revealed Steam keys, which go to the Keys queue unless you own the game already. Mixed into Farm.

Humble's site (Next.js, 2026) has no library API: the library page asks a server action for 5
items at a time, and requests made outside that page get only the first 5 back whatever they ask.
So the app's window opens the page and lets it read itself (app_webview.read_library)."""
import json
import os
import re
import time

from . import files, http, steam

SITE = "https://www.humblebundle.com"
SIGN_IN = f"{SITE}/home/library"  # signed out, Humble sends this on to its sign-in pages and back
LIBRARY = f"{SITE}/home/library"
# words a key's title may carry that the Steam library name doesn't (or the other way round)
EDITION = re.compile(r"\b(game of the year|goty|definitive|complete|deluxe|enhanced|ultimate|gold|digital|anniversary"
                     r"|remastered|director'?s cut|special|standard|collector'?s|premium|legendary|steam)\b( edition)?|\bedition\b")


def steam_keys(items):
    """(key, title) for every revealed Steam key among library items."""
    out = []
    for it in items:
        for k in ((it.get("entitlements") or {}).get("keys")) or []:
            key = (k.get("redemptionData") or {}).get("keyFormatted")
            if k.get("keyIntegrationId") == "steam" and key and not k.get("expired"):
                out.append((key, k.get("title") or it.get("title") or ""))
    return out


def title_key(name):
    """A game name as compared across Humble and Steam: no ™/®, edition words, punctuation or case."""
    name = re.sub(r"[™®©]", "", (name or "").lower()).replace("&", " and ")
    name = EDITION.sub(" ", name)
    return re.sub(r"[^a-z0-9]+", "", name)


def owned(title, owned_keys):
    """Whether a Humble title is a game the account has. Only a confident match counts: anything
    unsure goes to the queue, where Steam itself says "you own this" for nothing."""
    t = title_key(title)
    return bool(t) and t in owned_keys


def signed_in_as(cookies):
    """What Humble's session check says for these cookies: None when they're signed out."""
    status, body = http.request(f"{SITE}/api/auth/session-status", cookies=cookies)
    try:
        d = json.loads(body) if status == 200 else {}
    except ValueError:
        d = {}
    return d if d.get("status") and d["status"] != "anonymous" else None


class Humble:
    open_humble = None  # set by the window: opens Humble's sign-in, calls back with its cookies
    read_humble_library = None  # set by the window: (url, progress) -> every library item

    def _humble(self):
        if getattr(self, "_humble_box", None) is None:
            saved = files.read_json(self._path("humble.json"), {}, say=self.note)
            self._humble_box = saved if isinstance(saved, dict) else {}
        return self._humble_box

    def humble_sign_in(self):
        if not self.open_humble:
            raise ValueError("this window can't open Humble's sign-in")
        self.open_humble(SIGN_IN, self.humble_cookies)

    def humble_cookies(self, cookies):
        """From the sign-in window, every little while: True (close the window) once these cookies
        are signed in to Humble."""
        who = signed_in_as(cookies)
        if not who:
            return False
        box = {"cookies": cookies, "who": who, "at": time.time()}
        files.write_json(self._path("humble.json"), box, private=True)
        self._humble_box = box
        self.log("Signed in to Humble Bundle")
        return True

    def humble_sign_out(self):
        self._humble_box = {}
        try:
            os.remove(self._path("humble.json"))
        except OSError:
            pass
        self.log("Signed out of Humble Bundle")

    def humble_scan(self):
        if not self._humble().get("cookies"):
            raise ValueError("sign in to Humble first")
        self._in_background("humble", self._humble_scan)
        return {"started": True}

    def _humble_scan(self):
        box = self._humble()
        try:
            if not signed_in_as(box["cookies"]):
                raise ValueError("Humble signed this app out - sign in to Humble again")
            if not self.read_humble_library:
                raise ValueError("the scan needs the app's window (Humble's own library page does the reading)")

            def progress(page, pages):
                self.progress["humble"] = f"Reading your Humble library: page {page} of {pages}"
            self.progress["humble"] = "Opening your Humble library"
            items = self.read_humble_library(LIBRARY, progress)
            ids = [it.get("id") for it in items]
            if len(set(ids)) < len(ids) * 0.9:
                raise ValueError("Humble showed the same items on every page - the scan can't read the library")
            keys = steam_keys(items)
            self.progress["humble"] = "Checking which of its games you have on Steam"
            mine = {title_key(n) for n in steam.owned_names(self.session).values()} - {""}
            todo = [(k, t) for k, t in keys if not owned(t, mine)]
            queued = self.queue_keys(todo, "humble")
            unrevealed = sum(1 for it in items for k in ((it.get("entitlements") or {}).get("keys")) or []
                             if k.get("keyIntegrationId") == "steam" and not (k.get("redemptionData") or {}).get("keyFormatted")
                             and not k.get("expired"))
            result = {"at": time.time(), "items": len(items), "steam_keys": len(keys), "owned": len(keys) - len(todo),
                      "queued": queued, "listed_before": len(todo) - queued, "unrevealed": unrevealed}
            box["last_scan"] = result
            files.write_json(self._path("humble.json"), box, private=True)
            self.log(f"Humble library: {len(keys)} revealed Steam keys, {result['owned']} for games you have, "
                     f"{queued} new to redeem" + (f"; {unrevealed} not revealed yet" if unrevealed else ""))
        except Exception as e:
            box["last_scan"] = {"at": time.time(), "error": str(e)}
            self.log(f"Couldn't read your Humble library: {e}", "warn")
        finally:
            self.progress.pop("humble", None)

    def humble_snapshot(self):
        box = self._humble()
        who = box.get("who") or {}
        name = next((who[k] for k in ("email", "name", "username") if isinstance(who.get(k), str)), None)
        return {"signed_in": bool(box.get("cookies")), "who": name, "at": box.get("at", 0), "last_scan": box.get("last_scan")}
