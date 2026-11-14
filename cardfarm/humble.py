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
STORE_GAP = 1           # seconds between store searches
# Telling a Humble title and a Steam name for the same game apart from different games:
ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
TAGS = re.compile(r"\([^)]*\)|\[[^\]]*\]|\b(row|steam|key|retail|digital|pc)\b")   # (ROW), [Digital], (NEW 2025)
# edition names a key's title may carry that the Steam name doesn't, or the other way round: a few
# phrases that are always editions, else a word only together with "edition" ("Anniversary
# Edition", not "Tomb Raider: Anniversary", a game of its own) - and never a bare "collection" or
# "bundle": those hold more than the one game
EDITION = re.compile(r"\b(game of the year|goty|director'?s cut|final cut|complete collection)\b( edition)?"
                     r"|\b([a-z0-9']+ )?edition\b")
SUBTITLE = re.compile(r"\s*(?::|\s[-–—]\s)\s*")

# Put into Humble's library page: keeps every answer the page gets from its library action
# (React's line format: the line holding {results, pagination}), by the page number in the
# address at the time - the answer's own page field always says 1.
LIBRARY_HOOK = r"""
(function () {
  if (window.__cfLib) return;
  window.__cfLib = {pages: {}};
  var plain = window.fetch;
  window.fetch = function (input, init) {
    var asked = new URLSearchParams(location.search).get("page") || "1";
    var headers = (init && init.headers) || {};
    var action = headers["Next-Action"] || headers["next-action"] || (headers.get && headers.get("Next-Action"));
    return plain.apply(this, arguments).then(function (r) {
      if (action) r.clone().text().then(function (t) {
        var line = t.split("\n").find(function (l) { return /^[0-9a-f]+:\{.*"results"/.test(l); });
        if (line) window.__cfLib.pages[asked] = JSON.parse(line.slice(line.indexOf(":") + 1));
      }).catch(function () {});
      return r;
    });
  };
})();
"""


def read_library_pages(js, progress, gap=1, page_wait=45):
    """Every library item, from Humble's library page in a window of the app. js(code) runs code
    in that page and returns its value as text ("" for none). The page is sent through its pages
    the way its own links do it (?page=N, which its router follows); page 1 was fetched before the
    hook went in, so it's asked for again last."""
    js(LIBRARY_HOOK)

    def wait_for(page):
        end = time.time() + page_wait
        while time.time() < end:
            got = js(f"window.__cfLib.pages[{page}] ? JSON.stringify(window.__cfLib.pages[{page}]) : ''")
            if got:
                return json.loads(got)
            time.sleep(0.5)
        raise ValueError(f"Humble's library didn't show page {page}")

    pages, page, last = {}, 2, 2
    while page <= last:
        progress(page - 1, last)
        js(f"window.__cfLib.pages = {{}}; history.pushState(null, '', '?page={page}'); ''")
        pages[page] = wait_for(page)
        last = max(1, int((pages[page].get("pagination") or {}).get("totalPages") or 1))
        page += 1
        time.sleep(gap)
    js("window.__cfLib.pages = {}; history.pushState(null, '', '?page=1'); ''")
    pages[1] = wait_for(1)
    progress(last, last)
    return [item for n in sorted(pages) for item in pages[n].get("results") or []]


def steam_keys(items):
    """(key, title) for every revealed Steam key among library items."""
    out = []
    for it in items:
        for k in ((it.get("entitlements") or {}).get("keys")) or []:
            key = (k.get("redemptionData") or {}).get("keyFormatted")
            if k.get("keyIntegrationId") == "steam" and key and not k.get("expired"):
                out.append((key, k.get("title") or it.get("title") or ""))
    return out


def _plain(name):
    s = re.sub(r"[™®©]|\(tm\)", "", (name or "").lower())
    s = TAGS.sub(" ", s)
    s = re.sub(r"\b(\d+)\s*[&-]\s*(\d+)\b", r"\1 \2", s)              # Soul Reaver 1&2 / 1-2
    s = re.sub(r"[&+]|\band\b", " ", s)
    return re.sub(r"\b(" + "|".join(ROMAN) + r")\b", lambda m: ROMAN[m.group(1)], s)


def steam_unrevealed(items):
    """(item id for its page, title) for every unrevealed, unexpired Steam key among library items."""
    out = []
    for it in items:
        for k in ((it.get("entitlements") or {}).get("keys")) or []:
            if (k.get("keyIntegrationId") == "steam" and not (k.get("redemptionData") or {}).get("keyFormatted")
                    and not k.get("expired") and k.get("redeemable") is not False and it.get("productEntitlementId")):
                out.append((it["productEntitlementId"], k.get("title") or it.get("title") or ""))
    return out


def title_key(name):
    """A game name as compared across Humble and Steam: no ™/®, region tags, edition words, leading
    "The"/"A", roman numerals, punctuation or case."""
    s = re.sub(r"^\s*(the|a|an)\s+", "", _plain(name))
    s = EDITION.sub(" ", s)
    s = re.sub(r"\bthe\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def split_title(name):
    """(the name before its first subtitle, the subtitle or "")."""
    parts = SUBTITLE.split(_plain(name), 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


class OwnedNames:
    """The account's games by name. A title counts as owned when its key is a game's key, or when
    it's the short name of exactly one game Steam lists with a subtitle ("Stray Gods" for "Stray
    Gods: The Roleplaying Musical"). Never the other way round: "Dark Souls III - Ashes of
    Ariandel" is DLC, not Dark Souls III - those go by app id (Humble.resolve_title)."""
    def __init__(self, names):
        self.full, short = set(), {}
        for n in names:
            self.full.add(title_key(n))
            head = title_key(split_title(n)[0])
            short.setdefault(head, set()).add(title_key(n))
        self.short = {k for k, v in short.items() if len(v) == 1 and len(k) >= 6}
        self.full.discard("")

    def owns(self, title):
        k = title_key(title)
        return bool(k) and (k in self.full or k in self.short)


def title_words(name):
    """A name's words, in any order ("Witcher Tales Thronebreaker" is "Thronebreaker: The Witcher Tales")."""
    s = EDITION.sub(" ", _plain(name))
    return frozenset(w for w in re.findall(r"[a-z0-9]+", s.replace("'", "")) if w not in ("the", "a", "an"))


def store_match(title, hits):
    """The app id among store search hits that is this title, when one confidently is."""
    want, words = title_key(title), title_words(title)
    head, sub = split_title(title)
    for appid, name in hits:
        got = title_key(name)
        if got == want or (sub and got == title_key(head + " " + sub)) or (len(words) >= 2 and title_words(name) == words):
            return appid
    return None


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
    humble_page = None  # set by the window: (url, fn) -> fn(js) run in that Humble page, js(code) -> text

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
            if not self.humble_page:
                raise ValueError("the scan needs the app's window (Humble's own library page does the reading)")

            def progress(page, pages):
                self.progress["humble"] = f"Reading your Humble library: page {page} of {pages}"
            self.progress["humble"] = "Opening your Humble library"
            items = self.humble_page(LIBRARY, lambda js: read_library_pages(js, progress))
            ids = [it.get("id") for it in items]
            if len(set(ids)) < len(ids) * 0.9:
                raise ValueError("Humble showed the same items on every page - the scan can't read the library")
            keys = steam_keys(items)
            self.progress["humble"] = "Checking which of its games you have on Steam"
            result = self._sort_humble_keys(keys, steam_unrevealed(items))
            result.update(at=time.time(), items=len(items), steam_keys=len(keys), unrevealed=sum(
                1 for it in items for k in ((it.get("entitlements") or {}).get("keys")) or []
                if k.get("keyIntegrationId") == "steam" and not (k.get("redemptionData") or {}).get("keyFormatted")
                and not k.get("expired")))
            box["last_scan"] = result
            files.write_json(self._path("humble.json"), box, private=True)
            self.log(f"Humble library: {len(keys)} revealed Steam keys for {result['titles']} titles; {result['owned']} titles "
                     f"you have, {result['queued']} keys new to redeem, {result['spare']} spare copies kept"
                     + (f", {result['dropped']} taken off the list" if result["dropped"] else "")
                     + (f"; {result['unrevealed']} not revealed yet, {len(result['to_reveal'])} worth revealing"
                        if result["unrevealed"] else ""))
        except Exception as e:
            box["last_scan"] = {"at": time.time(), "error": str(e)}
            self.log(f"Couldn't read your Humble library: {e}", "warn")
        finally:
            self.progress.pop("humble", None)

    def resolve_title(self, title, owned_ids):
        """Whether the account owns what a Humble title is, by app id: the store's search finds the
        app (DLC too) and the store's list of owned apps says. True / False, or None when the
        search finds nothing that's surely it. Remembered in humble.json (an unknown one is asked
        again after a month)."""
        cache = self._humble().setdefault("titles", {})
        k = title_key(title)
        hit = cache.get(k)
        if hit is None or (hit.get("appid") is None and time.time() - hit.get("at", 0) > 30 * 86400):
            head, sub = split_title(title)
            appid = None
            for term in dict.fromkeys(t for t in (title, sub and f"{head} {sub}", sub) if t and len(t) > 3):
                try:
                    appid = store_match(title, steam.store_search(term))
                except Exception:
                    break
                if appid:
                    break
                time.sleep(STORE_GAP)
            hit = cache[k] = {"appid": appid, "at": time.time()}
        return None if hit["appid"] is None else hit["appid"] in owned_ids

    def _sort_humble_keys(self, keys, unrevealed=()):
        """Which revealed Steam keys go to the Keys list. Grouped by title: an owned title (by name,
        else by app id) sends none and takes its waiting ones off the list; otherwise one key per
        title waits at a time - its copies are kept unused (giftable) unless Steam answers that one
        is used or invalid, when the next copy gets its turn. Unrevealed keys worth revealing: one
        per title you don't have and hold no revealed copy of (the rest stay giftable)."""
        names = OwnedNames(steam.owned_names(self.session).values())
        try:
            owned_ids = steam.owned_apps(self.session)
        except Exception as e:
            owned_ids = None
            self.log(f"Couldn't get your owned DLC from the Steam store ({e}); going by names only", "warn")
        groups = {}
        for key, title in keys:
            groups.setdefault(title_key(title) or key, []).append((key, title))
        status = {k["key"]: k["status"] for k in self._keys()["list"]}
        todo, drop, owned, spare, done = [], [], 0, 0, 0
        for n, group in enumerate(groups.values(), 1):
            title = group[0][1]
            if n % 25 == 0:
                self.progress["humble"] = f"Checking which of its games you have on Steam: {n} of {len(groups)}"
            tried = [status.get(k) for k, _ in group]
            if any(s in ("added", "owned") for s in tried) or names.owns(title) or (
                    owned_ids is not None and self.resolve_title(title, owned_ids)):
                owned += 1
                drop += [k for k, _ in group if status.get(k) == "waiting"]
                continue
            waiting = [k for k, _ in group if status.get(k) == "waiting"]
            if waiting:
                drop += waiting[1:]                                    # one copy at a time
                spare += len(group) - 1
                continue
            untried = [(k, t) for k, t in group if k not in status]
            if "needs_base" in tried:
                done += 1                                              # another copy would need it too
            elif untried:
                todo.append(untried[0])
                spare += len(untried) - 1
            else:
                done += 1                                              # every copy tried: Steam said no to each
        reveal, seen = [], set(groups)
        for item_id, title in unrevealed:
            k = title_key(title) or item_id
            if k in seen or names.owns(title) or (owned_ids is not None and self.resolve_title(title, owned_ids)):
                continue
            seen.add(k)
            reveal.append({"id": item_id, "title": title})
        dropped = self.drop_waiting(drop, "humble")
        return {"titles": len(groups), "owned": owned, "queued": self.queue_keys(todo, "humble"), "spare": spare,
                "dropped": dropped, "exhausted": done, "to_reveal": reveal}

    def humble_reveal(self):
        if not (self._humble().get("last_scan") or {}).get("to_reveal"):
            raise ValueError("nothing to reveal - scan the library first")
        self._in_background("humble-reveal", self._humble_reveal)
        return {"started": True}

    def _humble_reveal(self):
        """Reveal the keys the last scan picked, one item page at a time, pressing its Reveal button
        like a person would, and queue each key it shows. Irreversible on Humble's side: a revealed
        key can't be gifted any more - which is why only those for games you don't have are picked."""
        box = self._humble()
        scan = box.get("last_scan") or {}
        done = 0
        try:
            while scan.get("to_reveal"):
                item = scan["to_reveal"][0]
                self.progress["humble-reveal"] = f"Revealing {item['title']} ({done + 1} of {done + len(scan['to_reveal'])})"
                key = self.humble_page(f"{SITE}/home/library/{item['id']}", reveal_in_page)
                self.queue_keys([(key, item["title"])], "humble")
                self.log(f"Revealed the Steam key for {item['title']} on Humble", "sell")
                scan["to_reveal"].pop(0)
                done += 1
                files.write_json(self._path("humble.json"), box, private=True)
                time.sleep(2)
        except Exception as e:
            self.log(f"Couldn't reveal {item['title']} on Humble: {e}", "warn")
        finally:
            self.progress.pop("humble-reveal", None)

    def humble_snapshot(self):
        box = self._humble()
        who = box.get("who") or {}
        name = next((who[k] for k in ("email", "name", "username") if isinstance(who.get(k), str)), None)
        return {"signed_in": bool(box.get("cookies")), "who": name, "at": box.get("at", 0), "last_scan": box.get("last_scan")}


# Pressing an item page's Reveal button (and the "are you sure" one, if Humble asks) and reading the
# key off the page. Text, not Humble's internals: the item page's code isn't on the library page.
REVEAL_PRESS = r"""
(function () {
  var b = Array.from(document.querySelectorAll("button, a")).find(function (e) {
    return /^\s*reveal( key)?\s*$/i.test(e.innerText || "") && !e.disabled; });
  if (!b) return "none";
  b.click();
  return "pressed";
})()
"""
REVEAL_LOOK = r"""
(function () {
  var text = (document.body.innerText || "") + " " + Array.from(document.querySelectorAll("input, textarea"))
    .map(function (i) { return i.value || ""; }).join(" ");
  var keys = text.toUpperCase().match(/[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}(?:-[A-Z0-9]{5}-[A-Z0-9]{5})?/g) || [];
  var dialog = document.querySelector("[role=alertdialog], [role=dialog]"), pressed = "";
  if (dialog) {
    var b = Array.from(dialog.querySelectorAll("button")).find(function (e) {
      return /^\s*(reveal|confirm|continue|yes|ok)\b/i.test(e.innerText || "") && !e.disabled && !e.dataset.cfPressed; });
    if (b) { b.dataset.cfPressed = "1"; b.click(); pressed = b.innerText.trim(); }
  }
  return JSON.stringify({keys: keys, pressed: pressed});
})()
"""


def reveal_in_page(js, wait=25):
    """In an item's page: press Reveal (once it has drawn), confirm if asked, and return the key it
    then shows. A key already on the page is returned as it is."""
    end = time.time() + wait
    while True:
        look = json.loads(js(REVEAL_LOOK) or "{}")
        if look.get("keys"):
            return look["keys"][0]
        state = js(REVEAL_PRESS)
        if state == "pressed":
            break
        if time.time() > end:
            raise ValueError("the item's page shows no Reveal button")
        time.sleep(1)
    end = time.time() + wait
    while time.time() < end:
        time.sleep(1)
        look = json.loads(js(REVEAL_LOOK) or "{}")
        if look.get("keys"):
            return look["keys"][0]
    raise ValueError("Humble didn't show the key after Reveal")
