"""Quick badge: pick one game and take its badge to the top level in one go. Your badges (started,
not crafted yet, finished), the market's cheapest sets, and a search for any game with cards; for
the game picked, every card's order book (how many are listed at each price), so the cost of each
level is what buying those exact listings comes to. Buying and crafting only happen when the person
presses the button, through the farm's saved queue."""
import time

from . import http, steam

TOP = 5  # a game's normal badge goes from level 1 to 5; each level takes one full set of its cards
BOOK_TTL = 15 * 60  # a card's listings, read for pricing, are reused this long (a retry goes on where it stopped)
WAIT_AT_MOST = 40 * 60  # pricing waits out Steam's "slow down" pauses this long in all before giving up


def quick_state():
    return {"badges": {}, "level": 0, "xp_to_next": 0, "checked": 0,
            "names": {},  # appid -> game name, for badges of games the badges page doesn't list
            "search": {"text": "", "results": [], "at": 0},
            "pick": None}


def buy_lots(sell, n):
    """The n cheapest listings of an order book [[price, count], ...]: [[price, how many]], or None
    when fewer than n are listed."""
    lots, left = [], n
    for price, count in sell:
        if left <= 0:
            break
        take = min(count, left)
        lots.append([price, take])
        left -= take
    return None if left > 0 else lots


def badge_plan(pick, target):
    """What taking the picked game's badge from its level to `target` takes: for each card, how many
    to buy (what you hold counts) and which listings; the total, and what's short."""
    levels = max(0, min(target, TOP) - pick["level"])
    cards, cost, short = [], 0, []
    for c in pick["cards"]:
        need = max(0, levels - c["have"])
        lots = buy_lots(c["sell"], need) if need else []
        if lots is None:
            short.append(c["name"])
            lots = buy_lots(c["sell"], sum(n for _, n in c["sell"])) or []
        spend = sum(p * n for p, n in lots)
        cost += spend
        cards.append({"hash": c["hash"], "name": c["name"], "need": need, "lots": lots, "cost": spend})
    return {"target": pick["level"] + levels, "levels": levels, "cost": cost, "cards": cards,
            "buying": sum(c["need"] for c in cards), "short": short}


def spend_limit(cost):
    """How far over the quote a purchase may go when prices move while buying."""
    return cost + max(5, round(cost * 0.1))


class QuickBadge:
    """Mixed into Farm (uses its state, lock, session, prices, log and queue, and the market
    parts' buy orders)."""

    def _quick(self):
        return self.state.setdefault("quick", quick_state())

    def _books(self):
        """Order books read lately, by card (kept in memory only)."""
        if not hasattr(self, "_book_cache"):
            self._book_cache = {}
        return self._book_cache

    # --- your badges ----------------------------------------------------------------------------
    def check_badges(self):
        self._in_background("badges", self._check_badges)

    def _check_badges(self):
        try:
            me = steam.player_badges(self.session)
            with self.lock:
                q = self._quick()
                q.update(badges={str(a): lv for a, lv in me["badges"].items()}, level=me["level"],
                         xp_to_next=me["xp_to_next"], checked=time.time())
        except Exception as e:
            self.log(f"Couldn't read your badges: {e}", "warn")

    def _badge_name(self, appid):
        q = self._quick()
        return (next((g["name"] for g in self.state["games"] if g["appid"] == appid), None)
                or q["names"].get(str(appid)) or getattr(self, "_names", {}).get(appid) or f"App {appid}")

    def quick_rows(self):
        """The lists on the page: started badges (closest to the top first), games you have cards for
        but no badge yet, the market's cheapest sets (from Level up's search), finished badges, and
        what the last search found. A set's price shows when it's been looked up lately."""
        q = self._quick()
        levels = {int(a): lv for a, lv in q["badges"].items()}

        def row(appid, game=None, one_set=None):
            if one_set is None:
                cards = self.prices.known_cards(appid)
                normal = [p for h, p in (cards or {}).items() if "(Foil" not in h]
                one_set = sum(normal) if normal and all(normal) else None
            return {"appid": appid, "game": game or self._badge_name(appid), "level": levels.get(appid, 0), "set": one_set}
        started = sorted((row(a) for a, lv in levels.items() if 0 < lv < TOP), key=lambda r: (-r["level"], r["game"].lower()))
        maxed = sorted((row(a) for a, lv in levels.items() if lv >= TOP), key=lambda r: r["game"].lower())
        new = sorted((row(g["appid"], g["name"]) for g in self.state["games"] if not levels.get(g["appid"])),
                     key=lambda r: r["game"].lower())
        cheap, seen = [], set()
        for o in sorted(self.state["levelup"].get("options", []), key=lambda o: o["cost"]):
            if o["source"] == "market" and o["appid"] not in seen and levels.get(o["appid"], 0) < TOP:
                seen.add(o["appid"])
                cheap.append(row(o["appid"], o["game"], o["cost"]))
        found = [dict(row(r["appid"], r["game"]), cheapest=r["cheapest"]) for r in q["search"]["results"]]
        return {"started": started, "new": new, "cheap": cheap, "maxed": maxed, "found": found}

    # --- finding a game ----------------------------------------------------------------------------
    def search_badges(self, text):
        text = " ".join(str(text or "").split())[:80]
        if len(text) < 2:
            raise ValueError("type at least two letters of the game's name")
        self._in_background("badge-search", lambda: self._search_badges(text))

    def _search_badges(self, text):
        try:
            found = steam.search_card_games(self.prices, text)
            with self.lock:
                q = self._quick()
                q["search"] = {"text": text, "results": found, "at": time.time()}
                q["names"].update({str(r["appid"]): r["game"] for r in found})
            if not found:
                self.log(f"No game with trading cards matches \"{text}\" on the market")
        except Exception as e:
            self.log(f"Couldn't search the market for \"{text}\": {e}", "warn")

    # --- one game, priced listing by listing ----------------------------------------------------------
    def price_badge(self, appid, game=None):
        with self.lock:
            if game:
                self._quick()["names"][str(appid)] = game
        self._in_background("badge", lambda: self._price_badge(appid))

    def _price_badge(self, appid):
        """The badge's level and the set's cards (from your card page), what you hold of each (cards
        listed and waiting for the Steam app don't count), and each card's order book."""
        game = self._badge_name(appid)
        try:
            self.progress["badge"] = f"Reading your {game} badge…"
            page = steam.card_set(self.session, appid)
            hashes = sorted(h for h in self.prices.game_cards(appid) if "(Foil" not in h)
            if not hashes:
                raise RuntimeError("Steam's market has none of its cards")
            pending = steam.pending_listings(self.session)
            held = {}
            for c in steam.cards_in_inventory(self.session):
                if c["appid"] == appid and not c["foil"] and c["assetid"] not in pending:
                    held[c["hash"]] = held.get(c["hash"], 0) + 1
            cards, wallet, waited = [], None, 0
            for i, h in enumerate(hashes):
                self.progress["badge"] = f"{game}: listings for card {i + 1} of {len(hashes)}"
                book = self._books().get(h)
                if book and time.time() - book["at"] < BOOK_TTL:
                    book = book["v"]
                else:
                    book, waited = self._read_book(game, h, i, len(hashes), waited)
                wallet = book["wallet"] if book["wallet"] is not None else wallet
                cards.append({"hash": h, "name": h.split("-", 1)[1].removesuffix(" (Trading Card)"),
                              "have": held.get(h, 0), "sell": book["sell"], "listed": book["listed"]})
            with self.lock:
                self._quick()["pick"] = {"appid": appid, "game": game, "level": page["level"], "cards": cards,
                                         "set_size": len(page["cards"]) or len(cards), "wallet": wallet, "checked": time.time()}
                self._quick()["badges"][str(appid)] = page["level"]
            plan = badge_plan(self._quick()["pick"], TOP)
            self.log(f"{game}: badge level {page['level']}; to level {TOP} costs ${plan['cost'] / 100:.2f} for {plan['buying']} cards"
                     if plan["levels"] else f"{game}: the badge is already at level {page['level']}")
        except steam.HttpError as e:
            self.log(f"Couldn't price the {game} badge: " + ("Steam asked to slow down; try again in a few minutes"
                                                             if e.status == 429 else str(e)), "warn")
        except Exception as e:
            self.log(f"Couldn't price the {game} badge: {e}", "warn")
        finally:
            self.progress.pop("badge", None)

    def _read_book(self, game, hash_name, i, of, waited):
        """One card's order book; when Steam asks to slow down, wait the pause out (saying so on the
        page) and ask again, up to WAIT_AT_MOST in all. Returns (book, time waited so far)."""
        while True:
            try:
                book = steam.order_book(self.session, self.prices, hash_name, tries=1)
                self._books()[hash_name] = {"at": time.time(), "v": book}
                return book, waited
            except steam.HttpError as e:
                until = http.paused_until(e.url or f"{steam.COMMUNITY}/") or time.time() + 60
                if e.status != 429 or waited + (until - time.time()) > WAIT_AT_MOST:
                    raise
                self.progress["badge"] = (f"{game}: {i} of {of} cards read; Steam asked to slow down, "
                                          f"going on at {time.strftime('%H:%M', time.localtime(until))}")
                pause = max(5.0, until - time.time() + 5)
                time.sleep(pause)
                waited += pause

    def quick_plans(self):
        """For the page: the picked game and a plan for each level it could be taken to."""
        pick = self._quick()["pick"]
        if not pick:
            return None
        plans = [badge_plan(pick, t) for t in range(pick["level"] + 1, TOP + 1)]
        return dict(pick, plans=plans, missing=max(0, pick["set_size"] - len(pick["cards"])))

    # --- buying and crafting ---------------------------------------------------------------------------
    def quick_badge(self, appid, target):
        """Queue the purchase and the crafts for the picked game, as priced."""
        with self.lock:
            pick = self._quick()["pick"]
            if not pick or pick["appid"] != appid:
                raise ValueError("price that game first")
            if time.time() - pick["checked"] > 30 * 60:
                raise ValueError("those prices are over half an hour old; price it again first")
            if pick["set_size"] > len(pick["cards"]):
                raise ValueError("some of its cards aren't on the market, so the set can't be bought")
            plan = badge_plan(pick, target)
            if not plan["levels"]:
                raise ValueError("the badge is already at that level")
            if plan["short"]:
                raise ValueError(f"not enough listed of {', '.join(plan['short'][:3])}")
            if pick["wallet"] is not None and spend_limit(plan["cost"]) > pick["wallet"]:
                raise ValueError(f"that may cost up to ${spend_limit(plan['cost']) / 100:.2f}; "
                                 f"your Steam wallet has ${pick['wallet'] / 100:.2f}")
            # from now until the badge is crafted, this game's cards aren't sold or ground
            self.state["finishing"][str(appid)] = {"game": pick["game"], "since": time.time()}
            self._quick()["pick"] = None  # bought from; price it again afterwards
        self._enqueue("quick", {"id": f"quick-{appid}-{int(time.time())}", "kind": "quick", "appid": appid,
                                "game": pick["game"], "from": pick["level"], "target": plan["target"],
                                "cards": plan["cards"], "cost": plan["cost"], "limit": spend_limit(plan["cost"])})
        self.log(f"Quick badge: {pick['game']} to level {plan['target']}, {plan['buying']} cards for about ${plan['cost'] / 100:.2f}")
        self.save()

    def _buy_many(self, hash_name, price, quantity):
        """Buy `quantity` at up to `price` each through one buy order; how many came in. Whatever
        hasn't filled within a few seconds is cancelled (those listings sold to someone else).
        Returns once Steam has closed the order, so the next one for the same card can go in."""
        order = self._order_for(hash_name, price, quantity, job="badge")
        got = 0
        for _ in range(8):
            time.sleep(2)
            try:
                st = steam.buy_order_status(self.session, order["orderid"], hash_name)
                if st.get("quantity_remaining") is not None:
                    got = quantity - int(st["quantity_remaining"])
                elif st.get("purchased") and not st.get("active"):
                    got = quantity
            except steam.HttpError:
                # Steam's status check turns away orders that filled at once (see MarketParts._buy)
                still = steam.buy_orders(self.session)
                got = quantity - still[order["orderid"]] if order["orderid"] in still else quantity
            if got >= quantity:
                self._until_closed(order["orderid"])
                return quantity
        if not self._cancel_rest(order["orderid"]):  # nothing left to cancel: the rest filled just now
            got = quantity
        self._until_closed(order["orderid"])
        return got

    def _do_quick(self, e):
        """Buy each card's listings as priced; a card whose listings sold meanwhile is priced again
        and bought at the next ones, as long as the whole stays within the limit. Then craft each
        level."""
        appid, game, spent, bought = e["appid"], e["game"], 0, 0
        level = steam.card_set(self.session, appid)["level"]
        crafts = e["target"] - level
        if crafts <= 0:
            with self.lock:
                self.state["finishing"].pop(str(appid), None)
            raise RuntimeError(f"the {game} badge is already at level {level}")
        try:
            for n, c in enumerate(e["cards"]):
                got = 0
                for price, count in c["lots"]:
                    k = self._buy_many(c["hash"], price, count)
                    got, spent = got + k, spent + price * k
                if got < c["need"]:  # some sold first: the next listings, if the whole still fits
                    lots = buy_lots(steam.order_book(self.session, self.prices, c["hash"])["sell"], c["need"] - got)
                    rest = sum(x["cost"] for x in e["cards"][n + 1:])
                    more = sum(p * k for p, k in lots or [])
                    if lots is None or spent + more + rest > e["limit"]:
                        raise RuntimeError(f"not enough {c['name']} left within the limit")
                    for price, count in lots:
                        k = self._buy_many(c["hash"], price, count)
                        got, spent = got + k, spent + price * k
                    if got < c["need"]:
                        raise RuntimeError(f"couldn't buy enough {c['name']}")
                bought += got
                if got:
                    self.log(f"Bought {got}× {c['name']} ({game})")
        except Exception as err:
            self.log(f"Quick badge for {game} stopped after {bought} cards (about ${spent / 100:.2f}): {err}. "
                     "Its cards are kept for the set: price it again to buy the rest, or stop finishing it under Finish sets on the Badges tab", "warn")
            raise
        done = 0
        for _ in range(crafts):
            for attempt in range(6):
                try:
                    steam.craft_badge(self.session, appid)
                    done += 1
                    break
                except (RuntimeError, steam.HttpError):
                    if attempt == 5:
                        raise RuntimeError(f"crafted {done} of {crafts} levels; Steam wouldn't craft the next")
                    time.sleep(5)  # bought cards take a moment to reach the inventory
        with self.lock:
            self.state["finishing"].pop(str(appid), None)
            self._quick()["badges"][str(appid)] = level + done
        self.log(f"Crafted the {game} badge to level {level + done}"
                 + (f" (bought {bought} cards for about ${spent / 100:.2f})" if bought else ""), "gems")
        return 0
