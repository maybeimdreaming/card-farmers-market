"""Humble Bundle: signing in (in a window of the app, on Humble's own pages) and keeping that
sign-in, readable only by you, in humble.json in the data folder; and reading the library for its
revealed Steam keys, which go to the Keys queue unless you own the game already. Mixed into Farm.

Humble's site (Next.js, 2026) has no library API: the library page asks a server action,
filterUserLibrary, for 5 items at a time. Its id changes whenever Humble ships a new site, so it's
looked up in the page's own scripts and kept until Humble stops knowing it."""
import json
import os
import re
import time
import uuid

from . import files, http, steam

SITE = "https://www.humblebundle.com"
SIGN_IN = f"{SITE}/home/library"  # signed out, Humble sends this on to its sign-in pages and back
LIBRARY = f"{SITE}/home/library"
PAGE_GAP = 1.5          # seconds between library pages: Humble resets connections that come too fast
ACTION_REF = re.compile(r'createServerReference\)?\(\s*"([0-9a-f]{40,})"[^;]{0,200}?"filterUserLibrary"\s*\)')
CHUNK = re.compile(r'(/_next/static/chunks/[A-Za-z0-9_\-./]+\.js)')
# words a key's title may carry that the Steam library name doesn't (or the other way round)
EDITION = re.compile(r"\b(game of the year|goty|definitive|complete|deluxe|enhanced|ultimate|gold|digital|anniversary"
                     r"|remastered|director'?s cut|special|standard|collector'?s|premium|legendary|steam)\b( edition)?|\bedition\b")


class ActionGone(Exception):
    """Humble doesn't know the library action's id any more (it shipped a new site)."""


def find_library_action(cookies, fetch=None):
    """The library action's id, from the library page's scripts."""
    fetch = fetch or (lambda url: http.request(url, cookies=cookies))
    status, page = fetch(LIBRARY)
    if status != 200:
        raise ValueError(f"Humble's library page answered {status}")
    for path in dict.fromkeys(CHUNK.findall(page)):
        status, js = fetch(SITE + path)
        m = ACTION_REF.search(js) if status == 200 else None
        if m:
            return m.group(1)
        time.sleep(0.3)
    raise ValueError("couldn't find Humble's library on its page - Humble may have changed its site")


def library_form(page, limit=5):
    """The library action's arguments as React sends them: (previous state, FormData)."""
    state = {"results": [], "pagination": {"page": 1, "totalPages": 1, "totalDocs": 0}, "success": True}
    fields = {"0": json.dumps([state, "$K1"]), "1_requestId": "1", "1_query": "", "1_sort": "mostRecent",
              "1_cursor": str(limit * page), "1_page": str(page), "1_limit": str(limit),
              "1_hideRevealed": "false", "1_hideExpired": "false", "1_dateExpires": ""}
    boundary = "----cardfarm" + uuid.uuid4().hex
    body = "".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in fields.items())
    return body + f"--{boundary}--\r\n", f"multipart/form-data; boundary={boundary}"


def parse_library_reply(text):
    """The {results, pagination, success} part of the action's reply (React's line format)."""
    for line in (text or "").splitlines():
        m = re.match(r"^[0-9a-f]+:(\{.*\})$", line)
        if m and '"results"' in m.group(1):
            return json.loads(m.group(1))
    return None


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

    def _library_page(self, action, page):
        body, ctype = library_form(page)
        status, text = http.request(LIBRARY, cookies=self._humble()["cookies"], raw=body, headers={
            "Accept": "text/x-component", "Next-Action": action, "Content-Type": ctype, "Origin": SITE, "Referer": LIBRARY})
        d = parse_library_reply(text) if status == 200 else None
        if d is None:
            if status in (200, 404) and ("Server action not found" in text or status == 404 or not text.strip()):
                raise ActionGone()
            raise ValueError(f"Humble's library answered {status}")
        if not d.get("success"):
            raise ValueError(f"Humble's library said no ({d.get('error') or 'no reason'}) - sign in to Humble again?")
        return d

    def _humble_scan(self):
        box, cookies = self._humble(), self._humble()["cookies"]
        try:
            if not signed_in_as(cookies):
                raise ValueError("Humble signed this app out - sign in to Humble again")
            action, items, page, pages = box.get("library_action"), [], 1, 1
            while page <= pages:
                self.progress["humble"] = f"Reading your Humble library: page {page} of {pages}"
                try:
                    if not action:
                        raise ActionGone()
                    d = self._library_page(action, page)
                except ActionGone:
                    if box.get("library_action_fresh"):
                        raise ValueError("Humble's library doesn't answer the way it used to - it may have changed its site")
                    self.progress["humble"] = "Finding Humble's library on its new site"
                    action = find_library_action(cookies)
                    box["library_action"], box["library_action_fresh"] = action, True
                    continue
                got = d.get("results") or []
                # Humble answers this app's requests with its first page whatever page is asked for
                # (2026-11-14) - stop at once rather than read page 1 two hundred times
                if page > 1 and got and items and [r.get("id") for r in got] == [r.get("id") for r in items[:len(got)]]:
                    raise ValueError("Humble only shows this app the first page of your library - the scan can't read it all yet")
                items += got
                pages = max(1, int((d.get("pagination") or {}).get("totalPages") or 1))
                page += 1
                time.sleep(PAGE_GAP)
            box.pop("library_action_fresh", None)
            keys = steam_keys(items)
            self.progress["humble"] = "Checking which of its games you have on Steam"
            mine = {title_key(n) for n in steam.owned_names(self.session).values()} - {""}
            todo = [(k, t) for k, t in keys if not owned(t, mine)]
            queued = self.queue_keys(todo, "humble")
            result = {"at": time.time(), "items": len(items), "steam_keys": len(keys),
                      "owned": len(keys) - len(todo), "queued": queued, "listed_before": len(todo) - queued}
            box["last_scan"] = result
            files.write_json(self._path("humble.json"), box, private=True)
            self.log(f"Humble library: {len(keys)} Steam keys, {result['owned']} for games you have, {queued} new to redeem")
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
