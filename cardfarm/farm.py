"""The farm: keeps the right games idling, notices drops, and sells / gems / queues each new card.
On request it also sorts the other things in your inventories (skins, cases, ...) for selling."""
import json
import os
import subprocess
import sys
import threading
import time
import traceback

from . import extras, files, http, market, power, qr, quick, rules, steam, update
from .auth import NO_RENEWAL, Session
from .idler import Idler, game_you_are_playing, steam_pid

BADGES_EVERY = 10 * 60
INVENTORY_EVERY = 30 * 60
ITEMS_EVERY = 60 * 60
UPDATE_EVERY = 6 * 3600
SKIP_FOR = 12 * 3600
NO_IDLE_FOR = 7 * 24 * 3600  # a game that never drops while idled (free-to-play ones like CS2) waits this long
NO_IDLE_AFTER = 6 * 3600     # ...judged after this much idling without a single drop
BULK_MINUTES = 120           # games played less than this idle together first: most don't drop cards before ~2 h
BULK_MAX = 32                # ...up to this many at once (Steam's own limit)
EXPLAIN_WAIT = 20 * 60  # the badges page shows a drop a minute or two after the inventory does
OWED_FOR = 2 * 3600     # a drop count no card turned up for is forgotten after this
WATCH_EVERY = 10        # seconds between looks at whether you started playing something yourself
RENEW_TRIES = 3         # Steam refusing to renew the sign-in this often, over RENEW_OVER, means signed out
RENEW_OVER = 30 * 60
ITEM_SPREAD = 0.7       # a new item whose cheapest listing is under this share of its usual price waits for review


def fresh_state():
    return {"seen": [], "review": [], "history": [], "games": [], "skipped": {}, "idling": {},
            "stats": {"drops": 0, "listed": 0, "listed_cents": 0, "gems": 0, "items_listed": 0, "items_cents": 0,
                      "foil_drops": []},  # which drops (by number, from 0) were foil cards: the garden's foil flowers
            "baseline": False,
            "queue": [],     # [{"action", "entry"}] still to do, in order; kept across restarts
            "kept": [],      # asset ids you chose to keep, so they aren't offered again
            "owed": {},      # appid -> [when] for card drops the badges page showed, not yet matched to a card
            "unexplained": {},  # asset id -> when a new card was first seen that no drop explained yet
            "steamid": None,
            "no_idle": {},   # appid -> until when: games that don't drop cards while idled
            "skipped_by_you": [],  # appids you skipped: left alone until you unskip them
            "free_to_play": {},    # appid -> whether the store calls it free to play (looked up once)
            "idle_anyway": [],     # free-to-play appids you chose to idle all the same
            "playtime": {},        # appid -> minutes played, from Steam (games with drops only)
            "bulk": {},            # appid -> {"base": minutes when bulk idling began, "secs": bulk idled since}
            "items": {"inventories": [], "review": [], "seen": {}, "checked": 0},
            **market.market_state(),
            **extras.extras_state(),
            "quick": quick.quick_state()}


class Farm(market.MarketParts, market.HeldBoosters, market.PatientBuying, quick.QuickBadge, extras.SaleRewards,
           extras.Worth, extras.Alerts):
    def __init__(self, data_dir):
        self.data = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.lock = threading.RLock()
        self.session = Session(os.path.join(data_dir, "session.json"))
        self.settings = rules.load_settings(self._path("settings.json"))
        self.prices = steam.Prices(self._path("prices.json"))
        self.prices.session = self.session
        self.idler = Idler(data_dir)
        self.state = fresh_state()
        woes = []  # said once the state (and so the Activity list) is there
        saved = files.read_json(self._path("state.json"), backup=True, say=woes.append)
        if isinstance(saved, dict):
            self.state.update(saved)
            self.state["stats"] = dict(fresh_state()["stats"], **saved.get("stats", {}))
            self.state["items"] = dict(fresh_state()["items"], **saved.get("items", {}))
        self.state["idling"] = {}  # the idlers went away with the last run
        for woe in woes:
            self.log(woe, "warn")
        if self.session.signed_in:
            self._for_account()
        self.status = "starting"
        self.error = None
        self.signin_error = None
        self.busy = set()  # long jobs running for the page: "cards", "items", "listings", ...
        self.progress = {}  # job -> what it's doing, for the page
        self._next_sales = self._next_listings = self._next_orders = 0
        self.notify = _notify
        self.on_quit = None  # set by the window: closes Card Farm (after an update is handed over)
        self.update, self._next_update = None, 0
        self._next_badges = self._next_inventory = 0
        self._next_items = time.time() + ITEMS_EVERY
        self._soon = False
        self._on_battery = False
        self._chores_running = threading.Lock()
        self._checking_items = threading.Lock()
        self._refused = []  # when Steam wouldn't renew the sign-in
        self._steam = None
        self._poller = None
        self._qr_svg = (None, None)
        self._wake = threading.Event()
        self._jobs = threading.Event()
        http.on_pause = self._slowed_down

    SITES = {"steamcommunity.com": "the Steam Community and market", "store.steampowered.com": "the Steam store",
             "api.steampowered.com": "Steam's Web API"}

    def _slowed_down(self, host, minutes):
        """Steam said too many requests: everything on that site waits (see http.PAUSING)."""
        self._starting(f"Steam asked to slow down; waiting {minutes} min…")
        self.log(f"Steam said too many requests; requests to {self.SITES.get(host, host)} wait {minutes} min", "warn")

    def _remember_farmed(self, card, number):
        """farm-history.json: every card this app farmed (number, when, card, game, foil, version), one
        entry per drop, kept for good; the garden's flowers are these."""
        path = self._path("farm-history.json")
        history = files.read_json(path, [], say=self.note)
        if not isinstance(history, list):
            history = []
        history.append({"n": number, "at": round(time.time()), "appid": card["appid"], "card": card["name"],
                        "game": card["game"], "foil": bool(card["foil"]), "version": update.VERSION})
        files.write_json(path, history, indent=0)

    def _path(self, name):
        return os.path.join(self.data, name)

    def save(self):
        with self.lock:
            files.write_json(self._path("state.json"), self.state)

    def log(self, text, kind="info"):
        with self.lock:
            self.state["history"].insert(0, {"at": time.time(), "kind": kind, "text": text})
            del self.state["history"][300:]
        self.note(text)

    def note(self, text):
        """For card-farm.log only (in the data folder): what doesn't belong in the Activity list
        but tells what went wrong afterwards."""
        print(text, flush=True)
        files.log_line(self._path("card-farm.log"), text)

    # --- settings / sign-in ------------------------------------------------------------------
    def update_settings(self, patch):
        with self.lock:
            was = self.settings["farming"]
            self.settings.update(rules.clean(patch))
            files.write_json(self._path("settings.json"), self.settings, indent=1)
        if was and not self.settings["farming"]:  # at once: the main loop may be busy reading from Steam
            self._idle_none("Stopped")
        self.poke()

    def start_sign_in(self):
        self.signin_error = None
        qr_code = self.session.start_qr()
        with self.lock:  # one poller follows whichever code is showing
            if self._poller is None:
                self._poller = threading.Thread(target=self._wait_for_phone, daemon=True)
                self._poller.start()
        return qr_code

    def _wait_for_phone(self):
        while True:
            with self.lock:
                pending = self.session.qr
                if not pending:
                    self._poller = None
                    return
            time.sleep(pending["interval"])
            try:
                if self.session.poll_qr():
                    self.log(f"Signed in as {self.session.account}")
                    self._for_account()
                    self._next_badges = self._next_inventory = 0
                    self.poke()
                elif not self.session.qr:
                    self.signin_error = "That code expired. Show a new one to try again."
            except Exception as e:
                self.signin_error = f"Steam didn't answer the sign-in check ({e}); still trying."

    def _for_account(self):
        """What Card Farm remembers belongs to one Steam account: signing in as another starts afresh,
        so that account's cards aren't mistaken for new drops."""
        with self.lock:
            if self.state.get("steamid") not in (None, self.session.steamid):
                history = self.state["history"]
                self.state = fresh_state()
                self.state["history"] = history
            self.state["steamid"] = self.session.steamid

    def sign_out(self):
        self.idler.stop_all()
        self.session.sign_out()
        with self.lock:
            self.state["idling"] = {}
        self.log("Signed out")

    def _renewal_refused(self):
        """Steam wouldn't renew the sign-in. Once can be a hiccup; again and again means the sign-in
        ended (after months, or you signed this device out), and only signing in again helps: back
        to the sign-in screen, instead of an error that repeats for good."""
        now = time.time()
        self._refused = [t for t in self._refused if now - t < 6 * 3600] + [now]
        if len(self._refused) < RENEW_TRIES or now - self._refused[0] < RENEW_OVER:
            return
        self._refused = []
        self.sign_out()
        self.signin_error = "Steam ended the sign-in. Scan a new code to sign in again."
        self.log("Steam wouldn't renew the sign-in any more; sign in again to go on", "warn")
        if self.settings["notify"]:
            self.notify("Signed out of Steam", "Open Card Farmer's Market and sign in again to go on farming.")

    def poke(self):
        self._wake.set()

    def refresh(self):
        self._next_badges = self._next_inventory = 0
        self.poke()

    # --- main loop ---------------------------------------------------------------------------
    def run(self):
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()
        backoff = 0  # minutes; grows while Steam keeps saying "too many requests"
        while True:
            wait = 30
            try:
                self.tick()
                self.error, backoff = None, 0
            except steam.HttpError as e:
                if e.status != 429:
                    self.error = str(e)
                else:
                    backoff = min(backoff * 2 or 5, 30)
                    wait = backoff * 60
                    self.error = f"Steam asked to slow down (too many requests); trying again in {backoff} min"
                    self.log(self.error, "warn")
            except Exception as e:
                self.error = str(e)
                self.note(traceback.format_exc())
                if NO_RENEWAL in str(e):
                    self._renewal_refused()
            self.save()
            self._wake.wait(wait)
            self._wake.clear()

    def tick(self):
        s = self.settings
        if time.time() >= self._next_update:
            self._next_update = time.time() + UPDATE_EVERY
            try:
                self.update = update.check()
            except Exception as e:
                self.note(f"Update check failed: {e}")
        if not self.session.signed_in:
            return self._idle_none("Sign in to start")
        threading.Thread(target=self._chores, daemon=True).start()
        if not s["farming"]:
            return self._idle_none("Stopped")
        pid = steam_pid()
        if not pid:
            return self._idle_none("Waiting for Steam to start")
        if not power.in_hours(s["hours_from"], s["hours_to"]):
            return self._idle_none(f"Waiting for your farming hours ({s['hours_from']}:00 to {s['hours_to']}:00)")
        self._on_battery = power.on_battery()
        if s["pause_on_battery"] and self._on_battery:
            return self._idle_none("Paused on battery power (plug in the charger to go on)")
        if self._steam and pid != self._steam and self.idler.running():
            self.log("Steam restarted; starting the games again")
            self._idle_none("Steam restarted")
        self._steam = pid
        playing = game_you_are_playing(self.idler.running()) if s["pause_when_playing"] else None
        if playing:
            return self._idle_none(f"Paused while you play ({self._name(playing)})")
        now, dropped = time.time(), False
        if now >= self._next_badges or now >= self._next_inventory:
            self._starting("Reading your badges…")
            dropped = self._refresh_badges()
            self._next_badges = now + BADGES_EVERY
            self._learn_free_games()
            self._read_playtime()
        self._count_idle_time()
        self._check_stalls()
        self._choose_games()  # idling first: the inventory and prices can take a while
        if dropped or now >= self._next_inventory:  # read right after fresh badges, to tell drops apart
            self._next_inventory = time.time() + 5 * 60  # if this fails, again in 5 min
            self._check_inventory()  # may ask for a sooner look, for cards the badges don't show yet
            self._next_inventory = min(self._next_inventory if self._soon else float("inf"),
                                       time.time() + INVENTORY_EVERY)
        self._price_one_game()

    def _chores(self):
        """The regular market reads (new items, sales, buy orders, old listings). On a thread of
        their own: they can take minutes, and starting, stopping and pausing games mustn't wait."""
        if not self._chores_running.acquire(blocking=False):
            return  # the last round is still at it
        try:
            s = self.settings
            if s["items_new"] != "off" and time.time() >= self._next_items:
                self._next_items = time.time() + ITEMS_EVERY
                self._check_items(manual=False)
            if time.time() >= self._next_sales:
                self._next_sales = time.time() + market.SALES_EVERY
                try:
                    self._sync_sales()
                except Exception as e:
                    self.note(f"Sales check failed: {e}")
            if time.time() >= self._next_orders:
                self._next_orders = time.time() + 30 * 60
                try:
                    self._check_orders()
                except Exception as e:
                    self.note(f"Buy order check failed: {e}")
            if s["cancel_after_days"] and time.time() >= self._next_listings:
                self._next_listings = time.time() + market.LISTINGS_EVERY
                self.check_listings()
            for chore in (self._sale_chore, self._alerts_chore, self._worth_chore):
                try:
                    chore()
                except Exception as e:
                    self.note(f"{chore.__name__.strip('_').replace('_', ' ').capitalize()} failed: {e}")
        finally:
            self._chores_running.release()

    def _watch(self):
        """Pausing for a game you start yourself doesn't wait for the main loop, which can be minutes
        into reading your inventory or prices."""
        while True:
            time.sleep(WATCH_EVERY)
            try:
                self._pause_if_playing()
            except Exception as e:
                self.note(f"Couldn't check what you're playing: {e}")

    def _pause_if_playing(self):
        ours = self.idler.running()
        if not (ours and self.settings["farming"] and self.settings["pause_when_playing"]):
            return
        playing = game_you_are_playing(ours)
        if playing:
            self._idle_none(f"Paused while you play ({self._name(playing)})")

    def _starting(self, what):
        """Say what's going on while nothing is idling yet (the first minutes after starting)."""
        if not self.state["idling"]:
            self.status = what

    def _idle_none(self, why):
        if self.idler.running():
            self.idler.stop_all()
        with self.lock:
            self.state["idling"] = {}
        self.status = why

    def _name(self, appid):
        return next((g["name"] for g in self.state["games"] if g["appid"] == appid), str(appid))

    def _refresh_badges(self):
        """Reads the badges page; returns True when cards dropped since the last read."""
        games = steam.badges(self.session)
        dropped = False
        with self.lock:
            old = {g["appid"]: g for g in self.state["games"]}
            owed = self.state["owed"]
            for g in games:
                before = old.get(g["appid"])
                if not before:
                    continue
                got = max(before["drops"] - g["drops"],
                          (g["earned"] - g["drops"]) - (before["earned"] - before["drops"]))
                if got > 0:
                    owed[str(g["appid"])] = _owed_list(owed.get(str(g["appid"]))) + [time.time()] * got
                    self.state.get("no_idle", {}).pop(str(g["appid"]), None)  # it drops after all
                    dropped = True
                    info = self.state["idling"].get(str(g["appid"]))
                    if info:
                        info.update(last_drop=time.time(), clock=time.time(), restarts=0)
            self.state["games"] = games
        return dropped

    def _candidates(self, with_skipped=False):
        now, s = time.time(), self.settings
        with self.lock:
            skipped = {int(a) for a, until in list(self.state["skipped"].items()) + list(self.state.get("no_idle", {}).items())
                       if until > now} | {int(a) for a in self.state.get("skipped_by_you", [])} | self._free_skipped()
            games = [g for g in self.state["games"] if g["drops"] > 0 and (with_skipped or g["appid"] not in skipped)]
        if s["order"] == "most":
            games.sort(key=lambda g: -g["drops"])
        elif s["order"] == "fewest":
            games.sort(key=lambda g: g["drops"])
        else:
            games.sort(key=lambda g: -(self._game_value(g["appid"]) or 0))
        return games

    def _free_skipped(self):
        """Free-to-play games left out by the "skip free-to-play games" rule."""
        if not self.settings["skip_free_to_play"]:
            return set()
        anyway = set(self.state.get("idle_anyway", []))
        return {int(a) for a, free in self.state.get("free_to_play", {}).items() if free and a not in anyway}

    def _learn_free_games(self):
        """Look up, once per game, whether a game with drops is free to play: those give card drops
        for in-game purchases, which come while you play them, not while they're idled (CS2)."""
        with self.lock:
            known = self.state.setdefault("free_to_play", {})
            todo = [g["appid"] for g in self.state["games"] if g["drops"] > 0 and str(g["appid"]) not in known][:20]
        for appid in todo:
            try:
                free = steam.free_to_play(appid)
            except Exception as e:  # tried again with the next badges check
                self.note(f"Free-to-play check failed: {e}")
                return
            with self.lock:
                known[str(appid)] = free
            if free and self.settings["skip_free_to_play"] and str(appid) not in self.state.get("idle_anyway", []):
                self.log(f"Skipping {self._name(appid)}: it's free to play, and those drop cards when you play them yourself")
            time.sleep(1)

    def _game_value(self, appid):
        hit = self.prices.cache.get(f"cards:{appid}")
        normal = [c for n, c in (hit or {}).get("v", {}).items() if "(Foil)" not in n]
        return sum(normal) / len(normal) if normal else None

    def _read_playtime(self):
        try:
            minutes = steam.playtime(self.session)
        except Exception as e:  # without it every game counts as past the bulk stage
            self.note(f"Playtime check failed: {e}")
            return
        with self.lock:
            self.state["playtime"] = {str(g["appid"]): minutes[g["appid"]] for g in self.state["games"]
                                      if g["drops"] > 0 and g["appid"] in minutes}

    def _played(self, appid):
        """Minutes of playtime: Steam's record, or more when bulk idling since added to it (Steam
        updates its record now and then). A game Steam has no record of counts as played enough."""
        steam_minutes = self.state.get("playtime", {}).get(str(appid))
        if steam_minutes is None:
            return float("inf")
        bulk = self.state.get("bulk", {}).get(str(appid))
        return max(steam_minutes, bulk["base"] + bulk["secs"] / 60) if bulk else steam_minutes

    def _pick(self):
        """(appids to idle, whether they're building playtime). Games short of the playtime most
        games need before they drop cards idle together first, since playtime counts for every
        running game; then one game at a time, in the "farm first" order, since Steam drops
        cards for one running game far more readily than for several."""
        games = self._candidates()
        with self.lock:
            young = [g["appid"] for g in games if self._played(g["appid"]) < BULK_MINUTES]
        if young:
            return young[:BULK_MAX], True
        return [g["appid"] for g in games[:1]], False

    def _choose_games(self):
        """Idle the games that should be idling. Starting takes a few seconds, so it happens without
        holding the lock (the window keeps reading the state meanwhile)."""
        want, bulk = self._pick()
        with self.lock:
            stop = [int(k) for k in self.state["idling"] if int(k) not in want]
            for appid in stop:
                self.state["idling"].pop(str(appid))
        for appid in stop:
            self.idler.stop(appid)
        if want and not self.idler.library:
            self._starting("Finding Steam's API library in your games (first start only)…")
        elif want:
            self._starting("Starting games…")
        failed = self.idler.start(want)
        now = time.time()
        with self.lock:
            idling = self.state["idling"]  # keyed by str(appid), like everything saved as JSON
            for appid in want:
                if appid in failed:
                    self.log(f"Couldn't start {self._name(appid)}: {failed[appid]}. Skipping it for now", "warn")
                    self.state["skipped"][str(appid)] = now + SKIP_FOR
                    idling.pop(str(appid), None)
                    continue
                info = idling.setdefault(str(appid), {"since": now, "clock": now, "last_drop": None, "restarts": 0})
                if info.get("bulk") and not bulk:  # done building playtime: the drop clock starts now
                    info.update(since=now, clock=now, restarts=0)
                info["bulk"] = bulk
                if bulk:
                    self.state.setdefault("bulk", {}).setdefault(
                        str(appid), {"base": self.state["playtime"].get(str(appid), 0), "secs": 0})
            names = [self._name(int(a)) for a in idling]
        left = sum(g["drops"] for g in self.state["games"])
        if names and bulk:
            self.status = (f"Building playtime: {len(names)} game{'s' * (len(names) > 1)} together until each has "
                           f"{BULK_MINUTES // 60} h (cards rarely drop before that)")
        elif names:
            self.status = f"Farming {', '.join(names)}" if len(names) <= 2 else f"Farming {len(names)} games"
        else:
            self.status = "All cards collected" if not left else "Waiting (games with drops are skipped for now)"

    def _check_stalls(self):
        limit = self.settings["stall_minutes"] * 60
        if not limit:
            return
        restart, give_up = [], []
        with self.lock:
            for appid, info in list(self.state["idling"].items()):
                if info.get("bulk") or time.time() - info["clock"] < limit:
                    continue
                if info["restarts"] < 1:
                    restart.append(appid)
                    info.update(restarts=1, clock=time.time())
                else:
                    give_up.append(appid)
                    self.state["idling"].pop(appid)
                    never = not self.state["farmed"].get(appid) and self.state["idled"].get(appid, 0) >= NO_IDLE_AFTER
                    if never:
                        self.state.setdefault("no_idle", {})[appid] = time.time() + NO_IDLE_FOR
                    else:
                        self.state["skipped"][appid] = time.time() + SKIP_FOR
        for appid in restart:
            self.log(f"No drop from {self._name(int(appid))} in {limit // 60} min; restarting it", "warn")
            self.idler.stop(int(appid))  # _choose_games starts it again
        for appid in give_up:
            if appid in self.state.get("no_idle", {}):
                self.log(f"{self._name(int(appid))} hasn't dropped a card in {self.state['idled'][appid] / 3600:.0f} h of idling; "
                         "setting it aside for 7 days (free-to-play games like CS2 drop cards when you play them yourself)", "warn")
            else:
                self.log(f"{self._name(int(appid))} still isn't dropping; trying other games for 12 h", "warn")
            self.idler.stop(int(appid))

    def _price_one_game(self):
        """Fill in card prices for one game per tick, so 'priciest first' learns the order gently."""
        if self.settings["order"] != "value":
            return
        for g in self._candidates():
            if self._game_value(g["appid"]) is None and f"cards:{g['appid']}" not in self.prices.cache:
                self.prices.game_cards(g["appid"])
                return

    # --- drops ---------------------------------------------------------------------------------
    def _check_inventory(self):
        self._soon = False
        cards = steam.cards_in_inventory(self.session)
        ids = {c["assetid"] for c in cards}
        self._hold(cards)
        with self.lock:
            seen = set(self.state["seen"])
            first_look = not self.state["baseline"]
            new = [c for c in cards if c["assetid"] not in seen]
            # cards that left (sold, ground, traded) drop out of the lists
            self.state["review"] = [e for e in self.state["review"] if e["id"] in ids]
            self.state["kept"] = [a for a in self.state["kept"] if a in ids]
            if first_look:
                self.state["seen"], self.state["baseline"] = sorted(ids), True
        if first_look:
            self.log(f"Found {len(cards)} cards already in your inventory; left alone "
                     "(use 'Sort cards I have' to sort them)")
            return
        if self.state["review"]:
            self._drop_pending_from_review()
        handled, now = set(), time.time()
        for card in new:
            key = str(card["appid"])
            with self.lock:
                owed = [t for t in _owed_list(self.state["owed"].get(key)) if now - t < OWED_FOR]
                self.state["owed"][key] = owed
                waiting_since = self.state["unexplained"].setdefault(card["assetid"], now)
                opened = self.state.get("opened", {}).get(key, 0)
                if opened and not owed:  # from a pack you opened: no need to wait for the badges page
                    self.state["opened"][key] = opened - 1
                    waiting_since = now - EXPLAIN_WAIT
            if not owed and now - waiting_since < EXPLAIN_WAIT:
                # not on the badges page yet, or not a drop: look again in a few minutes
                self._soon = True
                self._next_inventory = now + 3 * 60
                continue
            try:
                self.handle(card, "drop" if owed else "new")
            except Exception as e:  # stays unseen, so the next check tries again
                self.log(f"Couldn't price {card['name']} ({card['game']}): {e}", "warn")
                continue
            handled.add(card["assetid"])
            with self.lock:
                self.state["unexplained"].pop(card["assetid"], None)
                if owed:
                    self.state["owed"][key] = owed[1:]
                    self.state["stats"]["drops"] += 1
                    self._remember_farmed(card, self.state["stats"]["drops"] - 1)
                    if card["foil"]:
                        foils = self.state["stats"].setdefault("foil_drops", [])
                        foils.append(self.state["stats"]["drops"] - 1)
                        del foils[:-300]  # more than the garden ever shows
                    self._farmed_one(card["appid"])
        with self.lock:
            added = set(self.state["seen"]) - seen  # put back meanwhile (a cancelled listing): still not new
            self.state["seen"] = sorted((seen & ids) | handled | (added & ids))
            self.state["unexplained"] = {a: t for a, t in self.state["unexplained"].items() if a in ids}

    def _drop_pending_from_review(self):
        """Cards listed elsewhere and waiting for the phone can't be listed again."""
        pending = steam.pending_listings(self.session)
        with self.lock:
            self.state["review"] = [e for e in self.state["review"] if e["id"] not in pending]
            items = self.state["items"]
            items["review"] = [e for e in items["review"] if e["id"] not in pending]
        return pending

    def evaluate(self, card):
        if self.finishing(card["appid"]):
            return "keep", {"why": "you're finishing its set", "buyer": None, "gems": 0}
        lowest = self.prices.game_cards(card["appid"]).get(card["hash"])
        item_type = card["goo"][1] if card.get("goo") else None
        gems = self.prices.gems(card["appid"], item_type, card["foil"])
        action, detail = rules.decide(card, lowest, gems, self.prices.gem_sack(), self.settings)
        detail.setdefault("gems", gems)
        detail.setdefault("buyer", lowest)
        return action, detail

    def handle(self, card, why):
        """why: 'drop' follows the settings; 'new' (a card that isn't from a drop: a trade, a booster
        pack, a purchase) and 'existing' go to review at most."""
        action, detail = self.evaluate(card)
        label = f"{card['name']} ({card['game']})"
        # one of the two switched off: the other is still better than leaving the card
        if action == "sell" and self.settings["sell"] == "off" and detail.get("gems"):
            action = "gems"
        elif action == "gems" and self.settings["gems"] == "off" and detail.get("buyer") \
                and detail["buyer"] >= self.settings["min_sell_cents"]:
            action = "sell"
        mode = self.settings.get(action, "off") if action in ("sell", "gems") else "off"
        if why != "drop" and mode != "off":
            mode = "review"
        if action == "gems" and mode == "auto" and card.get("marketable", True) and not detail.get("buyer"):
            # no price isn't a low price: Steam may just not have shown one. Gems can't be undone.
            mode = "review"
        if action == "keep" or mode == "off":
            if why != "existing":
                self.log(f"New card: {label}; kept ({detail.get('why') or f'{action} is off'})")
            return
        entry = {"id": card["assetid"], "kind": "card", "card": card, "action": action, "detail": detail,
                 "at": time.time()}
        if mode == "auto":
            self.log(f"New card: {label}")
            self._enqueue(action, entry)
            return
        with self.lock:
            if not any(e["id"] == entry["id"] for e in self.state["review"]):
                self.state["review"].append(entry)
        if why == "drop":
            self.log(f"New card: {label}; waiting for your review")
        elif why == "new":
            self.log(f"New card: {label}, not from a card drop (a trade, booster pack or purchase?); "
                     "waiting for your review")

    def review_existing(self):
        """Run every card already in the inventory through the rules, into the review list."""
        self._in_background("cards", self._review_existing)

    def _in_background(self, job, fn):
        with self.lock:
            if job in self.busy:
                return
            self.busy.add(job)

        def go():
            try:
                fn()
            finally:
                with self.lock:
                    self.busy.discard(job)
        threading.Thread(target=go, daemon=True).start()

    def _queued_ids(self):
        with self.lock:
            return ({e["id"] for e in self.state["review"]} | {e["id"] for e in self.state["items"]["review"]}
                    | {j["entry"]["id"] for j in self.state["queue"]} | set(self.state["kept"]))

    def _review_existing(self):
        try:
            pending = self._drop_pending_from_review()
            skip = self._queued_ids() | pending
            cards = [c for c in steam.cards_in_inventory(self.session) if c["assetid"] not in skip]
            self.log(f"Pricing {len(cards)} cards you already have; Steam allows about 20 price lookups a "
                     "minute, so this can take a few minutes…")
            for c in cards:
                self.handle(c, "existing")
            self.log(f"Review list ready: {len(self.state['review'])} cards")
        except Exception as e:
            self.log(f"Couldn't review your cards: {e}", "warn")

    def decide_review(self, ids, action, items=False):
        """The person's call on queued cards (sell / gems / keep / suggested) or items (sell / keep /
        always: keep this and every other one of its kind)."""
        with self.lock:
            box = self.state["items"] if items else self.state
            picked = [e for e in box["review"] if ids == "all" or e["id"] in ids]
            box["review"] = [e for e in box["review"] if e not in picked]
            for e in picked:
                act = e["action"] if action == "suggested" else action
                if act == "always":
                    names = set(self.settings["items_keep"]) | {e["item"]["hash"]}
                    self.settings["items_keep"] = sorted(names)
                    box["review"] = [x for x in box["review"] if x["item"]["hash"] not in names]
                if act in ("keep", "always"):
                    self.state["kept"].append(e["id"])
                elif e.get("kind") == "card" and self.finishing(e["card"]["appid"]):
                    box["review"].append(e)  # stays: its set is being finished
                elif e.get("kind") == "item" and act == "gems" and not e["item"].get("goo"):
                    box["review"].append(e)  # only backgrounds and emoticons turn into gems
                elif act in ("sell", "gems"):
                    self._enqueue(act, e)
        if action == "always":
            self.update_settings({"items_keep": self.settings["items_keep"]})
        self.save()

    # --- other items ----------------------------------------------------------------------------
    def check_items(self):
        """The page's 'Check items': find the inventories and price what's in the chosen ones."""
        self._in_background("items", lambda: self._check_items(manual=True))

    def _check_items(self, manual):
        """manual: every sellable item in the chosen inventories goes to review. On the timer
        (items_new on), only items that weren't there last time count, and 'auto' lists them."""
        if not self._checking_items.acquire(blocking=False):
            return  # one check at a time (yours and the timer's would undo each other's lists)
        try:
            self._check_items_now(manual)
        except Exception as e:
            self.log(f"Couldn't check your items: {e}", "warn")
        finally:
            self._checking_items.release()

    def _check_items_now(self, manual):
        found = steam.inventories(self.session)
        chosen = [i for i in found if i["key"] in self.settings["items_inventories"]]
        with self.lock:
            self.state["items"]["inventories"] = found
        if not chosen:
            if manual:
                self.log("Found your inventories; pick which ones may be sold from")
            return
        pending = self._drop_pending_from_review()
        skip = self._queued_ids() | pending
        offered = held = 0
        sack = None  # a sack of gems' price, looked up once when an item can become gems
        worth = {"cents": 0, "priced": 0, "of": 0, "at": time.time()}
        for inv in chosen:
            stock = steam.items_in_inventory(self.session, inv["appid"], inv["context"], inv["name"])
            ids = {i["assetid"] for i in stock}
            with self.lock:
                seen = self.state["items"]["seen"]
                before = set(seen[inv["key"]]) if inv["key"] in seen else None
                seen[inv["key"]] = sorted(ids)
                box = self.state["items"]
                box["review"] = [e for e in box["review"]
                                 if e["item"]["context"] != inv["context"] or e["item"]["appid"] != inv["appid"]
                                 or e["id"] in ids]
            for item in stock:
                new = before is not None and item["assetid"] not in before
                if item["assetid"] in skip or not (manual or new):
                    continue
                try:
                    gems = 0
                    if item.get("goo") and self.settings["items_gems"] != "off":
                        gems = self.prices.gems(item["goo"][0], item["goo"][1], bool(item["goo"][2]))
                        sack = sack or self.prices.gem_sack()
                    no_market = {"lowest": None, "median": None, "volume": 0}
                    stats = self.prices.item_stats(item["appid"], item["hash"]) if item.get("marketable", True) else no_market
                    action, detail = rules.decide_item(item, stats, self.settings, gems, sack)
                except Exception as e:
                    self.log(f"Couldn't price {item['name']}: {e}", "warn")
                    continue
                going = rules.realistic(stats)
                worth["of"] += item["amount"]
                if going:
                    worth["cents"] += rules.received(going) * item["amount"]
                    worth["priced"] += item["amount"]
                if action == "keep":
                    held += 1
                    continue
                entry = {"id": item["assetid"], "kind": "item", "item": item, "action": action,
                         "detail": detail, "at": time.time()}
                # one listing far under what it usually sells for is somebody's mistake or bait,
                # not the price: that one is yours to judge
                odd = stats.get("lowest") and stats.get("median") and stats["lowest"] < stats["median"] * ITEM_SPREAD
                auto = self.settings["items_gems"] == "auto" if action == "gems" else self.settings["items_new"] == "auto" and not odd
                if new and not manual and auto:
                    self.log(f"New item: {item['name']} ({inv['name']})")
                    self._enqueue(action, entry)
                else:
                    with self.lock:
                        if not any(e["id"] == entry["id"] for e in box["review"]):
                            box["review"].append(entry)
                    offered += 1
        with self.lock:
            self.state["items"]["checked"] = time.time()
            if manual:  # every item was priced: what they'd all sell for
                self.state["items"]["worth"] = worth
        if manual or offered:
            self.log(f"Items checked: {offered} to review"
                     + (f", {held} left alone (nobody selling, not selling lately, under your minimum or kept)" if held else ""))

    # --- doing it ---------------------------------------------------------------------------------
    def _enqueue(self, action, entry):
        with self.lock:
            self.state["queue"].append({"action": action, "entry": entry})
        self._jobs.set()

    def _worker(self):
        """Works through the queue one job at a time, paced for Steam. A job leaves the queue once
        done (or failed), so a restart picks up where it stopped."""
        listed = 0
        while True:
            with self.lock:
                job = self.state["queue"][0] if self.state["queue"] and self.session.signed_in else None
            if not job:
                if listed and self.settings["notify"]:
                    self.notify(f"{listed} listing{'s' * (listed > 1)} to confirm",
                                "Confirm them in the Steam mobile app (Confirmations).")
                listed = 0
                self._jobs.wait(20)
                self._jobs.clear()
                continue
            e = job["entry"]
            what = _label(e)
            try:
                listed += self._do(job["action"], e)
            except steam.HttpError as err:
                if err.status == 429:  # leave it queued and try again later
                    self.log(f"Steam asked to slow down; {what} waits 5 min", "warn")
                    time.sleep(300)
                    continue
                self.log(f"Couldn't {VERBS.get(job['action'], job['action'])} {what}: {err}", "warn")
            except Exception as err:
                if steam.ALREADY_LISTED in str(err):
                    self.log(f"{what} is already listed and waits for your OK in the Steam app")
                    listed += 1
                else:
                    self.log(f"Couldn't {VERBS.get(job['action'], job['action'])} {what}: {err}", "warn")
            with self.lock:
                if job in self.state["queue"]:
                    self.state["queue"].remove(job)
            self.save()
            time.sleep(1.5)

    def _do(self, action, e):
        """Prices are looked up again here (from the few-hours cache), so a card that waited days
        for review isn't listed at an old price."""
        if e["kind"] == "listing":
            return self._do_listing(action, e)
        if e["kind"] == "gems":
            return self._do_pack(e)
        if e["kind"] == "booster":
            return self._do_booster(e)
        if e["kind"] == "set":
            return self._do_set(e)
        if e["kind"] == "pack":
            return self._do_open(e)
        if e["kind"] == "orders":
            return self._do_orders(e)
        if e["kind"] == "quick":
            return self._do_quick(e)
        if e["kind"] == "item" and action == "gems":
            it = e["item"]
            appid, item_type, border = it["goo"]
            gems = steam.grind(self.session, appid, it["assetid"], self.prices.gems(appid, item_type, bool(border)) or e["detail"]["gems"])
            with self.lock:
                self.state["stats"]["gems"] += gems
            self.log(f"Turned {it['name']} into {gems} gems", "gems")
            return 0
        if e["kind"] == "item":
            it = e["item"]
            buyer = rules.asking_price(rules.realistic(self.prices.item_stats(it["appid"], it["hash"])), self.settings)
            if not buyer:
                raise RuntimeError("nobody is selling or buying one now, so there's no price to go by")
            if buyer < self.settings["items_min_cents"]:
                raise RuntimeError(f"it now goes for ${buyer / 100:.2f}, under your minimum; left in your inventory")
            shown = e.get("detail", {}).get("buyer")
            if shown and buyer < shown * (1 - market.FALL):
                # you agreed to another price: it goes back for a second look at today's
                with self.lock:
                    box = self.state["items"]["review"]
                    if not any(x["id"] == e["id"] for x in box):
                        box.append(dict(e, detail=dict(e["detail"], buyer=buyer, receive=rules.received(buyer)), at=time.time()))
                self.log(f"Didn't list {it['name']}: it now goes for ${buyer / 100:.2f}, not the ${shown / 100:.2f} "
                         "you were shown. It's back in your review list at today's price", "warn")
                return 0
            receive = rules.received(buyer)
            steam.sell(self.session, it["assetid"], receive, it["appid"], it["context"], it["amount"])
            with self.lock:
                self.state["stats"]["items_listed"] += 1
                self.state["stats"]["items_cents"] += receive * it["amount"]
            count, each = (f" ×{it['amount']}", " each") if it["amount"] > 1 else ("", "")
            self.log(f"Listed {it['name']}{count} for ${rules.buyer_pays(receive) / 100:.2f}{each} "
                     f"(you get ${receive / 100:.2f}{each})", "sell")
            return 1
        card = e["card"]
        label = f"{card['name']} ({card['game']})"
        if self.finishing(card["appid"]):  # queued before the set was started
            self.log(f"Kept {label}: you're finishing its set")
            return 0
        if action == "sell":
            buyer = rules.asking_price(self.prices.game_cards(card["appid"]).get(card["hash"]), self.settings)
            if not buyer:
                raise RuntimeError("nobody is selling it now, so there's no price to match")
            receive = rules.received(buyer)
            steam.sell(self.session, card["assetid"], receive)
            with self.lock:
                self.state["stats"]["listed"] += 1
                self.state["stats"]["listed_cents"] += receive
            self.log(f"Listed {label} for ${rules.buyer_pays(receive) / 100:.2f} (you get ${receive / 100:.2f})", "sell")
            return 1
        item_type = card["goo"][1] if card.get("goo") else None
        expected = self.prices.gems(card["appid"], item_type, card["foil"]) or e["detail"]["gems"]
        gems = steam.grind(self.session, card["appid"], card["assetid"], expected)
        with self.lock:
            self.state["stats"]["gems"] += gems
        self.log(f"Turned {label} into {gems} gems", "gems")
        return 0

    # --- for the page --------------------------------------------------------------------------------
    def snapshot(self):
        with self.lock:
            st = self.state
            now = time.time()
            keep = set(self.settings["never_touch"])
            names = {g["appid"]: g["name"] for g in st["games"]}
            free_skipped = self._free_skipped()
            return {
                "signed_in": self.session.signed_in, "account": self.session.account,
                "qr_svg": self._qr(), "signin_error": self.signin_error,
                "status": self.status, "error": self.error, "working": len(st["queue"]),
                "busy": sorted(self.busy), "progress": dict(self.progress),
                "settings": self.settings,
                "idling": [{"appid": int(a), "name": self._name(int(a)), "since": i["since"],
                            "last_drop": i.get("last_drop"), "bulk": bool(i.get("bulk")),
                            "played": min(self._played(int(a)), 10 ** 9)} for a, i in st["idling"].items()],
                "games": [dict(g, value=self._game_value(g["appid"]), keep=g["appid"] in keep,
                               skipped=st["skipped"].get(str(g["appid"]), 0) > now or str(g["appid"]) in st.get("skipped_by_you", []),
                               skipped_by_you=str(g["appid"]) in st.get("skipped_by_you", []),
                               free=st.get("free_to_play", {}).get(str(g["appid"]), False),
                               free_skipped=g["appid"] in free_skipped,
                               no_idle=st.get("no_idle", {}).get(str(g["appid"]), 0) > now,
                               idled=st["idled"].get(str(g["appid"]), 0), farmed=st["farmed"].get(str(g["appid"]), 0))
                          for g in self._candidates(with_skipped=True)] if self.session.signed_in else [],
                "kept_games": [{"appid": a, "name": names.get(a, str(a))} for a in sorted(keep)],
                "drops_left": sum(g["drops"] for g in st["games"]),
                "review": st["review"], "history": st["history"][:80], "stats": st["stats"],
                "items": {"inventories": [dict(i, on=i["key"] in self.settings["items_inventories"])
                                          for i in st["items"]["inventories"]],
                          "review": st["items"]["review"], "checked": st["items"]["checked"]},
                "ui": _page_version(), "version": update.VERSION,
                "update": self.update and dict(self.update, installable=bool(self.update.get("url"))),
                "listings": st["listings"], "sales": dict(self.sales_summary(), lifetime=self.lifetime()), "gems": st["gems"],
                "sale": st["sale"], "worth": self.worth(), "alerts": st["alerts"], "alert_search": st["alert_search"],
                "power": {"battery": self._on_battery, "in_hours": power.in_hours(self.settings["hours_from"], self.settings["hours_to"])},
                "boosters": dict(st["boosters"], options=[o for o in st["boosters"]["options"] if o.get("profit") is not None][:150]),
                "sets": dict(st["sets"], finishing=[dict(v, appid=int(k)) for k, v in st["finishing"].items()]),
                "held": st.get("held", {"packs": [], "checked": 0}),
                "orders": st.get("orders", []), "patient": st.get("patient", []),
                "levelup": dict({k: v for k, v in st["levelup"].items() if k != "options"},
                                options=len(st["levelup"]["options"]), plan=self.levelup_plan()),
                "quick": {"level": self._quick()["level"], "xp_to_next": self._quick()["xp_to_next"], "checked": self._quick()["checked"],
                          "badges": sum(1 for v in self._quick()["badges"].values() if v > 0),
                          "maxed": sum(1 for v in self._quick()["badges"].values() if v >= quick.TOP),
                          "search": {k: v for k, v in self._quick()["search"].items() if k != "results"},
                          "rows": self.quick_rows(), "pick": self.quick_plans()},
            }

    def _qr(self):
        url = self.session.qr and self.session.qr["url"]
        if url and self._qr_svg[0] != url:
            self._qr_svg = (url, qr.svg(url))
        return self._qr_svg[1] if url else None

    def install_update(self):
        """The page's Update button: download, check, hand over to the installer helper, quit."""
        if not self.update:
            raise ValueError("no update to install")
        self._in_background("update", self._install_update)

    def _install_update(self):
        try:
            self.log(f"Downloading Card Farmer's Market {self.update['version']}…")
            path = update.fetch(self.update)
            self.idler.stop_all()
            self.save()
            update.install(path)
            self.log(f"Installing {self.update['version']}; it reopens in a moment")
            self.save()
            if self.on_quit:
                self.on_quit()
        except Exception as e:
            self.log(f"Couldn't update: {e}", "warn")

    def unskip(self, appid):
        with self.lock:
            self.state["skipped"].pop(str(appid), None)
            self.state.get("no_idle", {}).pop(str(appid), None)
            self.state["skipped_by_you"] = [a for a in self.state.get("skipped_by_you", []) if a != str(appid)]
            if self.state.get("free_to_play", {}).get(str(appid)) and str(appid) not in self.state.setdefault("idle_anyway", []):
                self.state["idle_anyway"].append(str(appid))
        self.poke()

    def unskip_all(self):
        """Every game back in the running: skipped by you, set aside for not dropping, or for not
        dropping while idled."""
        with self.lock:
            n = len({*self.state.get("skipped_by_you", []), *map(str, self._free_skipped()),
                     *(a for a, until in [*self.state["skipped"].items(), *self.state.get("no_idle", {}).items()]
                       if until > time.time())})
            self.state["skipped"], self.state["no_idle"], self.state["skipped_by_you"] = {}, {}, []
            self.state["idle_anyway"] = sorted(a for a, free in self.state.get("free_to_play", {}).items() if free)
        if n:
            self.log(f"Unskipped {n} game{'s' * (n > 1)}")
        self.poke()

    def skip(self, appid):
        """Skipped by you: left alone until you unskip it (the farm's own skips are for a while)."""
        with self.lock:
            mine = self.state.setdefault("skipped_by_you", [])
            if str(appid) not in mine:
                mine.append(str(appid))
            self.state["idle_anyway"] = [a for a in self.state.get("idle_anyway", []) if a != str(appid)]
        self.poke()


def _owed_list(value):
    """Drop counts saved by older versions were plain numbers."""
    return list(value) if isinstance(value, list) else [time.time()] * int(value or 0)


VERBS = {"gems": "turn into gems", "set": "finish", "booster": "make", "pack": "pack", "sell": "sell", "open": "open",
         "reprice": "reprice", "cancel": "cancel", "orders": "place", "quick": "level up"}


def _label(e):
    """What a queued job is about, for the tape."""
    kind = e["kind"]
    return {"card": lambda: e["card"]["name"], "item": lambda: e["item"]["name"], "listing": lambda: e["listing"]["name"],
            "gems": lambda: "your gems", "booster": lambda: f"the booster pack for {e['booster']['name']}",
            "set": lambda: f"the {e['offer']['game']} set", "pack": lambda: f"the {e['pack']['game']} booster pack",
            "orders": lambda: "the buy orders", "quick": lambda: f"the {e['game']} badge"}[kind]()


def _page_version():
    """Changes when the page's file does, so an open page reloads itself (0 inside a .pyz)."""
    try:
        return int(os.path.getmtime(os.path.join(os.path.dirname(__file__), "ui", "index.html")))
    except OSError:
        return 0


def _notify(title, body):
    """A desktop notification with each system's own tool (the Linux window sends its own)."""
    try:
        if sys.platform == "darwin":
            script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
            subprocess.run(["osascript", "-e", script], timeout=10)
        elif sys.platform == "win32":
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_TOAST, title, body],
                           timeout=20, creationflags=0x08000000)
        else:
            subprocess.run(["notify-send", "-a", "Card Farmer's Market", title, body], timeout=10)
    except Exception:
        pass


# Windows' own toast, shown under PowerShell's name (an unpackaged app can't use its own)
WINDOWS_TOAST = """
$title, $body = $args
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$text = $xml.GetElementsByTagName('text')
$text.Item(0).AppendChild($xml.CreateTextNode("Card Farmer's Market: $title")) > $null
$text.Item(1).AppendChild($xml.CreateTextNode($body)) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""
