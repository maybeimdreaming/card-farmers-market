"""What the window's page can ask of the farm. The page reaches it through Card Farm's own
cardfarm:// address inside the window; there's no web server and no network port."""
import json
import pkgutil

PAGES = {"/": ("index.html", "text/html; charset=utf-8"), "/icon.svg": ("icon.svg", "image/svg+xml")}


def answer(farm, method, path, body):
    """(status, content type, bytes) for one request from the page."""
    if method == "GET":
        if path in PAGES:
            name, kind = PAGES[path]
            return 200, kind, pkgutil.get_data("cardfarm", f"ui/{name}")  # also from inside a .pyz
        if path == "/api/state":
            return _json(200, farm.snapshot())
        return _json(404, {"error": "not found"})
    try:
        result = route(farm, path, json.loads(body or b"{}"))
    except Exception as e:
        return _json(400, {"error": str(e) or e.__class__.__name__})
    return _json(200, result if result is not None else {"ok": True})


def _json(status, data):
    return status, "application/json", json.dumps(data).encode()


def route(farm, path, body):
    if path == "/api/sign-in":
        farm.start_sign_in()
        return None
    if path == "/api/sign-out":
        return farm.sign_out()
    if path == "/api/settings":
        return farm.update_settings(body)
    if path in ("/api/review", "/api/items/review"):  # {"ids": [...] | "all", "action": ...}
        items = path == "/api/items/review"
        allowed = ("sell", "gems", "keep", "always", "suggested") if items else ("sell", "gems", "keep", "suggested")
        if body.get("action") not in allowed:
            raise ValueError("unknown action")
        ids = body.get("ids")
        return farm.decide_review(ids if ids == "all" else set(map(str, ids or [])), body["action"], items=items)
    if path == "/api/review-existing":
        return farm.review_existing()
    if path == "/api/items/check":
        return farm.check_items()
    if path == "/api/skip":
        return farm.skip(int(body["appid"]))
    if path == "/api/unskip":  # {"appid": n | "all"}
        return farm.unskip_all() if body.get("appid") == "all" else farm.unskip(int(body["appid"]))
    if path == "/api/listings/check":
        return farm.check_listings()
    if path == "/api/listings":  # {"ids": [...] | "undercut", "action": reprice|cancel}
        if body.get("action") not in ("reprice", "cancel"):
            raise ValueError("unknown action")
        ids = body.get("ids")
        return farm.decide_listings(ids if ids == "undercut" else set(map(str, ids or [])), body["action"])
    if path == "/api/gems/check":
        return farm.check_gems()
    if path == "/api/gems/pack":
        return farm.pack_gems()
    if path == "/api/gems/sell":
        return farm.sell_sacks()
    if path == "/api/boosters/check":
        return farm.check_boosters()
    if path == "/api/boosters/make":  # {"appids": [...] | "profitable"}
        a = body.get("appids")
        return farm.make_boosters(a if a == "profitable" else [int(x) for x in a or []])
    if path == "/api/sets/check":
        return farm.check_sets()
    if path == "/api/sets/stop":
        return farm.stop_finishing(int(body["appid"]))
    if path == "/api/sets/finish":
        return farm.finish_set(int(body["appid"]))
    if path == "/api/held/check":
        return farm.check_held()
    if path == "/api/held":  # {"ids": [...] | "suggested", "action": sell|open|suggested}
        if body.get("action") not in ("sell", "open", "suggested"):
            raise ValueError("unknown action")
        ids = body.get("ids")
        return farm.decide_held(ids if ids == "suggested" else set(map(str, ids or [])), body["action"])
    if path == "/api/orders/cancel":  # {"ids": [...] | "all"}
        ids = body.get("ids")
        return farm.cancel_orders(ids if ids == "all" else set(map(str, ids or [])))
    if path == "/api/orders/stop":
        return farm.stop_waiting(int(body["appid"]))
    if path == "/api/levelup/check":
        return farm.check_levelup()
    if path == "/api/levelup/go":
        return farm.level_up()
    if path == "/api/badges/check":
        return farm.check_badges()
    if path == "/api/badges/search":  # {"text"}
        return farm.search_badges(body.get("text"))
    if path == "/api/badges/price":  # {"appid", "game"?}
        return farm.price_badge(int(body["appid"]), body.get("game"))
    if path == "/api/badges/go":  # {"appid", "target"}
        return farm.quick_badge(int(body["appid"]), int(body["target"]))
    if path == "/api/alerts/search":  # {"text"}
        return farm.search_alerts(body.get("text"))
    if path == "/api/alerts/add":  # {"item": {appid, hash, name, game, icon}, "when": above|below, "cents"}
        return farm.add_alert(body["item"], body.get("when"), body.get("cents", 0))
    if path == "/api/alerts/remove":  # {"id"}
        return farm.remove_alert(str(body["id"]))
    if path == "/api/sales/export":
        return farm.export_sales()
    if path == "/api/update":
        return farm.install_update()
    if path == "/api/refresh":
        return farm.refresh()
    raise ValueError("unknown action")
