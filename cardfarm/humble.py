"""Humble Bundle: signing in (in a window of the app, on Humble's own pages) and keeping that
sign-in, readable only by you, in humble.json in the data folder. Mixed into Farm."""
import json
import os
import time

from . import files, http

SITE = "https://www.humblebundle.com"
SIGN_IN = f"{SITE}/home/library"  # signed out, Humble sends this on to its sign-in pages and back


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

    def humble_snapshot(self):
        box = self._humble()
        who = box.get("who") or {}
        name = next((who[k] for k in ("email", "name", "username") if isinstance(who.get(k), str)), None)
        return {"signed_in": bool(box.get("cookies")), "who": name, "at": box.get("at", 0)}
