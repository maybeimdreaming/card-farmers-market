"""Smaller parts of the farm, mixed into Farm: the free rewards of Steam sales, what your cards and
items are worth, and price alerts on things you watch."""
import time

from . import rules, steam

SALE_EVERY = 3 * 3600   # how often to ask whether a Steam sale is on
QUEUES_A_DAY = 3        # Discovery Queues explored per day during a sale (each has given a sticker)
WORTH_EVERY = 5 * 60    # one game of the cards you hold is priced this often, for their worth
WORTH_STALE = 24 * 3600 # ...when its prices are older than this (or unknown)
ALERTS_EVERY = 60 * 60  # price alerts are checked this often
ALERT_FRESH = 50 * 60   # ...with prices no older than this


def extras_state():
    return {"sale": {"on": False, "checked": 0, "day": "", "queues": 0, "sticker": False, "stickers": 0},
            "holding": {},  # card hash -> [game appid, how many you have]
            "alerts": [],   # [{"id", "appid", "hash", "name", "game", "icon", "when": above|below, "cents", "armed", ...}]
            "alert_search": {"text": "", "results": [], "at": 0}}


def steam_day(now=None):
    """Steam's day, which turns over at 10:00 in Seattle (18:00 UTC in winter): the sale's daily
    rewards come back then."""
    return time.strftime("%Y-%m-%d", time.gmtime((now or time.time()) - 18 * 3600))


class SaleRewards:
    """During a Steam sale: explore the Discovery Queue a few times a day and claim the free daily
    sticker. Between sales it only asks, every few hours, whether one has started."""

    def _sale_chore(self):
        if not self.settings["sale_rewards"] or time.time() < getattr(self, "_next_sale", 0):
            return
        self._next_sale = time.time() + SALE_EVERY
        st = steam.sale_status(self.session)
        with self.lock:
            sale = self.state["sale"]
            if sale["day"] != steam_day():
                sale.update(day=steam_day(), queues=0, sticker=False)
            was = sale["on"]
            sale.update(on=st["sale"], checked=time.time())
        if not st["sale"]:
            if was:
                self.log("The Steam sale is over; its Discovery Queue and sticker wait for the next one")
            return
        if not was:
            self.log("A Steam sale is on: exploring your Discovery Queue and claiming the free sticker each day")
        done = 0
        while sale["queues"] < QUEUES_A_DAY:
            steam.explore_queue(self.session)
            with self.lock:
                sale["queues"] += 1
            done += 1
        if done:
            self.log(f"Explored your Discovery Queue {done} time{'s' * (done > 1)} for the sale's rewards")
        if st["can_claim"] and not sale["sticker"]:
            name = steam.claim_sticker(self.session)
            with self.lock:
                sale.update(sticker=True, stickers=sale["stickers"] + 1)
            self.log(f"Claimed the sale's free sticker{f' ({name})' if name else ''}", "gems")


class Worth:
    """What the cards you hold would sell for, from prices Card Farmer's Market already looked up
    (and one more game now and then), and what the items in your chosen inventories would, from the
    last Check items."""

    def _hold(self, cards):
        """Remember how many of each card you hold (from an inventory read)."""
        held = {}
        for c in cards:
            h = held.setdefault(c["hash"], [c["appid"], 0])
            h[1] += 1
        with self.lock:
            self.state["holding"] = held

    def _card_price(self, appid, hash_name):
        """A card's last known lowest price, however old (prices are kept two weeks)."""
        hit = self.prices.cache.get(f"cards:{appid}")
        return (hit or {}).get("v", {}).get(hash_name)

    def _worth_chore(self):
        """Price one game of the cards you hold whose prices are unknown or a day old."""
        if time.time() < getattr(self, "_next_worth", 0):
            return
        self._next_worth = time.time() + WORTH_EVERY
        with self.lock:
            games = {}
            for appid, n in self.state["holding"].values():
                games[appid] = games.get(appid, 0) + n
        for appid in sorted(games, key=lambda a: -games[a]):
            hit = self.prices.cache.get(f"cards:{appid}")
            if not hit or time.time() - hit["at"] > WORTH_STALE:
                self.prices.game_cards(appid)
                return

    def worth(self):
        """{"cards": {"cents", "priced", "of"}, "items": {"cents", "priced", "of", "at"}}: what you'd
        get selling each at today's price (after Steam's fees)."""
        cents = priced = total = 0
        for hash_name, (appid, n) in self.state["holding"].items():
            total += n
            price = self._card_price(appid, hash_name)
            if price:
                cents += rules.received(rules.asking_price(price, self.settings)) * n
                priced += n
        items = self.state["items"].get("worth") or {"cents": 0, "priced": 0, "of": 0, "at": 0}
        return {"cards": {"cents": cents, "priced": priced, "of": total}, "items": items}


class Alerts:
    """Price alerts: a notice when something sells for at least an amount, or is listed for at most
    one. Each fires once, then again only after the price has been back on the other side."""

    def search_alerts(self, text):
        text = (text or "").strip()
        if len(text) < 2:
            raise ValueError("type at least 2 letters")

        def go():
            try:
                found = steam.search_market(self.prices, text)
            except Exception as e:
                self.log(f"Couldn't search the market: {e}", "warn")
                found = []
            with self.lock:
                self.state["alert_search"] = {"text": text, "results": found, "at": time.time()}
        self._in_background("alert-search", go)

    def add_alert(self, item, when, cents):
        if when not in ("above", "below"):
            raise ValueError("an alert is for above or below a price")
        cents = int(cents)
        if cents < 1:
            raise ValueError("pick a price for the alert")
        hash_name, appid = str(item["hash"]), int(item["appid"])
        alert = {"id": f"{appid}/{hash_name}/{when}", "appid": appid, "hash": hash_name,
                 "name": str(item.get("name") or hash_name)[:200], "game": str(item.get("game") or "")[:200],
                 "icon": str(item.get("icon") or ""), "when": when, "cents": cents, "armed": True,
                 "price": None, "lowest": None, "checked": 0, "fired": 0, "added": time.time()}
        with self.lock:
            self.state["alerts"] = [a for a in self.state["alerts"] if a["id"] != alert["id"]] + [alert]
        self._next_alerts = 0
        self.poke()

    def remove_alert(self, alert_id):
        with self.lock:
            self.state["alerts"] = [a for a in self.state["alerts"] if a["id"] != alert_id]

    def _alerts_chore(self):
        if not self.state["alerts"] or time.time() < getattr(self, "_next_alerts", 0):
            return
        self._next_alerts = time.time() + ALERTS_EVERY
        for a in list(self.state["alerts"]):
            stats = self.prices.item_stats(a["appid"], a["hash"], max_age=ALERT_FRESH)
            sells, lowest = rules.realistic(stats), stats.get("lowest")
            hit = (sells is not None and sells >= a["cents"]) if a["when"] == "above" else (lowest is not None and lowest <= a["cents"])
            with self.lock:
                a.update(price=sells, lowest=lowest, checked=time.time())
                fire, a["armed"] = hit and a["armed"], not hit
                if fire:
                    a["fired"] = time.time()
            if fire:
                text = (f"{a['name']} sells for {_money(sells)} now (your alert: {_money(a['cents'])} or more)" if a["when"] == "above"
                        else f"{a['name']} is listed for {_money(lowest)} now (your alert: {_money(a['cents'])} or less)")
                self.log(f"Price alert: {text}", "sell")
                self.notify("Price alert", text)


def _money(c):
    return f"${c / 100:.2f}"
