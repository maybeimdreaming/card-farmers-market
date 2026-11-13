"""Screenshots of every tab of the real window, filled with made-up data (no Steam account, no
network): for checking the layout after a change, and for the README.

    python3 tools/screenshots.py OUT_DIR

Needs GTK 4, libadwaita, WebKitGTK 6, and `cage` + `dbus-run-session` to run the window on a
hidden display of its own. Each tab is shot wide (1280 px) and narrow (430 px), and wide in the
light and pink themes as well; the run fails
if a page scrolls sideways or the page's script throws."""
import json
import os
import random
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABS = ["cards", "items", "market", "gems", "badges", "keys"]
NARROW = 430


def main():
    out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "screenshots")
    os.makedirs(out, exist_ok=True)
    if not os.environ.get("CARD_FARM_SHOTS_INSIDE"):  # run again on a display and bus of our own
        env = dict(os.environ, CARD_FARM_SHOTS_INSIDE="1", WLR_BACKENDS="headless", WLR_LIBINPUT_NO_DEVICES="1",
                   GTK_A11Y="none", GDK_DEBUG="no-portals")
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        # a neutral 1x scale for this run (the desktop's own text scaling would zoom the page)
        config = tempfile.mkdtemp(prefix="card-farm-demo-config-")
        os.makedirs(os.path.join(config, "gtk-4.0"))
        with open(os.path.join(config, "gtk-4.0", "settings.ini"), "w") as f:
            f.write("[Settings]\ngtk-xft-dpi=98304\n")
        env["XDG_CONFIG_HOME"] = config
        log = os.path.join(out, "run.log")
        started = time.time()
        with open(log, "w") as f:
            subprocess.run(["timeout", "-k", "5", "240", "dbus-run-session", "--", "cage", "--",
                            sys.executable, os.path.abspath(__file__), out], env=env, stdout=f, stderr=subprocess.STDOUT)
        stop_leftovers(started)
        result = json.load(open(os.path.join(out, "result.json"))) if os.path.exists(os.path.join(out, "result.json")) else None
        if not result:
            sys.exit(f"the window didn't finish; see {log}")
        for line in result["checks"]:
            print(line)
        sys.exit(0 if result["ok"] else 1)
    inside(out)


def stop_leftovers(since):
    """Desktop portal helpers the private session bus started for the window outlive it; stop the
    ones that were started on such a bus (/tmp/dbus-*) during this run, and nothing else."""
    ticks, boot = os.sysconf("SC_CLK_TCK"), time.time() - float(open("/proc/uptime").read().split()[0])
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read()
            env = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
            began = boot + int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[19]) / ticks
        except OSError:
            continue
        if b"xdg-desktop-portal" in cmd and began >= since - 1 and \
                any(e.startswith(b"DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/dbus-") for e in env):
            os.kill(int(pid), 15)


def demo(farm):
    """Made-up data for every tab."""
    from cardfarm import rules
    now, rnd = time.time(), random.Random(7)
    st, s = farm.state, farm.settings
    names = ["Ember Hollow", "Tidewater", "Paper Lanterns", "Rust & Rivets", "Night Bus", "Quiet Orchard",
             "Sky Ledger", "Bramble Keep"]
    st["games"] = [{"appid": 1000 + i, "name": n, "drops": rnd.randint(1, 5), "earned": 5} for i, n in enumerate(names)]
    st["games"].append({"appid": 730, "name": "Counter-Strike 2", "drops": 14, "earned": 65})
    st["games"].append({"appid": 2000, "name": "Pocket Arena", "drops": 6, "earned": 6})
    st["idling"] = {str(1000 + i): {"since": now - 3600 * (i + 1), "clock": now - 1800, "last_drop": now - 1200 if i == 0 else None,
                                    "restarts": 0} for i in range(3)}
    st["skipped"] = {"1006": now + 3600}
    st["no_idle"] = {"730": now + 6 * 86400}
    st["skipped_by_you"] = ["1007"]
    st["free_to_play"] = {"730": True, "2000": True, **{str(1000 + i): False for i in range(len(names))}}
    st["idled"] = {str(1000 + i): 3600 * (2 + i) for i in range(5)}
    st["farmed"] = {"1000": 3, "1001": 1, "1002": 2}
    farm.settings["never_touch"] = [1004]
    for i in range(3):
        farm.prices.cache[f"cards:{1000 + i}"] = {"at": now, "v": {f"{1000 + i}-Card {k}": 4 + i * 3 + k for k in range(5)}}
    card = lambda i, name, game, foil=False: {"assetid": f"c{i}", "appid": 1000, "name": name, "game": game,
                                             "hash": f"1000-{name}", "foil": foil, "icon": "", "goo": None}
    st["review"] = [
        {"id": "c1", "kind": "card", "card": card(1, "Lantern Keeper", "Paper Lanterns"), "action": "sell",
         "detail": {"buyer": 12, "receive": 10, "gems": 20}, "at": now},
        {"id": "c2", "kind": "card", "card": card(2, "The Long Night", "Night Bus", foil=True), "action": "sell",
         "detail": {"buyer": 84, "receive": 74, "gems": 120}, "at": now},
        {"id": "c3", "kind": "card", "card": card(3, "Orchard Gate", "Quiet Orchard"), "action": "gems",
         "detail": {"buyer": 3, "receive": 1, "gems": 15}, "at": now}]
    st["stats"] = {"drops": 37, "listed": 29, "listed_cents": 214, "gems": 340, "items_listed": 6, "items_cents": 912,
                   "foil_drops": [4, 19, 33]}  # foil flowers in the garden
    texts = [("sell", "Listed Lantern Keeper (Paper Lanterns) for $0.12 (you get $0.10)"),
             ("info", "New card: Lantern Keeper (Paper Lanterns)"), ("gems", "Turned Orchard Gate (Quiet Orchard) into 15 gems"),
             ("warn", "Steam said too many requests; market lookups wait 5 min"), ("info", "Listings checked: 41 on the market, 3 undercut"),
             ("sell", "Relisted Rust Bolt for $0.06 (was $0.09)"), ("info", "Signed in as demo")]
    st["history"] = [{"at": now - 600 * i, "kind": k, "text": t} for i, (k, t) in enumerate(texts * 3)]
    st["items"] = {"inventories": [{"key": "753/6", "appid": 753, "context": 6, "name": "Steam", "count": 88},
                                   {"key": "730/2", "appid": 730, "context": 2, "name": "Counter-Strike 2", "count": 41},
                                   {"key": "440/2", "appid": 440, "context": 2, "name": "Team Fortress 2", "count": 12}],
                   "review": [], "seen": {}, "checked": now - 900}
    s["items_inventories"] = ["730/2"]
    for i, (name, kind, price, vol, amount) in enumerate([("Dreams & Nightmares Case", "Container", 117, 98000, 1),
                                                          ("P250 | Sand Dune (Field-Tested)", "Pistol", 3, 4100, 1),
                                                          ("Sticker | Paper Moon (Holo)", "Sticker", 245, 12, 3)]):
        item = {"assetid": f"i{i}", "appid": 730, "context": 2, "amount": amount, "name": name, "hash": name,
                "game": "Counter-Strike 2", "icon": "", "kind": kind}
        st["items"]["review"].append({"id": f"i{i}", "kind": "item", "item": item, "action": "sell", "at": now,
                                      "detail": {"buyer": price, "receive": rules.received(price), "volume": vol, "median": price + 2}})
    bg = {"assetid": "i9", "appid": 753, "context": 6, "amount": 1, "name": "Harbor at Dusk (Profile Background)",
          "hash": "1001-Harbor at Dusk", "game": "Steam", "icon": "", "kind": "Profile Background", "marketable": True, "goo": [1001, 3, 0]}
    st["items"]["review"].append({"id": "i9", "kind": "item", "item": bg, "action": "gems", "at": now,
                                  "detail": {"buyer": 4, "receive": 2, "volume": 3, "median": 4, "gems": 80, "gem_value": 4.73}})
    st["items"]["worth"] = {"cents": 4127, "priced": 52, "of": 53, "at": now - 900}
    st["holding"] = {f"{1000 + i}-Card {k}": [1000 + i, 1 + (k % 2)] for i in range(4) for k in range(5)}
    st["sale"] = {"on": True, "checked": now - 1200, "day": "", "queues": 3, "sticker": True, "stickers": 4}
    st["alerts"] = [{"id": "753/1001-Harbor at Dusk/above", "appid": 753, "hash": "1001-Harbor at Dusk", "name": "Harbor at Dusk",
                     "game": "Tidewater", "icon": "", "when": "above", "cents": 20, "armed": True, "price": 12, "lowest": 13,
                     "checked": now - 1500, "fired": 0, "added": now - 86400},
                    {"id": "730/Dreams & Nightmares Case/below", "appid": 730, "hash": "Dreams & Nightmares Case",
                     "name": "Dreams & Nightmares Case", "game": "Counter-Strike 2", "icon": "", "when": "below", "cents": 120,
                     "armed": False, "price": 117, "lowest": 115, "checked": now - 1500, "fired": now - 1500, "added": now - 86400}]
    lst = lambda i, name, buyer, lowest, days, verdict, vol=6: {
        "listingid": f"L{i}", "assetid": f"A{i}", "appid": 753, "context": 6, "hash": f"1000-{name}", "name": name,
        "game": "Ember Hollow Trading Card", "icon": "", "classid": "c", "instanceid": "0", "receive": rules.received(buyer),
        "buyer": buyer, "created": now - days * 86400, "lowest": lowest, "median": lowest and lowest + 1, "volume": vol, "verdict": verdict}
    st["listings"] = {"checked": now - 600, "items": [
        lst(1, "Ash Gate", 14, 9, 3, "undercut"), lst(2, "Cinder Path", 11, 8, 6, "undercut"), lst(3, "Low Tide", 7, 7, 2, "ok"),
        lst(4, "Kite String", 9, None, 0.2, "young", None), lst(5, "Rust Bolt", 4, None, 4, "floor", None)]}
    st["sales_all"] = True
    st["idled"]["1005"] = 3600 * 140
    st["sales"] = [{"at": now - rnd.randint(0, (20 if i < 100 else 200) * 86400), "listingid": str(i), "name": f"Card {i}", "hash": "",
                    "game": rnd.choice(["Ember Hollow Trading Card", "Tidewater Trading Card", "Night Bus Trading Card"]),
                    "appid": rnd.choice([1000, 1001, 1004]), "received": rnd.randint(2, 30)} for i in range(140)]
    st["sales"].sort(key=lambda x: -x["at"])
    st["gems"] = {"gems": 2240, "sacks": 1, "gems_id": "g", "sack_ids": ["s"], "checked": now, "sack_price": 68}
    farm.prices.cache["sack"] = {"at": now, "v": 68}
    offer = lambda appid, name, gems, lowest, median, vol, unavailable=False: farm._booster_offer(
        {"appid": appid, "name": name, "series": 1, "gems": gems, "unavailable": unavailable, "available_at": "Nov 5 @ 9:12am" if unavailable else "",
         "lowest": lowest, "median": median, "volume": vol})
    st["boosters"] = {"checked": now, "done": 64, "of": 64, "every": 410, "tradable": 2240, "gems": 3240, "options": sorted([
        offer(1000, "Ember Hollow", 600, 72, 70, 9), offer(1001, "Tidewater", 750, 81, 79, 4),
        offer(1002, "Paper Lanterns", 500, 45, 41, 12, unavailable=True), offer(1003, "Rust & Rivets", 1000, 66, None, 0),
        offer(1004, "Night Bus", 400, 18, 17, 30)], key=lambda o: (not o["worth"], -(o["profit"] or -999)))}
    st["sets"] = {"checked": now, "offers": [
        {"appid": 1002, "game": "Paper Lanterns", "have": 6, "of": 6, "missing": [], "missing_hashes": [], "cost": 0, "worth": 31, "unpriced": False},
        {"appid": 1001, "game": "Tidewater", "have": 5, "of": 7, "missing": ["Gull", "Low Tide"], "missing_hashes": [], "cost": 14, "worth": 24, "unpriced": False},
        {"appid": 1005, "game": "Quiet Orchard", "have": 2, "of": 9, "missing": ["A", "B", "C", "D", "E", "F", "G"], "missing_hashes": [], "cost": 210, "worth": 9, "unpriced": False}]}
    st["finishing"] = {"1001": {"game": "Tidewater", "since": now - 600}}
    pack = lambda i, game, sell, opened, vol, helps=False: {
        "assetid": f"b{i}", "appid": 1000 + i, "name": f"{game} Booster Pack", "game": game, "hash": f"{1000 + i}-{game} Booster Pack",
        "icon": "", "marketable": True, "sell": sell, "opened": opened, "volume": vol, "helps": helps,
        "verdict": "open" if helps or opened > sell else "sell"}
    st["held"] = {"checked": now, "packs": [pack(1, "Tidewater", 41, 18, 7, helps=True), pack(3, "Rust & Rivets", 52, 21, 11),
                                            pack(6, "Sky Ledger", 9, 27, 2)]}
    lu = lambda appid, game, cost, levels, forgone=0, source="market": {
        "appid": appid, "game": game, "cost": cost, "levels": levels, "forgone": forgone, "source": source,
        "missing_hashes": [], "missing": [f"Card {k}" for k in range(7)]}
    st["levelup"] = {"level": 21, "xp": 0, "xp_to_next": 140, "maxed": 9, "checked": now, "options": [
        lu(2001, "Copper Kettle", 39, 5), lu(1001, "Tidewater", 14, 1, forgone=24, source="yours"),
        lu(2002, "Wayfinder", 48, 5), lu(2003, "Slaughter Horse 2", 103, 4)]}
    s["levelup_budget_cents"] = 300
    s["levelup_buying"] = "orders"
    st["patient"] = [{"appid": 2004, "game": "Lantern Row", "hashes": ["2004-A", "2004-B"]}] * 2
    st["orders"] = [{"orderid": f"o{k}", "hash": f"2004-{n}", "price": p, "quantity": 2, "placed": now - 3600 * (k + 1)}
                    for k, (n, p) in enumerate([("Night Market", 3), ("Paper Moon", 5), ("Gull's Way", 4)])]
    st["quick"] = {"level": 21, "xp_to_next": 140, "checked": now, "search": {"text": "", "results": [], "at": 0},
                   "names": {"2001": "Copper Kettle", "2005": "Moth Lamp", "2006": "Glass Harbor"},
                   "badges": {"1000": 3, "1001": 1, "1002": 4, "2005": 5, "2006": 5, "2001": 2}, "pick": {
        "appid": 1000, "game": "Ember Hollow", "level": 3, "set_size": 6, "wallet": 3111, "checked": now - 120,
        "cards": [{"hash": f"1000-{n}", "name": n, "have": have, "sell": sell, "listed": sum(c for _, c in sell)} for n, have, sell in [
            ("Ash Gate", 1, [[6, 31], [7, 12], [9, 4]]), ("Cinder Path", 0, [[5, 2], [6, 40], [8, 9]]),
            ("Low Tide", 2, [[7, 18], [8, 3]]), ("Kite String", 0, [[4, 1], [5, 1], [6, 22], [7, 30], [9, 3], [11, 2], [15, 1]]),
            ("Rust Bolt", 1, [[9, 6], [10, 14]]), ("Lantern Keeper", 0, [[12, 3], [13, 8]])]]}}
    farm._keys()["list"] = [
        {"key": k, "id": f"k{i}", "status": st_, "names": names, "at": now - 300 * i if st_ != "waiting" else 0, "source": "pasted"}
        for i, (k, st_, names) in enumerate([("AAAAA-BBBBB-CCC01", "waiting", []), ("AAAAA-BBBBB-CCC02", "waiting", []),
                                             ("AAAAA-BBBBB-CCC03", "added", ["Paper Lanterns"]), ("AAAAA-BBBBB-CCC04", "added", ["Night Bus: Deluxe Edition"]),
                                             ("AAAAA-BBBBB-CCC05", "owned", ["Tidewater"]), ("AAAAA-BBBBB-CCC06", "invalid", []),
                                             ("AAAAA-BBBBB-CCC07", "region", ["Sky Ledger"])])]
    farm.update = {"version": "v0.4.0-beta.1", "page": "https://example.invalid", "url": "x", "sha256": ""}
    farm.status = "Farming 3 games"


def inside(out):
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("WebKit", "6.0")
    from gi.repository import GLib, WebKit
    sys.path.insert(0, ROOT)
    from cardfarm import app as appmod, farm as farm_mod

    data = tempfile.mkdtemp(prefix="card-farm-demo-")
    with open(os.path.join(data, "session.json"), "w") as f:
        json.dump({"steamid": "76561190000000001", "account": "demo", "refresh_token": "demo"}, f)
    os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="card-farm-demo-xdg-")
    farm_mod.Farm.run = lambda self: None  # no Steam, no network

    def make():
        f = farm_mod.Farm(data)
        demo(f)
        return f

    app = appmod.CardFarmApp(make)
    checks, steps, ok = [], [], [True]

    def later(ms, fn):
        GLib.timeout_add(ms, lambda: (fn(), False)[1])

    def js(code, then):
        app.view.evaluate_javascript(code, -1, None, None, None, lambda v, r: then(v.evaluate_javascript_finish(r).to_string()))

    def step():
        if steps:
            steps.pop(0)()
        else:
            with open(os.path.join(out, "result.json"), "w") as f:
                json.dump({"ok": ok[0], "checks": checks}, f)
            app.quit()

    def check(name, code, want):
        def go():
            def got(v):
                good = v == want
                ok[0] &= good
                checks.append(f"{'PASS' if good else 'FAIL'}  {name}" + ("" if good else f" (got {v!r})"))
                later(300, step)
            js(code, got)
        return go

    def shot(name):
        def go():
            def done(v, r):
                v.get_snapshot_finish(r).save_to_png(os.path.join(out, f"{name}.png"))
                later(200, step)
            app.view.get_snapshot(WebKit.SnapshotRegion.FULL_DOCUMENT, WebKit.SnapshotOptions.NONE, None, done)
        return go

    def tab(name):
        return lambda: js(f"document.querySelector('[data-tab={name}]').click(); 'ok'", lambda v: later(600, step))

    def width(px):
        """The page px CSS pixels wide (None: as wide as the window), by zooming it; the hidden
        display keeps the window itself at full size."""
        def go():
            def got(v):
                app.view.set_zoom_level(1.0 if px is None else int(v) / px)
                later(1200, step)
            app.view.set_zoom_level(1.0)
            later(400, lambda: js("String(innerWidth)", got))
        return go

    def measure(label):
        def go():
            def got(v):
                checks.append(f"INFO  {label}: page width {v}")
                later(100, step)
            js("innerWidth + ' CSS px at ' + devicePixelRatio + 'x'", got)
        return go

    # "true", or the elements sticking out past the right edge (the outermost ones)
    no_scroll = """(() => {
      const w = document.documentElement.clientWidth;
      if (document.documentElement.scrollWidth <= w) return "true";
      const out = [...document.querySelectorAll("body *")].filter(e => e.offsetParent !== null && e.getBoundingClientRect().right > w + 1);
      const top = out.filter(e => !out.includes(e.parentElement));
      return top.slice(0, 6).map(e => e.tagName.toLowerCase() + (e.id ? "#" + e.id : "") + (e.className && typeof e.className === "string" ? "." + e.className.trim().split(/ +/).join(".") : "")
        + " " + Math.round(e.getBoundingClientRect().right) + "px").join(", ");
    })()"""
    no_errors = "String((window.__errors || []).length === 0 && !!state)"
    for size, px in (("wide", None), ("narrow", NARROW)):
        steps += [width(px), measure(size)]
        for t in TABS:
            steps += [tab(t), shot(f"{t}-{size}"), check(f"{t} ({size}): nothing scrolls sideways", no_scroll, "true")]
    def theme(name):
        return lambda: js(f"document.documentElement.dataset.theme = '{name}'; 'ok'", lambda v: later(500, step))
    steps += [width(None), theme("light")]
    for t in TABS:  # the light theme too, wide
        steps += [tab(t), shot(f"{t}-light")]
    steps.append(theme("pink"))
    for t in TABS:  # and the pink one
        steps += [tab(t), shot(f"{t}-pink")]
    steps.append(check("the page's script ran without errors", no_errors, "true"))

    def started(*a):
        catcher = WebKit.UserScript.new("window.__errors = []; addEventListener('error', e => window.__errors.push(String(e.message)));",
                                        WebKit.UserContentInjectedFrames.TOP_FRAME, WebKit.UserScriptInjectionTime.START, None, None)
        app.view.get_user_content_manager().add_script(catcher)
        app.window.set_default_size(1280, 900)
        begun = []

        def loaded(view, event):
            if event == WebKit.LoadEvent.FINISHED and not begun:
                begun.append(1)
                later(2500, step)
        app.view.connect("load-changed", loaded)
        app.view.reload()

    app.connect("activate", lambda *a: GLib.idle_add(started))
    GLib.timeout_add_seconds(200, lambda: (checks.append("FAIL  timed out"), app.quit()))
    app.run(None)


if __name__ == "__main__":
    main()
