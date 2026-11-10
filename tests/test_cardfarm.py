import base64
import hashlib
import json
import pathlib
import sys
import threading
import time
import urllib.parse

import pytest

from cardfarm import api, farm as farm_mod, idler, market as market_mod, qr, rules, steam

HERE = pathlib.Path(__file__).parent


# --- rules ---------------------------------------------------------------------------------
def card(**kw):
    return dict({"assetid": "1", "appid": 10, "name": "Hero", "game": "Game", "hash": "10-Hero",
                 "foil": False, "marketable": True, "icon": "", "goo": None}, **kw)


S = dict(rules.DEFAULTS)


def test_received_matches_steam_fees():
    # what a buyer pays -> what the seller gets
    assert [rules.received(c) for c in (3, 10, 18, 92, 115)] == [1, 8, 16, 80, 100]
    # 22c can't be hit exactly: the seller gets 19c and buyers see 21c
    assert rules.received(22) == 19 and rules.buyer_pays(19) == 21


def test_sells_when_the_market_beats_gems():
    action, d = rules.decide(card(), 18, 80, 70, S)
    assert action == "sell" and d["buyer"] == 18 and d["receive"] == 16


def test_gems_when_nobody_sells_or_too_cheap():
    assert rules.decide(card(), None, 20, 70, S)[0] == "gems"
    assert rules.decide(card(), 2, 20, 70, S)[0] == "gems"       # under min_sell_cents
    rich_gems = rules.decide(card(), 4, 2000, 70, S)              # 2000 gems are worth more than 2c
    assert rich_gems[0] == "gems" and rich_gems[1]["receive"] == 2  # the page can still show a sale


def test_keep_rules():
    assert rules.decide(card(appid=7), 50, 20, 70, dict(S, never_touch=[7]))[0] == "keep"
    assert rules.decide(card(foil=True), 90, 20, 70, dict(S, foils="keep"))[0] == "keep"
    assert rules.decide(card(), None, 0, 70, S)[0] == "keep"


def test_undercut_by_a_cent():
    assert rules.decide(card(), 18, 0, 70, dict(S, pricing="undercut"))[1]["buyer"] == 17


def test_settings_are_cleaned():
    assert rules.clean({"sell": "auto", "gems": "maybe", "games_at_once": 99, "bogus": 1,
                        "never_touch": ["5", 5, 6]}) == {"sell": "auto", "never_touch": [5, 6]}
    assert rules.clean({"items_inventories": ["730/2", "nope", "440/2"], "items_new": "auto"}) == \
        {"items_inventories": ["440/2", "730/2"], "items_new": "auto"}


def test_items_are_left_alone_by_default():
    assert rules.DEFAULTS["items_new"] == "off" and rules.DEFAULTS["items_inventories"] == []


def test_item_rules():
    item = {"hash": "Case", "name": "Case"}
    st = lambda lowest, median=None, volume=5: {"lowest": lowest, "median": median, "volume": volume}
    assert rules.decide_item(item, st(119), S) == ("sell", {"buyer": 119, "receive": 104, "volume": 5, "median": None})
    assert rules.decide_item(item, st(4), S)[0] == "keep"              # under items_min_cents
    assert rules.decide_item(item, st(None), S)[0] == "keep"           # nobody sells one
    assert rules.decide_item(item, st(119), dict(S, items_keep=["Case"]))[0] == "keep"
    assert rules.decide_item(item, st(119, volume=0), S)[0] == "keep"  # nobody bought one lately
    assert rules.decide_item(item, st(119, median=80), S)[1]["buyer"] == 80   # sales went for less than the ask


# --- badges page ---------------------------------------------------------------------------------
def test_badges_page():
    page = (HERE / "badges_page.html").read_text()
    assert steam.parse_badges(page) == [
        {"appid": 440, "name": "Team & Fortress", "drops": 3, "earned": 4},
        {"appid": 620, "name": "Portal Two", "drops": 1, "earned": 1},
        {"appid": 730, "name": "Strike", "drops": 0, "earned": 0},
    ]
    assert steam.page_count(page) == 2


# --- QR -----------------------------------------------------------------------------------------
def test_qr_shape():
    m = qr.matrix("https://s.team/q/1/2625894210510258091")
    assert len(m) == 29 and all(len(r) == 29 for r in m)  # version 3
    finder = [r[:7] for r in m[:7]]
    assert finder[0] == [True] * 7 and finder[1] == [True] + [False] * 5 + [True]
    assert qr.svg("hi").startswith("<svg")


def test_qr_unchanged():
    # these exact codes were read back correctly by zbar; any change to the encoder must be re-checked
    def digest(text):
        return hashlib.sha256("".join("1" if d else "0" for r in qr.matrix(text) for d in r).encode()).hexdigest()[:16]
    assert digest("hi") == HI and digest("y" * 100) == LONG


HI = "69f439bee9c38226"
LONG = "058cebbfc8fda720"


# --- the window's requests ------------------------------------------------------------------------
class PageFarm:
    def __init__(self):
        self.settings = {}
        self.decided = None

    def snapshot(self):
        return {"ok": True}

    def update_settings(self, patch):
        self.settings.update(patch)

    def decide_review(self, ids, action, items=False):
        self.decided = (ids, action, items)


def test_page_and_state():
    status, kind, body = api.answer(PageFarm(), "GET", "/", b"")
    assert status == 200 and kind.startswith("text/html") and b"Card Farm" in body
    assert api.answer(PageFarm(), "GET", "/api/state", b"")[2] == b'{"ok": true}'
    assert api.answer(PageFarm(), "GET", "/nope", b"")[0] == 404


def test_actions():
    f = PageFarm()
    assert api.answer(f, "POST", "/api/settings", b'{"sell": "off"}')[0] == 200 and f.settings == {"sell": "off"}
    assert api.answer(f, "POST", "/api/items/review", b'{"ids": ["9"], "action": "always"}')[0] == 200
    assert f.decided == ({"9"}, "always", True)
    assert api.answer(f, "POST", "/api/review", b'{"ids": "all", "action": "always"}')[0] == 400  # items only
    assert api.answer(f, "POST", "/api/bogus", b"{}")[0] == 400


# --- the farm ----------------------------------------------------------------------------------
class FakeIdler:
    def __init__(self, farm=None):
        self.procs, self.farm, self.lock_free, self.library = set(), farm, [], "lib"

    def running(self):
        return set(self.procs)

    def start(self, appids):
        if self.farm:  # starting games must not hold the lock the window reads through
            def probe():
                ok = self.farm.lock.acquire(timeout=0.2)
                if ok:
                    self.farm.lock.release()
                self.lock_free.append(ok)
            t = threading.Thread(target=probe)
            t.start(); t.join()
        self.procs |= set(appids)
        return set()

    def stop(self, appid):
        self.procs.discard(appid)

    def stop_all(self):
        self.procs.clear()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A signed-in farm whose Steam is a dict we control."""
    (tmp_path / "session.json").write_text(json.dumps({"steamid": "7656", "refresh_token": "x", "access_token": "y"}))
    w = {"cards": [], "pending": set(), "badges": [], "inventories": [], "items": {}, "sold": []}
    monkeypatch.setattr(steam, "cards_in_inventory", lambda s, stock=None: list(w["cards"]))
    monkeypatch.setattr(steam, "pending_listings", lambda s: set(w["pending"]))
    monkeypatch.setattr(steam, "badges", lambda s: [dict(g) for g in w["badges"]])
    monkeypatch.setattr(steam, "inventories", lambda s: list(w["inventories"]))
    monkeypatch.setattr(steam, "items_in_inventory", lambda s, a, c, n="": list(w["items"].get(f"{a}/{c}", [])))
    monkeypatch.setattr(steam, "sell", lambda s, asset, receive, *a: w["sold"].append((asset, receive)))

    def make():
        f = farm_mod.Farm(str(tmp_path))
        f.idler = FakeIdler(f)
        f.prices.game_cards = lambda appid: {"10-Hero": 50}
        f.prices.gems = lambda *a: 20
        f.prices.gem_sack = lambda: 70
        f.prices.item = lambda appid, name: 119
        f.prices.item_stats = lambda appid, name: {"lowest": 119, "median": 119, "volume": 9}
        f.update_settings({"sell": "auto"})
        return f
    w["make"] = make
    return w


def test_only_real_drops_are_sold_automatically(world):
    f = world["make"]()
    world["badges"] = [{"appid": 10, "name": "Game", "drops": 3, "earned": 3}]
    f._refresh_badges()
    f._check_inventory()                       # first look: whatever is there is left alone
    world["cards"] = [card(assetid="1")]
    world["badges"] = [{"appid": 10, "name": "Game", "drops": 2, "earned": 3}]
    f._refresh_badges(); f._check_inventory()  # a drop: sold by the rules
    assert [j["entry"]["id"] for j in f.state["queue"]] == ["1"] and f.state["stats"]["drops"] == 1
    world["cards"].append(card(assetid="2"))   # turned up without a drop (bought for a badge?)
    f._refresh_badges(); f._check_inventory()
    assert f.state["review"] == []             # first it waits for the badges page to catch up
    f.state["unexplained"]["2"] -= farm_mod.EXPLAIN_WAIT
    f._refresh_badges(); f._check_inventory()
    assert [j["entry"]["id"] for j in f.state["queue"]] == ["1"]
    assert [e["id"] for e in f.state["review"]] == ["2"] and f.state["stats"]["drops"] == 1


def test_queue_survives_a_restart(world):
    f = world["make"]()
    f._enqueue("sell", {"id": "1", "kind": "card", "card": card(), "detail": {}})
    f.save()
    g = world["make"]()
    assert [j["entry"]["id"] for j in g.state["queue"]] == ["1"]


def test_sorting_skips_listed_kept_and_gone_cards(world):
    f = world["make"]()
    f.state["baseline"] = True
    world["cards"] = [card(assetid=a) for a in "123"]
    world["pending"] = {"2"}                       # listed, waiting for the phone
    f.decide_review(set(), "keep")
    f.state["kept"] = ["3"]
    f._review_existing()
    assert [e["id"] for e in f.state["review"]] == ["1"]
    world["cards"] = []                            # sold elsewhere
    f._check_inventory()
    assert f.state["review"] == [] and f.state["kept"] == []


def test_another_account_starts_fresh(world):
    f = world["make"]()
    f.state.update(seen=["1"], baseline=True, review=[{"id": "1"}])
    f.session.steamid = "other"
    f._for_account()
    assert f.state["seen"] == [] and not f.state["baseline"] and f.state["steamid"] == "other"


def test_skipped_games_stay_on_the_page(world):
    f = world["make"]()
    f.state["games"] = [{"appid": 10, "name": "Game", "drops": 3, "earned": 3}]
    f.skip(10)
    games = f.snapshot()["games"]
    assert [(g["appid"], g["skipped"]) for g in games] == [(10, True)]


def test_games_start_without_holding_the_lock(world):
    f = world["make"]()
    f.state["games"] = [{"appid": 10, "name": "Game", "drops": 3, "earned": 3}]
    f._choose_games()
    assert f.idler.lock_free == [True] and list(f.state["idling"]) == ["10"]


def test_items_wait_for_you_unless_auto(world):
    f = world["make"]()
    world["inventories"] = [{"key": "730/2", "appid": 730, "context": 2, "name": "CS2", "count": 2}]
    case = {"assetid": "c1", "appid": 730, "context": 2, "amount": 1, "name": "Case", "hash": "Case",
            "game": "CS2", "icon": "", "kind": "Container"}
    world["items"]["730/2"] = [case]
    f._check_items(manual=True)                       # no inventory switched on: only lists them
    assert f.state["items"]["review"] == [] and f.state["items"]["inventories"][0]["key"] == "730/2"
    f.update_settings({"items_inventories": ["730/2"], "items_new": "auto"})
    f._check_items(manual=False)                      # first look at this inventory: nothing happens
    assert f.state["queue"] == [] and f.state["items"]["review"] == []
    world["items"]["730/2"] = [case, dict(case, assetid="c2")]
    f._check_items(manual=False)                      # a new one, with Auto on: listed
    assert [j["entry"]["id"] for j in f.state["queue"]] == ["c2"]
    f._check_items(manual=True)                       # the older one waits for review
    assert [e["id"] for e in f.state["items"]["review"]] == ["c1"]
    f.decide_review({"c1"}, "always", items=True)
    assert f.settings["items_keep"] == ["Case"] and f.state["items"]["review"] == []


def test_listing_uses_todays_price(world):
    f = world["make"]()
    f._do("sell", {"kind": "card", "card": card(), "detail": {"buyer": 500}})
    assert world["sold"] == [("1", rules.received(50))]


# --- finding Steam on each system -------------------------------------------------------------
def test_library_folders_are_read():
    vdf = '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"C:\\\\Program Files (x86)\\\\Steam"\n\t}\n' \
          '\t"1"\n\t{\n\t\t"path"\t\t"D:\\\\SteamLibrary"\n\t}\n}\n'
    assert idler.parse_library_folders(vdf) == ["C:\\Program Files (x86)\\Steam", "D:\\SteamLibrary"]


def test_packaged_app_is_its_own_idler(monkeypatch):
    monkeypatch.setattr(idler.sys, "frozen", True, raising=False)
    monkeypatch.setattr(idler.sys, "executable", "/Applications/Card Farm.app/Contents/MacOS/Card Farm")
    assert idler.child_command("lib") == ["/Applications/Card Farm.app/Contents/MacOS/Card Farm", "--idle-child", "lib"]


def test_idler_child_gives_up_on_a_bad_library():
    import subprocess
    assert subprocess.run(idler.child_command("/no/such/library"), stdin=subprocess.DEVNULL).returncode == 2


def test_idling_starts_before_the_inventory_is_read(world, monkeypatch):
    """A slow or rate-limited inventory mustn't keep games from starting."""
    f = world["make"]()
    world["badges"] = [{"appid": 10, "name": "Game", "drops": 3, "earned": 3}]
    monkeypatch.setattr(farm_mod, "steam_pid", lambda: 1)
    monkeypatch.setattr(farm_mod, "game_you_are_playing", lambda ours: None)

    def limited(session):
        raise steam.HttpError(429)
    monkeypatch.setattr(steam, "cards_in_inventory", limited)
    with pytest.raises(steam.HttpError):
        f.tick()
    assert list(f.state["idling"]) == ["10"] and f.status == "Farming Game"


def test_market_requests_are_spaced_out(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(steam, "get_json", lambda url: asked.append(time.time()) or {"goo_value": "5"})
    p = steam.Prices(str(tmp_path / "prices.json"))
    p.GAP = 0.2
    for appid in (1, 2, 3):
        p.gems(appid, 3)
    assert all(b - a >= 0.19 for a, b in zip(asked, asked[1:])) and len(asked) == 3


def test_a_drop_the_badges_page_shows_late_is_still_a_drop(world):
    """Steam's badges page lags the inventory by a minute or two."""
    f = world["make"]()
    world["badges"] = [{"appid": 10, "name": "Game", "drops": 3, "earned": 3}]
    f._refresh_badges(); f._check_inventory()          # first look
    world["cards"] = [card(assetid="1")]
    f._refresh_badges(); f._check_inventory()          # the card is there, the badges don't know yet
    assert f.state["queue"] == [] and f.state["review"] == [] and f._soon
    world["badges"] = [{"appid": 10, "name": "Game", "drops": 2, "earned": 3}]
    f._refresh_badges(); f._check_inventory()          # a few minutes later
    assert [j["entry"]["id"] for j in f.state["queue"]] == ["1"] and f.state["stats"]["drops"] == 1


def test_a_card_no_drop_explains_goes_to_review_after_a_while(world):
    f = world["make"]()
    world["badges"] = [{"appid": 10, "name": "Game", "drops": 3, "earned": 3}]
    f._refresh_badges(); f._check_inventory()
    world["cards"] = [card(assetid="1")]
    f._check_inventory()
    f.state["unexplained"]["1"] -= farm_mod.EXPLAIN_WAIT
    f._check_inventory()
    assert [e["id"] for e in f.state["review"]] == ["1"] and f.state["queue"] == []


# --- updates ---------------------------------------------------------------------------------
from cardfarm import update  # noqa: E402


def release(tag, prerelease=True, names=("Card-Farm-Windows.zip", "card-farm.pyz")):
    return {"tag_name": tag, "prerelease": prerelease, "draft": False, "html_url": f"https://x/{tag}",
            "assets": [{"name": n, "browser_download_url": f"https://x/{tag}/{n}", "digest": "sha256:ab"} for n in names]}


def test_versions_sort():
    assert update.version_key("v0.2.0-beta.7") < update.version_key("v0.2.0-beta.10") < update.version_key("v0.2.0")
    assert update.version_key("dev") is None


def test_the_newest_release_for_this_system_is_offered():
    rel = [release("v0.2.0-beta.6"), release("v0.2.0-beta.8"), release("v0.2.0-beta.7")]
    offer = update.newest(rel, "v0.2.0-beta.7", "windows")
    assert offer == {"version": "v0.2.0-beta.8", "page": "https://x/v0.2.0-beta.8",
                     "url": "https://x/v0.2.0-beta.8/Card-Farm-Windows.zip", "sha256": "ab"}
    assert update.newest(rel, "v0.2.0-beta.8", "windows") is None          # already the newest
    assert update.newest(rel, "v0.2.0", "windows") is None                 # finished versions skip betas
    assert update.newest(rel, "v0.2.0-beta.7", None)["url"] is None        # from source: a link only


def test_pyz_update_swaps_the_file_after_card_farm_exits(tmp_path):
    import subprocess
    if sys.platform != "linux":
        pytest.skip("the .pyz is the Linux download")
    old, new = tmp_path / "card-farm.pyz", tmp_path / "new.pyz"
    old.write_text("old"), new.write_text("new")
    waiter = subprocess.Popen(["sleep", "1"])
    threading.Thread(target=waiter.wait).start()  # collect it when it ends, as a launcher would
    update.install(str(new), relaunch=False, wait_for=waiter.pid, target=str(old)).wait(10)
    assert old.read_text() == "new" and not new.exists() and waiter.poll() is not None


@pytest.mark.skipif(sys.platform not in ("win32", "darwin"), reason="the Windows and macOS installers")
@pytest.mark.parametrize("new_name", ["Card Farm", "Card Farmer's Market"])
def test_app_update_swaps_the_app_after_card_farm_exits(tmp_path, new_name):
    """Runs on the release builders: the real helper replaces a stand-in Card Farm with the new one,
    under the old name or the new one."""
    import shutil
    import subprocess
    windows = sys.platform == "win32"
    inner = new_name if windows else f"{new_name}.app"
    new_app = tmp_path / "new" / inner
    (new_app if windows else new_app / "Contents" / "MacOS").mkdir(parents=True)
    marker = f"{new_name}.exe" if windows else f"Contents/MacOS/{new_name}"
    (new_app / marker).write_text("new")
    if windows:
        package = shutil.make_archive(str(tmp_path / "Card-Farm-Windows"), "zip", tmp_path / "new", inner)
    else:
        package = str(tmp_path / "Card-Farm-macOS.zip")
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(new_app), package], check=True)
    old_inner, old_marker = ("Card Farm", "Card Farm.exe") if windows else ("Card Farm.app", "Contents/MacOS/Card Farm")
    old_app = tmp_path / "installed" / old_inner
    (old_app / old_marker).parent.mkdir(parents=True)
    (old_app / old_marker).write_text("old")
    waiter = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
    threading.Thread(target=waiter.wait).start()
    update.install(package, relaunch=False, wait_for=waiter.pid, target=str(old_app)).wait(120)
    logs = "\n".join(f"{p.name}: {p.read_text(errors='replace')}" for p in pathlib.Path(package).parent.glob("update*.log"))
    if windows:  # the same folder, now with the new program in it (and no stale old one)
        assert (old_app / marker).read_text() == "new", logs or "no logs"
        assert new_name == "Card Farm" or not (old_app / old_marker).exists()
    else:        # the app sits where the old one was, under its new name
        placed = old_app.parent / inner
        assert (placed / marker).read_text() == "new", logs or "no logs"
        assert new_name == "Card Farm" or not old_app.exists()


# --- market, gems, boosters, sets -------------------------------------------------------------
def test_sales_are_read_from_market_history():
    page = {"events": [{"listingid": "L1", "purchaseid": "P1", "event_type": 3, "time_event": 1000},
                       {"listingid": "L2", "purchaseid": "P2", "event_type": 1, "time_event": 900}],
            "purchases": {"L1_P1": {"received_amount": 9}},
            "listings": {"L1": {"asset": {"appid": 753, "contextid": "6", "id": "A1"}}},
            "assets": {"753": {"6": {"A1": {"market_name": "Hero", "market_hash_name": "10-Hero",
                                            "type": "Game Trading Card", "market_fee_app": 10}}}}}
    assert steam.parse_sales(page) == [{"at": 1000, "listingid": "L1", "name": "Hero", "hash": "10-Hero",
                                        "game": "Game Trading Card", "appid": 10, "received": 9}]


def test_set_and_booster_pages_are_read():
    set_page = ('<div class="badge_info_title">x</div> Level 2 Game Badge - 3 of 5 cards collected '
                '<div class="badge_card_set_card owned"><div class="badge_card_set_text_qty">(2)</div>'
                '<div class="badge_card_set_text badge_card_set_title ellipsis"> <span class="badge_card_set_text_qty">(2)</span> Hero &amp; Co <div></div></div></div>'
                '<div class="badge_card_set_card unowned"><div class="badge_card_set_text badge_card_set_title ellipsis"> Villain <div></div></div></div>')
    assert steam.parse_set_page(set_page) == {"level": 2, "cards": [{"name": "Hero & Co", "owned": 2}, {"name": "Villain", "owned": 0}]}
    booster_page = ('CBoosterCreatorPage.Init(\n [{"appid":10,"name":"Game","series":1,"price":"600"},'
                    '{"appid":11,"name":"Other","series":1,"price":"400","unavailable":true,"available_at_time":"Nov 4"}],\n'
                    ' parseFloat( "1238" ),\n parseFloat( "1000" ),\n parseFloat( "238" ), "<div>")')
    boosters, tradable = steam.parse_booster_page(booster_page)
    assert tradable == 1000 and boosters[0] == {"appid": 10, "name": "Game", "series": 1, "gems": 600,
                                                "unavailable": False, "available_at": ""}
    assert boosters[1]["unavailable"] and boosters[1]["available_at"] == "Nov 4"


def listing(**kw):
    return dict({"listingid": "L1", "assetid": "A1", "appid": 753, "context": 6, "hash": "10-Hero", "name": "Hero",
                 "game": "Game Trading Card", "icon": "", "classid": "C", "instanceid": "0", "receive": 17, "buyer": 20,
                 "created": time.time() - 3 * 86400}, **kw)


def test_only_old_undercut_listings_are_repriced(world):
    f = world["make"]()
    now = time.time()
    assert f._verdict(listing(), 19, now) == "ok"                                   # within the 2c gap
    assert f._verdict(listing(), 15, now) == "undercut"
    assert f._verdict(listing(created=now - 3600), 15, now) == "young"              # listed an hour ago
    assert f._verdict(listing(), None, now) == "ok"


def test_repricing_cancels_and_relists_at_todays_price(world, monkeypatch):
    f = world["make"]()
    removed, stock = [], [[({"assetid": "X", "classid": "Z", "instanceid": "0"}, {})]]
    monkeypatch.setattr(steam, "remove_listing", lambda s, lid: (removed.append(lid), stock.append(
        stock[0] + [({"assetid": "A9", "classid": "C", "instanceid": "0"}, {})])))
    monkeypatch.setattr(steam, "_inventory", lambda s, a, c: stock[-1])
    monkeypatch.setattr(market_mod.time, "sleep", lambda x: None)
    f.prices.item_stats = lambda appid, h: {"lowest": 15, "median": 16, "volume": 4}
    f.state["listings"]["items"] = [listing(verdict="undercut"), listing(listingid="c", verdict="ok")]
    f.decide_listings("undercut", "reprice")
    assert [j["entry"]["id"] for j in f.state["queue"]] == ["L1"]   # only the undercut one
    f._do_listing("reprice", {"listing": listing()})
    assert removed == ["L1"] and world["sold"] == [("A9", rules.received(15))] and "A9" in f.state["seen"]


def test_boosters_worth_making(world):
    f = world["make"]()
    f.prices.gem_sack = lambda: 69
    o = f._booster_offer({"appid": 10, "name": "Game", "series": 1, "gems": 600, "unavailable": False, "lowest": 60,
                          "median": 62, "volume": 9})
    assert o["receive"] == rules.received(60) and o["cost"] == round(600 * 69 / 1000 / 1.15, 1) and o["worth"]
    cheap = f._booster_offer({"appid": 11, "name": "Cheap", "series": 1, "gems": 1200, "unavailable": False,
                              "lowest": 20, "median": 20, "volume": 9})
    assert cheap["profit"] < 0 and not cheap["worth"]


def test_a_stale_high_ask_doesnt_make_a_booster_worth_it(world):
    f = world["make"]()
    f.prices.gem_sack = lambda: 69
    base = {"appid": 10, "name": "Game", "series": 1, "gems": 600, "unavailable": False}
    nobody_buys = f._booster_offer(dict(base, lowest=120, median=None, volume=0))     # only an old listing up
    assert not nobody_buys["sells"] and not nobody_buys["worth"]
    sold_cheaper = f._booster_offer(dict(base, lowest=120, median=30, volume=5))      # sales went for 30c
    assert sold_cheaper["receive"] == rules.received(30) and not sold_cheaper["worth"]


def test_sets_you_hold_part_of_are_offered(world, monkeypatch):
    f = world["make"]()
    monkeypatch.setattr(steam, "cards_in_inventory", lambda s: [card(assetid="1", hash="10-A"), card(assetid="2", hash="10-B")])
    f.prices.game_cards = lambda appid: {"10-A": 5, "10-B": 6, "10-C": 7, "10-A (Foil)": 90}
    f._check_sets()
    (offer,) = f.state["sets"]["offers"]
    assert (offer["have"], offer["of"], offer["missing"], offer["cost"]) == (2, 3, ["C"], 7)
    f.update_settings({"set_budget_cents": 5})
    with pytest.raises(ValueError):
        f.finish_set(10)                          # over the limit: nothing is bought


def test_idle_time_and_drops_add_up_per_game(world):
    f = world["make"]()
    f.state["idling"] = {"10": {}}
    f._last_idle_count = time.time() - 60
    f._count_idle_time()
    f._farmed_one(10)
    assert 59 <= f.state["idled"]["10"] <= 61 and f.state["farmed"]["10"] == 1


def test_steam_hiccups_are_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(steam.time, "sleep", lambda x: None)

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise steam.HttpError(500)
        return "ok"
    assert steam._patient(flaky) == "ok" and len(calls) == 3


def test_market_page_listings_leave_out_your_own():
    listings = [{"listingid": "1", "unPrice": 9, "unFee": 2, "eCurrency": 1, "bMine": False},
                {"listingid": "2", "unPrice": 4, "unFee": 2, "eCurrency": 1, "bMine": True},
                {"listingid": "3", "unPrice": 6, "unFee": 2, "eCurrency": 1, "bMine": False}]
    query_data = json.dumps({"queries": [{"queryKey": ["market_item_search", {}], "state": {"data": {"pages": [{"listings": listings}]}}},
                                         {"queryKey": ["market"], "state": {"data": {"pages": [{"listings": [{"listingid": "x"}]}]}}}]})
    page = f'<html><script id="valve-ssr-data" type="application/json">{json.dumps({"renderContext": {"queryData": query_data}})}</script>'
    assert steam.parse_listing_page(page) == [("3", 8), ("1", 11)]


def buying_world(monkeypatch, fills=True):
    log = []
    monkeypatch.setattr(market_mod.time, "sleep", lambda x: None)
    monkeypatch.setattr(steam, "create_buy_order", lambda s, h, p, q=1, **kw: log.append(("order", h, p) if q == 1 else ("order", h, p, q)) or {"orderid": h})
    monkeypatch.setattr(steam, "buy_order_status", lambda s, o, *a: {"purchased": 1 if fills else 0})
    monkeypatch.setattr(steam, "cancel_buy_order", lambda s, o: log.append(("cancel", o)))
    monkeypatch.setattr(steam, "card_set", lambda s, a: {"level": 0, "cards": []})
    monkeypatch.setattr(steam, "craft_badge", lambda s, a: log.append(("craft", a)))
    monkeypatch.setattr(steam, "buy_orders", lambda s: {})  # Steam closes an order once it filled or was cancelled
    return log


def test_a_set_is_bought_and_crafted(world, monkeypatch):
    log = buying_world(monkeypatch)
    monkeypatch.setattr(steam, "cheapest_listing", lambda s, p, h: {"10-B": 6, "10-C": 7}[h])
    f = world["make"]()
    f._do_set({"offer": {"appid": 10, "game": "Game", "missing_hashes": ["10-B", "10-C"]}})
    assert log == [("order", "10-B", 6), ("order", "10-C", 7), ("craft", 10)]


def test_nothing_is_bought_when_the_set_got_dearer(world, monkeypatch):
    log = buying_world(monkeypatch)
    monkeypatch.setattr(steam, "cheapest_listing", lambda s, p, h: 30)
    f = world["make"]()
    with pytest.raises(RuntimeError, match="nothing bought"):
        f._do_set({"offer": {"appid": 10, "game": "Game", "missing_hashes": ["10-B", "10-C"]}})  # 60c > 50c
    assert log == []


def test_an_unfilled_buy_order_is_cancelled(world, monkeypatch):
    log = buying_world(monkeypatch, fills=False)
    f = world["make"]()
    assert f._buy("10-B", 6) is False and log == [("order", "10-B", 6), ("cancel", "10-B")]


def test_a_set_being_finished_keeps_its_cards(world, monkeypatch):
    f = world["make"]()
    f.state["sets"]["offers"] = [{"appid": 10, "game": "Game", "have": 2, "of": 3, "missing": ["C"],
                                  "missing_hashes": ["10-C"], "cost": 7, "worth": 9, "unpriced": False}]
    f.state["review"] = [{"id": "r1", "kind": "card", "card": card(assetid="r1"), "action": "sell", "detail": {}}]
    f._enqueue("gems", {"id": "q1", "kind": "card", "card": card(assetid="q1"), "detail": {"gems": 20}})
    f.finish_set(10)
    assert f.evaluate(card())[0] == "keep"                     # a new drop of it is kept
    f.decide_review("all", "suggested")
    assert [e["id"] for e in f.state["review"]] == ["r1"]      # the review list keeps it too
    assert f._do("gems", f.state["queue"][0]["entry"]) == 0 and world["sold"] == []   # queued earlier: skipped
    f.stop_finishing(10)
    assert f.evaluate(card())[0] == "sell"


def test_a_filled_order_counts_even_when_the_status_check_fails(world, monkeypatch):
    log = buying_world(monkeypatch)
    def refuse(s, o, *a):
        raise steam.HttpError(400)
    monkeypatch.setattr(steam, "buy_order_status", refuse)
    monkeypatch.setattr(steam, "open_buy_orders", lambda s: set())
    f = world["make"]()
    assert f._buy("10-B", 6) is True and ("cancel", "10-B") not in log


def test_only_listings_that_could_be_repriced_are_priced(world, monkeypatch):
    now, asked = time.time(), []
    monkeypatch.setattr(steam, "my_listings", lambda s: [
        listing(listingid="new", created=now - 3600, buyer=20),          # too young
        listing(listingid="min", buyer=4),                               # at the 3c minimum (+2c gap)
        listing(listingid="old", buyer=20, hash="10-Hero")])             # could be repriced
    f = world["make"]()
    f._stats = lambda appid, h: asked.append(h) or {"lowest": 12, "median": 13, "volume": 3}
    f._check_listings()
    got = {l["listingid"]: l["verdict"] for l in f.state["listings"]["items"]}
    assert got == {"new": "young", "min": "floor", "old": "undercut"} and asked == ["10-Hero"]


def test_repricing_follows_what_sells_not_one_cheap_seller(world):
    f = world["make"]()
    now = time.time()
    assert f._verdict(listing(buyer=20), rules.realistic({"lowest": 19, "median": 25, "volume": 4}), now) == "ok"
    assert f._verdict(listing(buyer=20), rules.realistic({"lowest": 30, "median": 12, "volume": 4}), now) == "undercut"


# --- level up -----------------------------------------------------------------------------------
def test_steam_level_xp():
    assert market_mod.xp_for_level(9) == 100 and market_mod.xp_for_level(10) == 200 and market_mod.xp_for_level(55) == 600
    assert market_mod.xp_between(55, 130, 56) == 130 and market_mod.xp_between(55, 130, 57) == 730
    assert market_mod.level_after(55, 130, 200) == 56 and market_mod.level_after(55, 130, 800) == 57


def test_the_cheapest_crafts_reach_the_target_within_budget(world):
    f = world["make"]()
    opt = lambda appid, cost, levels=1, forgone=0: {"appid": appid, "game": f"G{appid}", "levels": levels, "cost": cost,
                                                     "forgone": forgone, "source": "market", "missing_hashes": [], "missing": []}
    f.state["levelup"] = {"level": 55, "xp": 0, "xp_to_next": 130, "maxed": 0, "checked": 1,
                          "options": [opt(1, 30, levels=3), opt(2, 20), opt(3, 10, forgone=30)]}
    f.update_settings({"levelup_target": 56, "levelup_budget_cents": 100})
    plan = f.levelup_plan()                      # 130 XP: two crafts, cheapest first (G2 20c, then G1 30c)
    assert [c["appid"] for c in plan["crafts"]] == [2, 1] and plan["cost"] == 50 and plan["reaches"] == 56
    f.update_settings({"levelup_target": 57, "levelup_budget_cents": 60})
    plan = f.levelup_plan()                      # 730 XP wanted, the budget stops it short
    assert plan["cost"] <= 60 and plan["short"] > 0


def test_level_up_queues_the_plan_and_keeps_the_cards(world, monkeypatch):
    log = buying_world(monkeypatch)
    monkeypatch.setattr(steam, "cheapest_listing", lambda s, p, h: 5)
    f = world["make"]()
    f.state["levelup"] = {"level": 9, "xp": 0, "xp_to_next": 200, "maxed": 0, "checked": 1,
                          "options": [{"appid": 7, "game": "G", "levels": 2, "cost": 10, "forgone": 0, "source": "market",
                                       "missing_hashes": ["7-A", "7-B"], "missing": ["A", "B"]}]}
    f.level_up()
    assert [j["entry"]["id"] for j in f.state["queue"]] == ["set-7-0", "set-7-1"] and f.finishing(7)
    first = f.state["queue"][0]["entry"]
    f._do_set(first)                             # limit comes with the plan: 10c * 1.25 + 2
    f.state["queue"].pop(0)
    assert f.finishing(7)                        # another craft of it is still queued
    f._do_set(f.state["queue"][0]["entry"])
    assert not f.finishing(7) and log.count(("craft", 7)) == 2


def test_after_a_429_requests_to_that_steam_site_pause_together(tmp_path, monkeypatch):
    from cardfarm import http
    sent, said = [], []

    class Done:
        stdout = b"\n429"

    def curl(cmd, input, **kw):
        sent.append(input.decode().splitlines()[0])
        return Done()
    monkeypatch.setattr(http.subprocess, "run", curl)
    monkeypatch.setattr(http, "_pause", {})
    monkeypatch.setattr(http, "on_pause", lambda host, minutes: said.append((host, minutes)))
    monkeypatch.setattr(steam.time, "sleep", lambda x: None)
    p = steam.Prices(str(tmp_path / "prices.json"))
    for appid in (1, 2, 3):
        with pytest.raises(steam.HttpError):
            p.item_stats(753, f"{appid}-Pack")
    assert len(sent) == 1 and said == [("steamcommunity.com", 5)]  # only the first reached Steam
    assert http.request("https://steamcommunity.com/profiles/1/badges/")[0] == 429 and len(sent) == 1  # the badges wait too
    http.request("https://store.steampowered.com/api/appdetails?appids=1")  # another site has its own limit
    assert len(sent) == 2 and said[-1] == ("store.steampowered.com", 5)
    http._pause["steamcommunity.com"]["until"] = 0  # the pause is over; Steam still says no: a longer one
    http.request("https://steamcommunity.com/market/")
    assert said[-1] == ("steamcommunity.com", 10)


def test_recent_sales_from_price_history():
    import calendar
    now = calendar.timegm((2026, 11, 4, 12, 30, 0))
    points = [["Nov 02 2026 12: +0", 0.90, "50"],       # too old
              ["Nov 04 2026 01: +0", 0.20, "3"], ["Nov 04 2026 11: +0", 0.30, "10"], ["Nov 04 2026 12: +0", 0.40, "4"]]
    assert steam.recent_sales(points, now=now) == {"median": 30, "volume": 17}
    assert steam.recent_sales([], now=now) == {"median": None, "volume": 0}


def test_boosters_are_made_within_your_gems(world, monkeypatch):
    f = world["make"]()
    f.prices.gem_sack = lambda: 69
    offer = lambda appid, gems: f._booster_offer({"appid": appid, "name": f"G{appid}", "series": 1, "gems": gems,
                                                  "unavailable": False, "lowest": 120, "median": 110, "volume": 5})
    f.state["boosters"] = {"options": [offer(1, 600), offer(2, 750), offer(3, 400)], "gems": 1100, "checked": 1}
    f.make_boosters("profitable")                 # 750 + 400 = 1150 > 1100: the two that fit
    assert sorted(j["entry"]["booster"]["appid"] for j in f.state["queue"]) in ([1, 3], [2])
    assert sum(j["entry"]["booster"]["gems"] for j in f.state["queue"]) <= 1100
    with pytest.raises(ValueError):
        f.make_boosters([2])                      # what's left doesn't cover it


def test_a_booster_unpacks_a_sack_when_loose_gems_fall_short(world, monkeypatch):
    unpacked = []
    monkeypatch.setattr(steam, "booster_options", lambda s: ([], 522))
    monkeypatch.setattr(steam, "gem_stock", lambda s: {"gems": 522, "sacks": 1, "gems_id": "g", "sack_ids": ["S1"]})
    monkeypatch.setattr(steam, "pack_gems", lambda s, a, n, unpack=False: unpacked.append((a, unpack)))
    monkeypatch.setattr(steam, "create_booster", lambda s, a, se: (_ for _ in ()).throw(RuntimeError("stop here")))
    monkeypatch.setattr(steam, "_inventory", lambda s, a, c: [])
    f = world["make"]()
    with pytest.raises(RuntimeError, match="stop here"):
        f._do_booster({"booster": {"appid": 1, "name": "G", "series": 1, "gems": 600}})
    assert unpacked == [("S1", True)]


def test_a_game_that_never_drops_when_idled_is_set_aside_for_a_week(world):
    f = world["make"]()
    f.update_settings({"stall_minutes": 90})
    f.state["games"] = [{"appid": 730, "name": "CS2", "drops": 14, "earned": 65}]
    f.state["idled"] = {"730": 7 * 3600}
    f.state["idling"] = {"730": {"since": 0, "clock": 0, "last_drop": None, "restarts": 1}}
    f._check_stalls()
    assert f.state["no_idle"]["730"] > time.time() + 6 * 86400 and "730" not in f.state["skipped"]
    assert f.snapshot()["games"][0]["no_idle"] and f._candidates() == []
    world["badges"] = [{"appid": 730, "name": "CS2", "drops": 13, "earned": 65}]   # you played it: a drop
    f._refresh_badges()
    assert "730" not in f.state["no_idle"]


def test_held_packs_suggest_open_or_sell(world, monkeypatch):
    monkeypatch.setattr(steam, "boosters_held", lambda s: [
        {"assetid": "p1", "appid": 10, "name": "G Booster Pack", "game": "G", "hash": "10-G Booster Pack", "icon": "", "marketable": True},
        {"assetid": "p2", "appid": 11, "name": "H Booster Pack", "game": "H", "hash": "11-H Booster Pack", "icon": "", "marketable": True}])
    f = world["make"]()
    f.prices.item_stats = lambda a, h: {"lowest": 60, "median": 58, "volume": 8} if h.startswith("10") else {"lowest": 5, "median": 5, "volume": 8}
    f.prices.game_cards = lambda appid: {f"{appid}-A": 6, f"{appid}-B": 7}
    f._check_held()
    got = {p["assetid"]: p["verdict"] for p in f.state["held"]["packs"]}
    assert got == {"p1": "sell", "p2": "open"}           # the pack sells for more / the cards are worth more
    f.decide_held("suggested", "suggested")
    assert sorted((j["action"], j["entry"]["id"]) for j in f.state["queue"]) == [("open", "p2"), ("sell", "p1")]


def test_cards_from_an_opened_pack_go_straight_to_review(world, monkeypatch):
    monkeypatch.setattr(steam, "unpack_booster", lambda s, a, i: ["A", "B", "C"])
    f = world["make"]()
    world["badges"] = [{"appid": 10, "name": "Game", "drops": 3, "earned": 3}]
    f._refresh_badges(); f._check_inventory()                           # first look
    f._do_open({"pack": {"appid": 10, "assetid": "p", "game": "Game"}})
    world["cards"] = [card(assetid="n1")]
    f._check_inventory()
    assert [e["id"] for e in f.state["review"]] == ["n1"] and f.state["queue"] == []


def test_buy_orders_go_a_little_under_the_ask_never_under_3c():
    assert market_mod.order_price(20) == 17 and market_mod.order_price(4) == 3 and market_mod.order_price(3) == 3
    assert market_mod.order_price(None) == 3


def test_level_up_by_buy_orders(world, monkeypatch):
    log = buying_world(monkeypatch)
    f = world["make"]()
    f.update_settings({"levelup_buying": "orders"})
    f.prices.game_cards = lambda appid: {"7-A": 10, "7-B": 3}
    f.state["levelup"] = {"level": 9, "xp": 0, "xp_to_next": 200, "maxed": 0, "checked": 1,
                          "options": [{"appid": 7, "game": "G", "levels": 2, "cost": 13, "forgone": 0, "source": "market",
                                       "missing_hashes": ["7-A", "7-B"], "missing": ["A", "B"]}]}
    f.level_up()
    f._do("place", f.state["queue"].pop(0)["entry"])
    assert ("order", "7-A", 8, 2) in log and ("order", "7-B", 3, 2) in log      # two of each, under the ask
    assert len(f.state["patient"]) == 2 and f.finishing(7)
    # one full set has come in, one A and one B
    monkeypatch.setattr(steam, "buy_orders", lambda s: {"7-A": 1, "7-B": 1})
    monkeypatch.setattr(steam, "cards_in_inventory", lambda s: [card(assetid="1", hash="7-A", appid=7), card(assetid="2", hash="7-B", appid=7)])
    f._check_orders()
    assert [j["entry"]["kind"] for j in f.state["queue"]] == ["set"] and len(f.state["patient"]) == 1
    # a week later, still unfilled: cancelled
    for o in f.state["orders"]:
        o["placed"] -= 8 * 86400
    f._check_orders()
    assert f.state["orders"] == [] and ("cancel", "7-A") in log


def test_intel_macs_take_their_own_download_or_the_universal_one(monkeypatch):
    assert update.asset_names("macos", "arm64")[-1] == "Card-Farm-macOS.zip"
    assert "Card-Farm-macOS-Intel.zip" not in update.asset_names("macos", "arm64")
    both = release("v0.4.0", names=("Card-Farm-macOS.zip", "Card-Farm-macOS-Intel.zip"))
    universal = release("v0.4.0", names=("Card-Farm-macOS.zip",))
    monkeypatch.setattr(update.platform, "machine", lambda: "arm64")
    assert update.newest([both], "v0.3.0-beta.9", "macos")["url"].endswith("/Card-Farm-macOS.zip")
    monkeypatch.setattr(update.platform, "machine", lambda: "x86_64")
    assert update.newest([both], "v0.3.0-beta.9", "macos")["url"].endswith("/Card-Farm-macOS-Intel.zip")
    assert update.newest([universal], "v0.3.0-beta.9", "macos")["url"].endswith("/Card-Farm-macOS.zip")


def test_new_download_names_are_preferred_and_old_ones_still_found():
    new = release("v0.4.0", names=("Card-Farmers-Market-Windows.zip", "Card-Farm-Windows.zip", "card-farmers-market.pyz"))
    assert update.newest([new], "v0.3.0-beta.9", "windows")["url"].endswith("/Card-Farmers-Market-Windows.zip")
    assert update.newest([new], "v0.3.0-beta.9", "pyz")["url"].endswith("/card-farmers-market.pyz")
    old = release("v0.4.0", names=("Card-Farm-Windows.zip",))
    assert update.newest([old], "v0.3.0-beta.9", "windows")["url"].endswith("/Card-Farm-Windows.zip")


def test_a_web_sign_in_is_renewed_the_way_a_browser_does_it(tmp_path, monkeypatch):
    from cardfarm import auth
    s = auth.Session(str(tmp_path / "session.json"))
    s.steamid, s.refresh_token, s.access_token = "7656", "refresh", "old"
    sent = []

    def post_json(url, data, **kw):
        sent.append(url)
        if "GenerateAccessTokenForApp" in url:
            return {"response": {}}  # what Steam answers for a web sign-in
        assert data["nonce"] == "refresh"
        return {"steamID": "7656", "transfer_info": [
            {"url": "https://store.steampowered.com/login/settoken", "params": {"nonce": "a", "auth": "b"}},
            {"url": "https://steamcommunity.com/login/settoken", "params": {"nonce": "c", "auth": "d"}}]}

    def post_for_cookies(url, data):
        assert url == "https://steamcommunity.com/login/settoken" and data == {"nonce": "c", "auth": "d", "steamID": "7656"}
        return 200, '{"result":1}', {"steamLoginSecure": "7656%7C%7Cnew-token", "sessionid": "x"}

    monkeypatch.setattr(auth, "post_json", post_json)
    monkeypatch.setattr(auth, "post_for_cookies", post_for_cookies)
    s.renew()
    assert s.access_token == "new-token" and s.refresh_token == "refresh"
    assert json.load(open(tmp_path / "session.json"))["access_token"] == "new-token"
    assert sent[-1] == "https://login.steampowered.com/jwt/finalizelogin"


def test_renewing_fails_clearly_when_steam_refuses_both_ways(tmp_path, monkeypatch):
    from cardfarm import auth
    s = auth.Session(str(tmp_path / "session.json"))
    s.steamid, s.refresh_token = "7656", "refresh"
    monkeypatch.setattr(auth, "post_json", lambda url, data, **kw: {"response": {}} if "Generate" in url else {"error": 8})
    with pytest.raises(RuntimeError, match="sign in again"):
        s.renew()


def test_a_game_you_skip_stays_skipped_until_you_unskip_it(world, monkeypatch):
    f = world["make"]()
    f.state["games"] = [{"appid": 10, "name": "A", "drops": 3, "earned": 3}, {"appid": 11, "name": "B", "drops": 2, "earned": 2}]
    f.skip(10)
    later = time.time() + 30 * 86400
    monkeypatch.setattr(farm_mod.time, "time", lambda: later)
    assert [g["appid"] for g in f._candidates()] == [11]
    f.unskip(10)
    assert {g["appid"] for g in f._candidates()} == {10, 11}


def test_free_to_play_games_are_skipped_unless_you_try_them(world, monkeypatch):
    f = world["make"]()
    f.state["games"] = [{"appid": 730, "name": "CS2", "drops": 14, "earned": 65}, {"appid": 11, "name": "B", "drops": 2, "earned": 2}]
    monkeypatch.setattr(steam, "free_to_play", lambda appid: appid == 730)
    monkeypatch.setattr(farm_mod.time, "sleep", lambda s: None)
    f._learn_free_games()
    assert f.state["free_to_play"] == {"730": True, "11": False}
    assert [g["appid"] for g in f._candidates()] == [11]
    shown = {g["appid"]: g for g in f.snapshot()["games"]}
    assert shown[730]["free_skipped"] and not shown[11]["free_skipped"]
    f.unskip(730)  # "Try"
    assert {g["appid"] for g in f._candidates()} == {730, 11}
    f.skip(730)
    assert [g["appid"] for g in f._candidates()] == [11]
    f.unskip_all()
    assert {g["appid"] for g in f._candidates()} == {730, 11}
    f.update_settings({"skip_free_to_play": False})
    f.state["idle_anyway"] = []
    assert {g["appid"] for g in f._candidates()} == {730, 11}


def test_amounts_read_in_cents_under_a_dollar_and_dollars_above():
    assert market_mod.cents_text(50) == "50¢" and market_mod.cents_text(150) == "$1.50" and market_mod.cents_text(500) == "$5.00"


def test_settings_that_are_gone_are_dropped(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"games_at_once": 32, "bogus": 1, "sell": "off"}))
    s = rules.load_settings(str(tmp_path / "settings.json"))
    assert "games_at_once" not in s and "bogus" not in s and s["sell"] == "off"


def test_games_short_of_playtime_idle_together_first_then_one_at_a_time(world, monkeypatch):
    f = world["make"]()
    f.state["games"] = [{"appid": a, "name": str(a), "drops": 2, "earned": 2} for a in (10, 11, 12, 13)]
    f.state["playtime"] = {"10": 300, "11": 30, "12": 0, "13": 119}
    want, bulk = f._pick()
    assert bulk and set(want) == {11, 12, 13}
    f._choose_games()
    assert all(i["bulk"] for i in f.state["idling"].values()) and "Building playtime" in f.status
    # bulk time counts toward playtime, not toward drops or stalls
    f._last_idle_count = time.time() - 120
    f._count_idle_time()
    assert f.state["bulk"]["13"]["secs"] >= 119 and "13" not in f.state["idled"]
    f.state["bulk"]["11"]["secs"] = f.state["bulk"]["12"]["secs"] = 2 * 3600
    for i in f.state["idling"].values():
        i["clock"] -= 5 * 3600
    f._check_stalls()
    assert len(f.state["idling"]) == 3  # nobody restarted or set aside while building playtime
    # all past 2 h now: one at a time for drops, with a fresh drop clock
    want, bulk = f._pick()
    assert not bulk and len(want) == 1
    f._choose_games()
    (appid, info), = f.state["idling"].items()
    assert not info["bulk"] and time.time() - info["clock"] < 60


def test_unknown_playtime_counts_as_enough_and_drops_follow_the_farm_first_order(world):
    f = world["make"]()
    f.state["games"] = [{"appid": 10, "name": "A", "drops": 2, "earned": 2}, {"appid": 11, "name": "B", "drops": 5, "earned": 5}]
    f.state["playtime"] = {"10": 0}
    assert f._pick() == ([10], True)
    f.state["playtime"] = {}
    f.update_settings({"order": "most"})
    assert f._pick() == ([11], False)
    f.update_settings({"order": "fewest"})
    assert f._pick() == ([10], False)


def test_at_most_32_games_build_playtime_at_once(world):
    f = world["make"]()
    f.state["games"] = [{"appid": a, "name": str(a), "drops": 1, "earned": 1} for a in range(100, 140)]
    f.state["playtime"] = {str(a): 0 for a in range(100, 140)}
    want, bulk = f._pick()
    assert bulk and len(want) == 32


def test_threads_needing_cookies_at_once_renew_the_sign_in_once(tmp_path, monkeypatch):
    from cardfarm import auth
    s = auth.Session(str(tmp_path / "session.json"))
    s.steamid, s.refresh_token, s.access_token = "7656", "refresh", None
    calls = []

    def renew():
        calls.append(1)
        time.sleep(0.2)
        body = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 86400}).encode()).decode().rstrip("=")
        s.access_token = f"x.{body}.y"
    monkeypatch.setattr(s, "renew", renew)
    threads = [threading.Thread(target=s.cookies) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert calls == [1]


def test_foil_drops_are_remembered_for_the_garden(world):
    f = world["make"]()
    world["badges"] = [{"appid": 10, "name": "Game", "drops": 3, "earned": 3}]
    f._refresh_badges(); f._check_inventory()
    world["cards"] = [card(assetid="1")]
    world["badges"] = [{"appid": 10, "name": "Game", "drops": 2, "earned": 3}]
    f._refresh_badges(); f._check_inventory()
    world["cards"].append(card(assetid="2", foil=True, name="Hero (Foil)", hash="10-Hero (Foil)"))
    world["badges"] = [{"appid": 10, "name": "Game", "drops": 1, "earned": 3}]
    f._refresh_badges(); f._check_inventory()
    assert f.state["stats"]["drops"] == 2 and f.state["stats"]["foil_drops"] == [1]
    assert f.snapshot()["stats"]["foil_drops"] == [1]
    history = json.load(open(f._path("farm-history.json")))
    assert [(e["n"], e["card"], e["foil"]) for e in history] == [(0, "Hero", False), (1, "Hero (Foil)", True)]
    assert all(e["version"] and e["game"] == "Game" for e in history)


def test_the_theme_is_a_setting():
    assert rules.DEFAULTS["theme"] == "auto"
    assert rules.clean({"theme": "pink"}) == {"theme": "pink"} and rules.clean({"theme": "neon"}) == {}


def test_an_idler_that_stops_at_once_says_why(tmp_path, monkeypatch):
    class Proc:
        def __init__(self, code):
            self.returncode = code
        def poll(self):
            return self.returncode
    codes = iter([3, None, 7])
    monkeypatch.setattr(idler.subprocess, "Popen", lambda *a, **kw: Proc(next(codes)))
    monkeypatch.setattr(idler.time, "sleep", lambda s: None)
    i = idler.Idler(str(tmp_path))
    i.library = "lib"
    failed = i.start([10, 11, 12])
    assert set(failed) == {10, 12} and "signed in" in failed[10] and "exit code 7" in failed[12]
    assert set(i.procs) == {11}


def test_steam_errors_read_plainly():
    assert "couldn't reach Steam" in str(steam.HttpError(0))
    assert "servers are having trouble" in str(steam.HttpError(502))
    assert "sign in again" in str(steam.HttpError(403))


# --- quick badge ----------------------------------------------------------------------------
from cardfarm import quick as quick_mod


def ssr_page(queries):
    query_data = json.dumps({"queries": [{"queryKey": k, "state": {"data": d}} for k, d in queries]})
    return f'<html><script id="valve-ssr-data" type="application/json">{json.dumps({"renderContext": {"queryData": query_data}})}</script>'


def test_the_order_book_counts_listings_at_each_price_without_yours():
    page = ssr_page([
        (["CurrentUserWalletDetails", 1], {"has_wallet": True, "balance": "3111", "currency_code": 1}),
        (["market_item_search", {}], {"pages": [{"listings": [{"listingid": "9", "unPrice": 6, "unFee": 2, "eCurrency": 1, "bMine": True}]}]}),
        (["market", "orderbook", 753, "10-Hero"], {"eCurrency": 1, "rgCompactSellOrders": [7, 49, 8, 2, 12, 1]})])
    assert steam.parse_order_book(page) == {"sell": [[7, 49], [8, 1], [12, 1]], "listed": 51, "wallet": 3111}


def test_buying_walks_up_the_order_book():
    assert quick_mod.buy_lots([[7, 2], [8, 1], [12, 5]], 4) == [[7, 2], [8, 1], [12, 1]]
    assert quick_mod.buy_lots([[7, 2]], 3) is None and quick_mod.buy_lots([[7, 2]], 0) == []


def test_a_badge_plan_counts_the_cards_you_have():
    pick = {"level": 2, "cards": [{"hash": "10-A", "name": "A", "have": 1, "sell": [[5, 1], [6, 9]]},
                                  {"hash": "10-B", "name": "B", "have": 0, "sell": [[9, 2]]}]}
    plan = quick_mod.badge_plan(pick, 5)         # 3 levels: A needs 2 more (5 + 6), B needs 3, only 2 listed
    assert plan["levels"] == 3 and plan["cards"][0]["lots"] == [[5, 1], [6, 1]] and plan["short"] == ["B"]
    plan = quick_mod.badge_plan(pick, 4)         # 2 levels: A 1 more, B 2
    assert plan["cost"] == 5 + 18 and plan["buying"] == 3 and not plan["short"]


def quick_world(world, monkeypatch, wallet=1000):
    log = buying_world(monkeypatch)
    monkeypatch.setattr(quick_mod.time, "sleep", lambda x: None)
    monkeypatch.setattr(steam, "buy_order_status", lambda s, o, *a: {"quantity": "9", "quantity_remaining": "0"})
    monkeypatch.setattr(steam, "buy_orders", lambda s: {})  # every order closed once filled
    books = {"7-A": [[3, 5], [4, 5]], "7-B": [[5, 1], [9, 9]]}
    monkeypatch.setattr(steam, "order_book", lambda s, p, h, tries=6: {"sell": books[h], "listed": 10, "wallet": wallet})
    monkeypatch.setattr(steam, "card_set", lambda s, a: {"level": 1, "cards": [{"name": "A", "owned": 0}, {"name": "B", "owned": 0}]})
    f = world["make"]()
    f.prices.game_cards = lambda appid: {"7-A": 3, "7-B": 5, "7-A (Foil)": 40}
    world["cards"] = [card(assetid="x", appid=7, hash="7-B", name="B")]
    return f, log


def test_quick_badge_prices_buys_and_crafts_to_the_top(world, monkeypatch):
    f, log = quick_world(world, monkeypatch)
    f._price_badge(7)
    pick = f.quick_plans()
    assert [c["have"] for c in pick["cards"]] == [0, 1] and [p["target"] for p in pick["plans"]] == [2, 3, 4, 5]
    top = pick["plans"][-1]                      # 4 levels: A ×4 at 3c; B ×3 more: 5c + 9c + 9c
    assert top["cost"] == 12 + 23 and top["buying"] == 7
    f.quick_badge(7, 5)
    assert f.finishing(7) and f.state["quick"]["pick"] is None
    f._do_quick(f.state["queue"][0]["entry"])
    assert ("order", "7-A", 3, 4) in log and ("order", "7-B", 5) in log and ("order", "7-B", 9, 2) in log
    assert log.count(("craft", 7)) == 4 and not f.finishing(7)


def test_quick_badge_wont_spend_past_the_wallet(world, monkeypatch):
    f, log = quick_world(world, monkeypatch, wallet=20)
    f._price_badge(7)
    with pytest.raises(ValueError, match="wallet"):
        f.quick_badge(7, 5)
    assert not f.state["queue"] and not f.finishing(7)


def test_pricing_waits_out_a_slow_down_and_goes_on(world, monkeypatch):
    f, log = quick_world(world, monkeypatch)
    asked, slept = [], []
    books = {"7-A": [[3, 5], [4, 5]], "7-B": [[5, 1], [9, 9]]}

    def book(s, p, h, tries=6):
        asked.append(h)
        if len(asked) == 2:                      # the second card is turned away once
            raise steam.HttpError(429, "", "https://steamcommunity.com/market/listings/753/7-B")
        return {"sell": books[h], "listed": 10, "wallet": 1000}
    monkeypatch.setattr(steam, "order_book", book)
    monkeypatch.setattr(quick_mod.time, "sleep", slept.append)
    monkeypatch.setattr(quick_mod.http, "paused_until", lambda url: time.time() + 300)
    f._price_badge(7)
    assert asked == ["7-A", "7-B", "7-B"] and 300 <= slept[0] <= 306 and f.quick_plans()["plans"]
    f._price_badge(7)                            # priced again within 15 min: no new lookups
    assert len(asked) == 3


def test_quick_badge_waits_while_steam_still_holds_the_last_order(world, monkeypatch):
    f, log = quick_world(world, monkeypatch)
    placed = []

    def order(s, h, p, q=1, **kw):              # Steam still counts the last order for a moment
        placed.append((h, p, q))
        if len(placed) == 3:
            raise RuntimeError("You already have an active buy order for this item. You will need to either cancel that order, or wait for it to be fulfilled before you can place a new order.")
        return {"orderid": f"o{len(placed)}"}
    monkeypatch.setattr(steam, "create_buy_order", order)
    f._price_badge(7)
    f.quick_badge(7, 5)
    f._do_quick(f.state["queue"][0]["entry"])
    assert placed[2] == placed[3] and log.count(("craft", 7)) == 4 and not f.finishing(7)


# --- things that went wrong quietly ------------------------------------------------------------
def orders_job(world, monkeypatch):
    log = buying_world(monkeypatch)
    f = world["make"]()
    f.update_settings({"levelup_buying": "orders"})
    f.prices.game_cards = lambda appid: {"7-A": 10, "7-B": 3}
    f.state["levelup"] = {"level": 9, "xp": 0, "xp_to_next": 200, "maxed": 0, "checked": 1,
                          "options": [{"appid": 7, "game": "G", "levels": 1, "cost": 13, "forgone": 0, "source": "market",
                                       "missing_hashes": ["7-A", "7-B"], "missing": ["A", "B"]}]}
    f.level_up()
    return f, log, f.state["queue"][0]["entry"]


def test_a_buy_order_is_on_record_the_moment_its_placed(world, monkeypatch):
    f, log, job = orders_job(world, monkeypatch)
    slow = [True]

    def order(s, h, p, q=1, **kw):
        if h == "7-B" and slow[0]:
            raise steam.HttpError(429)
        log.append(("order", h))
        return {"orderid": h}
    monkeypatch.setattr(steam, "create_buy_order", order)
    with pytest.raises(steam.HttpError):
        f._do("place", job)
    assert [o["hash"] for o in f.state["orders"]] == ["7-A"]            # known, and on disk
    assert [o["hash"] for o in world["make"]().state["orders"]] == ["7-A"]
    slow[0] = False
    f._do("place", job)                                                  # the job stayed queued: only the rest goes up
    assert log == [("order", "7-A"), ("order", "7-B")] and len(f.state["patient"]) == 1
    assert [o["hash"] for o in f.state["orders"]] == ["7-A", "7-B"]


def test_orders_steam_refuses_dont_leave_a_set_waiting_for_nothing(world, monkeypatch):
    f, log, job = orders_job(world, monkeypatch)

    def refuse(s, h, p, q=1, **kw):
        raise RuntimeError("You don't have enough funds")
    monkeypatch.setattr(steam, "create_buy_order", refuse)
    with pytest.raises(RuntimeError):
        f._do("place", job)
    f.state["queue"].clear()
    assert f.state["orders"] == [] and f.state["patient"] == [] and not f.finishing(7)


def test_a_card_with_no_price_is_never_turned_into_gems_unasked(world):
    f = world["make"]()
    f.update_settings({"gems": "auto"})
    f.prices.game_cards = lambda appid: {}       # Steam's search came back empty
    f.handle(card(), "drop")
    assert f.state["queue"] == [] and [e["action"] for e in f.state["review"]] == ["gems"]
    f.prices.game_cards = lambda appid: {"10-Hero": 3}   # a real, low price: gems by the rules
    f.handle(card(assetid="2"), "drop")
    assert [j["action"] for j in f.state["queue"]] == ["gems"]


def test_a_card_list_cut_short_is_asked_again_soon(tmp_path):
    p = steam.Prices(str(tmp_path / "prices.json"))
    pages = [{"results": [{"hash_name": "7-A", "sell_price": 5}], "total_count": 12}, {"results": [], "total_count": 0}]
    p._ask = lambda url: pages.pop(0)
    assert p.game_cards(7) == {"7-A": 5} and not p.complete(7)
    assert p.cache["cards:7"]["ttl"] == p.DOUBT_TTL
    p._ask = lambda url: {"results": [{"hash_name": "8-A", "sell_price": 5}], "total_count": 1}
    assert p.game_cards(8) and p.complete(8) and "ttl" not in p.cache["cards:8"]


def test_steams_own_items_are_searched_by_name_within_their_game(tmp_path):
    p, asked = steam.Prices(str(tmp_path / "prices.json")), []

    def ask(url):
        asked.append(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query))
        return {"results": [{"hash_name": "1086940-Karlach & Shadowheart", "sell_price": 5}]}
    p._ask = ask
    assert p._lowest(753, "1086940-Karlach & Shadowheart") == 5
    assert asked[0]["query"] == ["Karlach & Shadowheart"] and asked[0]["category_753_Game[]"] == ["tag_app_1086940"]
    assert p._lowest(730, "AK-47 | Redline") is None and asked[1]["query"] == ["AK-47 | Redline"]
    assert "category_753_Game[]" not in asked[1]


def test_no_price_for_a_sack_of_gems_isnt_remembered_as_one(tmp_path):
    p = steam.Prices(str(tmp_path / "prices.json"))
    p.item_stats = lambda appid, name: {"lowest": None, "median": None, "volume": 0}
    with pytest.raises(RuntimeError, match="sack of gems"):
        p.gem_sack()
    assert "sack" not in p.cache
    p.item_stats = lambda appid, name: {"lowest": 70, "median": 71, "volume": 9}
    assert p.gem_sack() == 70


def test_old_prices_are_dropped_from_the_file(tmp_path):
    now = time.time()
    (tmp_path / "prices.json").write_text(json.dumps({
        "cards:7": {"at": now, "v": {"7-A": 5}}, "cards:8": {"at": now - 30 * 86400, "v": {}},
        "item:730:Case": {"at": now, "v": 5}}))
    assert list(steam.Prices(str(tmp_path / "prices.json")).cache) == ["cards:7"]
    (tmp_path / "prices.json").write_text("{not json")
    assert steam.Prices(str(tmp_path / "prices.json")).cache == {}


def test_an_item_that_got_much_cheaper_goes_back_to_review(world):
    f = world["make"]()
    case = {"assetid": "c1", "appid": 730, "context": 2, "amount": 1, "name": "Case", "hash": "Case",
            "game": "CS2", "icon": "", "kind": "Container"}
    entry = {"id": "c1", "kind": "item", "item": case, "action": "sell", "detail": {"buyer": 400, "receive": 348}}
    assert f._do("sell", entry) == 0 and world["sold"] == []           # you were shown $4.00; it's $1.19 now
    assert [(e["id"], e["detail"]["buyer"]) for e in f.state["items"]["review"]] == [("c1", 119)]
    f.update_settings({"items_min_cents": 200})
    with pytest.raises(RuntimeError, match="under your minimum"):
        f._do("sell", dict(entry, detail={"buyer": 119}))
    f.update_settings({"items_min_cents": 5})
    assert f._do("sell", dict(entry, detail={"buyer": 125})) == 1 and world["sold"] == [("c1", rules.received(119))]


def test_a_new_item_with_one_odd_cheap_listing_waits_for_you(world):
    f = world["make"]()
    world["inventories"] = [{"key": "730/2", "appid": 730, "context": 2, "name": "CS2", "count": 2}]
    case = {"assetid": "c1", "appid": 730, "context": 2, "amount": 1, "name": "Case", "hash": "Case",
            "game": "CS2", "icon": "", "kind": "Container"}
    world["items"]["730/2"] = [case]
    f.update_settings({"items_inventories": ["730/2"], "items_new": "auto"})
    f._check_items(manual=False)
    f.prices.item_stats = lambda appid, name: {"lowest": 40, "median": 119, "volume": 9}
    world["items"]["730/2"] = [case, dict(case, assetid="c2")]
    f._check_items(manual=False)
    assert f.state["queue"] == [] and [e["id"] for e in f.state["items"]["review"]] == ["c2"]


def cancelling(world, monkeypatch):
    """Listings whose items come back as A1, A2, ... when cancelled; counts the inventory reads."""
    f = world["make"]()
    stock, reads, removed = [({"assetid": "X", "classid": "Z", "instanceid": "0"}, {})], [], []

    def remove(s, lid):
        removed.append(lid)
        stock.append(({"assetid": f"A{len(removed)}", "classid": "C", "instanceid": "0"}, {}))
    monkeypatch.setattr(steam, "remove_listing", remove)
    monkeypatch.setattr(steam, "_inventory", lambda s, a, c: reads.append(1) or list(stock))
    monkeypatch.setattr(market_mod.time, "sleep", lambda x: None)
    return f, reads, removed


def test_a_listing_isnt_repriced_far_under_what_the_check_showed(world, monkeypatch):
    f, reads, removed = cancelling(world, monkeypatch)
    f.prices.item_stats = lambda appid, h: {"lowest": 15, "median": 16, "volume": 4}
    assert f._do_listing("reprice", {"listing": listing(lowest=40, median=41)}) == 0
    assert removed == [] and world["sold"] == [] and reads == []       # still listed, nothing touched
    assert f._do_listing("reprice", {"listing": listing(lowest=16, median=17)}) == 1 and removed == ["L1"]


def test_cancelling_listings_reads_the_inventory_once_each(world, monkeypatch):
    f, reads, removed = cancelling(world, monkeypatch)
    f.state["baseline"] = True
    world["cards"] = [card(assetid="A1"), card(assetid="A2")]
    for lid in ("L1", "L2", "L3"):
        f._do_listing("cancel", {"listing": listing(listingid=lid)})
    assert removed == ["L1", "L2", "L3"] and len(reads) == 4           # one before the first, one after each
    assert {"A1", "A2", "A3"} <= set(f.state["seen"]) and [e["id"] for e in f.state["review"]] == ["A1", "A2"]


def test_a_cancelled_item_waits_for_review_instead_of_being_listed_again(world, monkeypatch):
    f, reads, removed = cancelling(world, monkeypatch)
    f.update_settings({"items_inventories": ["730/2"], "items_new": "auto"})
    f.state["items"]["seen"]["730/2"] = ["X"]
    f.state["items"]["inventories"] = [{"key": "730/2", "appid": 730, "context": 2, "name": "CS2", "count": 1}]
    case = {"assetid": "A1", "appid": 730, "context": 2, "amount": 1, "name": "Case", "hash": "Case",
            "game": "CS2", "icon": "", "kind": "Container"}
    monkeypatch.setattr(steam, "sellable_items", lambda stock, a, c, n="": [case])
    f._do_listing("cancel", {"listing": listing(appid=730, context=2, hash="Case", name="Case", game="CS2")})
    assert f.state["items"]["seen"]["730/2"] == ["A1", "X"] and f.state["queue"] == []
    assert [e["id"] for e in f.state["items"]["review"]] == ["A1"]


def test_buying_waits_while_steam_still_holds_the_last_order(world, monkeypatch):
    log = buying_world(monkeypatch)
    f, tries = world["make"](), []

    def order(s, h, p, q=1, **kw):
        tries.append(h)
        if len(tries) == 1:
            raise RuntimeError("You already have an active buy order for this item.")
        return {"orderid": h}
    monkeypatch.setattr(steam, "create_buy_order", order)
    assert f._buy("10-B", 6) is True and tries == ["10-B", "10-B"]


def test_an_order_that_filled_as_it_was_cancelled_counts_as_bought(world, monkeypatch):
    buying_world(monkeypatch, fills=False)
    f = world["make"]()

    def gone(s, o):
        raise RuntimeError("There was a problem cancelling your buy order")
    monkeypatch.setattr(steam, "cancel_buy_order", gone)
    assert f._buy("10-B", 6) is True                                   # no longer open: Steam filled it
    monkeypatch.setattr(steam, "buy_orders", lambda s: {"10-B": 1})    # still open: the cancel really failed
    monkeypatch.setattr(market_mod.MarketParts, "_until_closed", lambda self, o: None)
    with pytest.raises(RuntimeError, match="cancelling"):
        f._buy("10-B", 6)


def test_stopping_stops_the_games_at_once(world, monkeypatch):
    f = world["make"]()
    f.idler.procs = {10}
    f.state["idling"] = {"10": {"since": time.time()}}
    f.update_settings({"farming": False})          # no waiting for the main loop
    assert f.idler.running() == set() and f.state["idling"] == {} and f.status == "Stopped"


def test_starting_a_game_yourself_pauses_without_the_main_loop(world, monkeypatch):
    f = world["make"]()
    f.state["games"] = [{"appid": 20, "name": "Other", "drops": 0, "earned": 0}]
    f.idler.procs = {10}
    monkeypatch.setattr(farm_mod, "game_you_are_playing", lambda ours: 20)
    f._pause_if_playing()
    assert f.idler.running() == set() and f.status == "Paused while you play (Other)"


def test_the_market_chores_dont_run_on_the_main_loop(world, monkeypatch):
    f, ran = world["make"](), []
    monkeypatch.setattr(farm_mod.Farm, "_sync_sales", lambda self: ran.append(threading.current_thread()))
    monkeypatch.setattr(farm_mod.Farm, "_check_orders", lambda self: None)
    f.update_settings({"farming": False})
    f.tick()
    for _ in range(100):
        if ran:
            break
        time.sleep(0.02)
    assert ran and ran[0] is not threading.current_thread()


def test_damaged_files_dont_keep_the_app_from_opening(tmp_path, world):
    f = world["make"]()
    f.state["stats"]["drops"] = 7
    f.save()
    world["make"]()                                 # a good start keeps a copy of what it read
    (tmp_path / "state.json").write_text('{"seen": ["1"], "stats": {"dro')
    (tmp_path / "settings.json").write_text("")
    g = world["make"]()
    assert g.state["stats"]["drops"] == 7 and g.settings["farming"] is True
    assert (tmp_path / "state.json.broken").exists() and any("couldn't be read" in h["text"] for h in g.state["history"])
    (tmp_path / "session.json").write_text("garbage")
    assert not world["make"]().session.signed_in    # signed out, not stuck
    assert "couldn't be read" in (tmp_path / "card-farm.log").read_text()


def test_a_badges_page_that_fails_isnt_taken_for_games_gone(monkeypatch):
    page = (HERE / "badges_page.html").read_text()
    answers = [(200, page), (502, "Bad Gateway")]
    monkeypatch.setattr(steam, "request", lambda url, **kw: answers.pop(0))
    monkeypatch.setattr(steam.time, "sleep", lambda x: None)

    class Signed:
        steamid = "7656"
        cookies = staticmethod(lambda: {})
    with pytest.raises(steam.HttpError):
        steam.badges(Signed)


def test_a_sign_in_steam_wont_renew_ends_at_the_sign_in_screen(world):
    f = world["make"]()
    f.notify = lambda *a: None
    f._renewal_refused(); f._renewal_refused(); f._renewal_refused()
    assert f.session.signed_in                      # three in a minute: could be a hiccup
    f._refused = [time.time() - 3600, time.time() - 1800]
    f._renewal_refused()
    assert not f.session.signed_in and "sign in again" in f.signin_error.lower()


def test_the_order_status_check_names_the_items_market_page(monkeypatch):
    asked = {}
    monkeypatch.setattr(steam, "get_json", lambda url, **kw: asked.update(kw, url=url) or {"success": 1})

    class Signed:
        sessionid = "sid"
        cookies = staticmethod(lambda: {})
    steam.buy_order_status(Signed, "42", "10-Hero & Co")     # without it Steam answers 400
    assert asked["headers"]["Referer"].endswith("/market/listings/753/10-Hero%20%26%20Co") and "buy_orderid=42" in asked["url"]
