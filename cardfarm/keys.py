"""Redeeming Steam keys in bulk: paste any text (an email, a Humble page, a list) and every Steam
key in it is added to the account, one after another, waiting out Steam's hourly limit. Mixed into
Farm. The keys are kept in keys.json in the data folder, readable only by you."""
import hashlib
import re
import threading
import time

from . import files, steam

KEY = re.compile(r"(?<![A-Z0-9-])[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}(?:-[A-Z0-9]{5}-[A-Z0-9]{5})?(?![A-Z0-9-])")
GAP = 4               # seconds between keys
LIMIT_WAIT = 3600     # Steam's "too many attempts" lasts about an hour
# Steam's answers (purchase_result_details), as the page shows them
RESULTS = {0: "added", 9: "owned", 13: "region", 14: "invalid", 15: "used", 24: "needs_base", 36: "invalid",
           50: "wallet", 53: "limit"}


def find_keys(text):
    """Every Steam key in a piece of text, in order, each once."""
    seen, out = set(), []
    for k in KEY.findall((text or "").upper()):
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


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
        box = self._keys()
        with self.lock:
            have = {k["key"] for k in box["list"]}
            new = [k for k in find_keys(text) if k not in have]
            box["list"] += [{"key": k, "id": _id(k), "status": "waiting", "names": [], "at": 0, "source": source} for k in new]
        if not new:
            raise ValueError("no new Steam keys in that text (they look like XXXXX-XXXXX-XXXXX)")
        self._save_keys()
        self.log(f"{len(new)} Steam key{'s' * (len(new) > 1)} to redeem")
        self._redeem_soon()
        return {"added": len(new)}

    def _redeem_soon(self):
        self._in_background("keys", self._redeem_keys)

    def _redeem_keys(self):
        box = self._keys()
        added = 0
        while True:
            if box["paused_until"] > time.time():
                break
            with self.lock:
                k = next((k for k in box["list"] if k["status"] == "waiting"), None)
            if not k:
                break
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
                    k.update(status=status, names=r["names"], at=time.time(), code=r["code"])
            self._save_keys()
            if status == "limit":
                self.log("Steam says too many keys were tried for now; the rest are redeemed in an hour", "warn")
                break
            what = ", ".join(r["names"]) or k["key"]
            if status == "added":
                added += 1
                self.log(f"Redeemed {what}", "sell")
            else:
                self.log(f"Key {k['key'][:5]}…: {MESSAGES.get(status, 'Steam refused it (code %d)' % r['code'])}"
                         + (f" ({what})" if r["names"] else ""), "warn" if status in ("invalid", "used", "error") else "info")
            time.sleep(GAP)
        self.progress.pop("keys", None)
        if added and self.settings["notify"]:
            self.notify(f"Redeemed {added} key{'s' * (added > 1)}", "They're in your Steam library.")

    def _keys_chore(self):
        """Go on after a restart or after Steam's hourly limit."""
        box = self._keys()
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
