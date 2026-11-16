"""Redeeming Steam keys in bulk: paste any text (an email, a Humble page, a list) and every Steam
key in it is added to the account, one after another, waiting out Steam's hourly limit. Mixed into
Farm. The keys are kept in keys.json in the data folder, readable only by you."""
import hashlib
import re
import threading
import time

from . import files, humble, steam

KEY = re.compile(r"(?<![A-Z0-9-])[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}(?:-[A-Z0-9]{5}-[A-Z0-9]{5})?(?![A-Z0-9-])")
KEY_ANY_CASE = re.compile(KEY.pattern, re.I)
# what's left of "1. Portal 2 - Steam key:" around the title
LIST_MARK = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
KEY_WORDS = re.compile(r"(?:\b(?:your|steam|product|activation|cd|redeem|redemption|key|code)\b|[\s:#=|–—-])+$", re.I)
GAP = 4               # seconds between keys
LIMIT_WAIT = 3600     # Steam's "too many attempts" lasts about an hour
# Steam's answers (purchase_result_details), as the page shows them
RESULTS = {0: "added", 9: "owned", 13: "region", 14: "invalid", 15: "used", 24: "needs_base", 36: "invalid",
           50: "wallet", 53: "limit"}


def find_keys(text):
    """Every Steam key in a piece of text, in order, each once."""
    return [k for k, _ in find_titled_keys(text)]


def find_titled_keys(text):
    """(key, title or None) for every Steam key in a piece of text, in order, each once. The title is
    what names the key on its line, before it ("Portal 2: XXXXX-XXXXX-XXXXX"), when something does."""
    seen, out = set(), []
    for line in (text or "").splitlines():
        start = 0
        for m in KEY_ANY_CASE.finditer(line):
            k = m.group(0).upper()
            if k not in seen:
                seen.add(k)
                out.append((k, _title(line[start:m.start()])))
            start = m.end()
    return out


def _title(before):
    s = KEY_WORDS.sub("", LIST_MARK.sub("", before)).strip(" \t,;:.|–—-")
    return s if len(re.findall(r"[^\W\d_]", s)) >= 2 and len(s) <= 100 else None


class KeyRedeemer:
    def _keys_path(self):
        return self._path("keys.json")

    def _keys(self):
        """{"list": [{"key", "status": waiting|added|owned|..., "names", "at", "source"}], "paused_until"}"""
        if getattr(self, "_key_box", None) is None:
            saved = files.read_json(self._keys_path(), {}, say=self.note)
            self._key_box = {"list": [], "paused_until": 0, **(saved if isinstance(saved, dict) else {})}
            self._keys_lock = threading.Lock()
        return self._key_box

    def _save_keys(self):
        with self.lock:
            files.write_json(self._keys_path(), self._keys(), private=True)

    def add_keys(self, text, source="pasted"):
        """Queue every key found in `text` that isn't on the list yet; returns how many were new."""
        n = self.queue_keys(find_titled_keys(text), source)
        if not n:
            raise ValueError("no new Steam keys in that text (they look like XXXXX-XXXXX-XXXXX)")
        return {"added": n}

    def queue_keys(self, pairs, source):
        """Queue (key, name or None) pairs not on the list yet - the name shows until Steam names the
        key itself. Returns how many were new (0 is fine: everything was queued before)."""
        box = self._keys()
        with self.lock:
            have = {k["key"] for k in box["list"]}
            new, seen = [], set()
            for key, name in pairs:
                key = (key or "").strip().upper()
                if KEY.fullmatch(key) and key not in have and key not in seen:
                    seen.add(key)
                    new.append({"key": key, "id": _id(key), "status": "waiting", "names": [name] if name else [],
                                "at": 0, "source": source})
            box["list"] += new
        if new:
            self._save_keys()
            self.log(f"{len(new)} Steam key{'s' * (len(new) > 1)} to redeem")
            self._redeem_soon()
        return len(new)

    def drop_waiting(self, keys, source):
        """Take waiting keys off the list - only ones that came from `source`, so a scan never
        removes keys someone pasted. Returns how many went."""
        keys, box = set(keys), self._keys()
        with self.lock:
            before = len(box["list"])
            box["list"] = [k for k in box["list"] if not (k["key"] in keys and k["status"] == "waiting" and k.get("source") == source)]
            gone = before - len(box["list"])
        if gone:
            self._save_keys()
        return gone

    def _redeem_soon(self):
        self._in_background("keys", self._redeem_keys)

    def _owned_titles(self):
        """The account's games by name, so keys titled as one of them aren't spent on Steam's hourly
        limit. None when Steam won't say: then every key is tried."""
        try:
            return humble.OwnedNames(steam.owned_names(self.session).values())
        except Exception as e:
            self.log(f"Couldn't get your games from Steam ({e}); trying every key", "warn")
            return None

    def _redeem_keys(self):
        box = self._keys()
        added, owned, got = 0, False, set()       # owned: read once, when a titled key comes up
        while True:
            if box["paused_until"] > time.time():
                break
            with self.lock:
                k = next((k for k in box["list"] if k["status"] == "waiting"), None)
            if not k:
                break
            title = (k.get("names") or [None])[0]
            if title:
                if owned is False:
                    owned = self._owned_titles()
                if humble.title_key(title) in got or (owned and owned.owns(title)):
                    # an attempt counts toward the limit even when Steam says "owned"; untried, it stays giftable
                    with self.lock:
                        k.update(status="owned", skipped=True, at=time.time())
                    self._save_keys()
                    self.log(f"Skipped the key for {title}: you have it already, so it wasn't tried and stays giftable")
                    continue
            self.progress["keys"] = f"Redeeming {k['key'][:5]}…"
            try:
                r = steam.redeem_key(self.session, k["key"])
            except Exception as e:
                self.log(f"Couldn't redeem a key: {e}; trying again in 5 min", "warn")
                with self.lock:
                    box["paused_until"] = time.time() + 300
                break
            status = RESULTS.get(r["code"], "error")
            with self.lock:
                if status == "limit":
                    box["paused_until"] = time.time() + LIMIT_WAIT
                else:
                    # Steam names what it added or you own; otherwise keep what the key came with (Humble's title)
                    k.update(status=status, names=r["names"] or k.get("names") or [], at=time.time(), code=r["code"])
            self._save_keys()
            if status == "limit":
                self.log("Steam says too many keys were tried for now; the rest are redeemed in an hour", "warn")
                break
            what = ", ".join(r["names"]) or k["key"]
            if status in ("added", "owned"):
                got.update(humble.title_key(n) for n in (r["names"] or []) + ([title] if title else []))
            if status == "added":
                added += 1
                self.log(f"Redeemed {what}", "sell")
                if self._retry_needs_base():
                    self.log("Trying again the keys that needed a base game")
            else:
                self.log(f"Key {k['key'][:5]}…: {MESSAGES.get(status, 'Steam refused it (code %d)' % r['code'])}"
                         + (f" ({what})" if r["names"] else ""), "warn" if status in ("invalid", "used", "error") else "info")
            time.sleep(GAP)
        self.progress.pop("keys", None)
        if added and self.settings["notify"]:
            self.notify(f"Redeemed {added} key{'s' * (added > 1)}", "They're in your Steam library.")

    def _retry_needs_base(self):
        """Queue again the keys Steam turned down for want of a base game, if a key was added since
        they were tried - it may have been that game (a bundle's DLC often comes up before it).
        Each try stamps the key again, so it waits for the next added key before another."""
        box = self._keys()
        with self.lock:
            last = max((k["at"] for k in box["list"] if k["status"] == "added"), default=0)
            again = [k for k in box["list"] if k["status"] == "needs_base" and k["at"] < last]
            for k in again:
                k["status"] = "waiting"
        if again:
            self._save_keys()
        return len(again)

    def _keys_chore(self):
        """Go on after a restart or after Steam's hourly limit."""
        box = self._keys()
        self._retry_needs_base()
        if box["paused_until"] <= time.time() and any(k["status"] == "waiting" for k in box["list"]):
            self._redeem_soon()

    def clear_keys(self, which):
        """which: "done" (every key no longer waiting) or one key's id."""
        box = self._keys()
        with self.lock:
            box["list"] = [k for k in box["list"] if not (k["status"] != "waiting" if which == "done" else k["id"] == which)]
        self._save_keys()

    def keys_snapshot(self):
        box = self._keys()
        return {"list": [dict(k, key=_masked(k["key"])) for k in box["list"]][-300:],
                "paused_until": box["paused_until"] if box["paused_until"] > time.time() else 0,
                "counts": {s: sum(1 for k in box["list"] if k["status"] == s) for s in ("waiting", "added", "owned")},
                "problems": sum(1 for k in box["list"] if k["status"] not in ("waiting", "added", "owned"))}


MESSAGES = {"owned": "you own it already", "region": "not for your region", "invalid": "Steam says it isn't a valid key",
            "used": "already redeemed by another account", "needs_base": "needs the base game first",
            "wallet": "it's a Steam Wallet code: add it at store.steampowered.com/account/redeemwalletcode"}


def _id(key):
    """A key's name for the page, which never sees the key itself."""
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _masked(key):
    """A key as the page shows it: only its last group (the full keys stay in keys.json)."""
    return "•••••-" * (key.count("-")) + key.rsplit("-", 1)[-1]
