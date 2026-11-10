"""Settings, and what to do with a card (sell it, turn it into gems, or keep it) or another item."""
import re

from . import files

DEFAULTS = {
    "theme": "auto",             # auto (the system's light or dark) | light | dark | pink
    "farming": True,
    "order": "value",            # value: priciest cards first | most: most drops left | fewest: closest to done
    "pause_when_playing": True,  # step aside while you play something yourself
    "stall_minutes": 90,         # no drop for this long: restart the idler, then move on (0: never)
    "skip_free_to_play": True,   # free-to-play games (CS2) drop cards for in-game purchases while played, not idled
    "sell": "auto",              # auto: list it | review: ask first | off: never sell
    "gems": "review",            # auto | review | off
    "pricing": "match",          # match the lowest listing, or undercut it by a cent
    "min_sell_cents": 3,         # cheaper than this (what a buyer pays): gems instead
    "foils": "sell",             # sell (by the rules above) | keep
    "never_touch": [],           # appids whose cards are kept, e.g. sets you're crafting
    "notify": True,              # desktop notification when listings wait for phone confirmation
    # other items (skins, cases, ...): nothing happens to them unless you ask
    "items_new": "off",          # new items in the chosen inventories: off | review | auto (list them)
    "items_inventories": [],     # "appid/context" of inventories to look in, e.g. "730/2"
    "items_min_cents": 5,        # don't list an item for less than this (what a buyer pays)
    "items_keep": [],            # market names never listed, e.g. a skin you use
    "items_min_sales": 1,        # only offer items that sold at least this many times in the last day (0: any)
    # your listings
    "reprice_after_hours": 24,   # only reprice listings at least this old
    "reprice_gap_cents": 2,      # ...and only when someone sells for more than this below yours
    "cancel_after_days": 0,      # cancel listings unsold this long (0: never); they come back for review
    "notify_sales": True,        # desktop notification when things sell
    "booster_min_sales": 2,      # a booster pack is only worth making if at least this many sold in the last day
    "levelup_target": 0,         # Steam level to reach (0: the next one)
    "levelup_budget_cents": 100, # most to spend on one level-up plan
    "levelup_buying": "now",     # now: at the cheapest listings | orders: buy orders a bit under them, filling over time
    "order_days": 7,             # buy orders still unfilled after this many days are cancelled (0: never)
    "set_budget_cents": 50,      # most to spend buying the missing cards of one set
}
MODES = ("auto", "review", "off")


def load_settings(path):
    s = dict(DEFAULTS)
    saved = files.read_json(path, backup=True)
    if isinstance(saved, dict):
        s.update(clean(saved))  # settings that are gone (games_at_once) are dropped
    return s


def clean(patch):
    """Only known settings, with sane values."""
    out = {}
    for k, v in patch.items():
        if k not in DEFAULTS:
            continue
        d = DEFAULTS[k]
        if isinstance(d, bool):
            out[k] = bool(v)
        elif k in ("stall_minutes", "min_sell_cents", "items_min_cents", "reprice_after_hours", "reprice_gap_cents",
                   "cancel_after_days", "set_budget_cents", "booster_min_sales", "items_min_sales", "levelup_target",
                   "levelup_budget_cents", "order_days"):
            out[k] = max(0, int(v))
        elif k in ("sell", "gems", "items_new") and v in MODES:
            out[k] = v
        elif k == "order" and v in ("value", "most", "fewest"):
            out[k] = v
        elif k == "pricing" and v in ("match", "undercut"):
            out[k] = v
        elif k == "theme" and v in ("auto", "light", "dark", "pink"):
            out[k] = v
        elif k == "levelup_buying" and v in ("now", "orders"):
            out[k] = v
        elif k == "foils" and v in ("sell", "keep"):
            out[k] = v
        elif k == "never_touch":
            out[k] = sorted({int(a) for a in v})
        elif k == "items_inventories":
            out[k] = sorted({str(i) for i in v if re.fullmatch(r"\d+/\d+", str(i))})
        elif k == "items_keep":
            out[k] = sorted({str(n)[:200] for n in v})
    return out


def fees(receive_cents):
    """Steam's 5% and the game's 10% on top of what the seller gets, each at least 1c."""
    return max(1, receive_cents * 5 // 100) + max(1, receive_cents * 10 // 100)


def received(buyer_cents):
    """What the seller gets when a buyer pays at most this."""
    r = buyer_cents
    while r > 0 and r + fees(r) > buyer_cents:
        r -= 1
    return r


def buyer_pays(receive_cents):
    """What the listing then shows buyers (can be a cent under the price aimed for)."""
    return receive_cents + fees(receive_cents)


def asking_price(lowest_cents, settings):
    """What to ask buyers given the lowest listing: match it, or undercut it by a cent."""
    if not lowest_cents:
        return None
    return lowest_cents - 1 if settings["pricing"] == "undercut" and lowest_cents > 3 else lowest_cents


def decide(card, lowest_cents, gems, sack_cents, settings):
    """(action, detail) where action is sell / gems / keep.

    sell: detail = {"buyer": cents the listing asks, "receive": cents you get}
    gems: detail = {"gems": n}
    keep: detail = {"why": text}
    """
    if card["appid"] in settings["never_touch"]:
        return "keep", {"why": "game is on your keep list"}
    if card["foil"] and settings["foils"] == "keep":
        return "keep", {"why": "foils are kept"}
    if not card.get("marketable", True):
        return ("gems", {"gems": gems}) if gems else ("keep", {"why": "can't be sold or turned into gems"})
    gem_value = gems * sack_cents / 1000 / 1.15  # what those gems fetch once sold as a sack
    buyer = asking_price(lowest_cents, settings)
    if buyer and buyer >= settings["min_sell_cents"] and received(buyer) > 0 \
            and (received(buyer) > gem_value or not gems or card["foil"]):
        return "sell", {"buyer": buyer, "receive": received(buyer), "gem_value": round(gem_value, 2), "gems": gems}
    if gems:
        return "gems", {"gems": gems, "gem_value": round(gem_value, 2), "buyer": buyer,
                        "receive": received(buyer) if buyer else None}
    return "keep", {"why": "nobody is selling it and it isn't worth gems"}


def realistic(stats):
    """What something really sells for: the lower of its cheapest listing and its median sale over the
    last day (a stale, high asking price doesn't count). None when there's neither."""
    prices = [p for p in (stats.get("lowest"), stats.get("median")) if p]
    return min(prices) if prices else None


def decide_item(item, stats, settings):
    """('sell', {buyer, receive, volume, median}) or ('keep', {why, ...}) for an item that isn't a card.
    stats: {"lowest", "median", "volume"} from the market."""
    if item["hash"] in settings["items_keep"]:
        return "keep", {"why": "on your keep list"}
    seen = {"volume": stats.get("volume", 0), "median": stats.get("median")}
    buyer = asking_price(realistic(stats), settings)
    if not buyer:
        return "keep", {"why": "nobody is selling one", "buyer": None, **seen}
    if stats.get("volume", 0) < settings["items_min_sales"]:
        return "keep", {"why": "doesn't sell (too few sales in the last day)", "buyer": buyer, **seen}
    if buyer < settings["items_min_cents"] or received(buyer) < 1:
        return "keep", {"why": "under your minimum", "buyer": buyer, "receive": received(buyer), **seen}
    return "sell", {"buyer": buyer, "receive": received(buyer), **seen}
