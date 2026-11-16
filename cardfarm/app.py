"""Card Farm's window (GTK 4 + libadwaita). The page inside is drawn by WebKit from Card Farm's own
files and talks to the farm in this same program through a private cardfarm:// address: nothing
listens on the network, and no browser can open it.

One copy at a time: opening Card Farm again brings its window back. Closing the window while
farming keeps farming in the background; Quit (in the menu) stops everything."""
import json
import os
import pkgutil
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
gi.require_version("Soup", "3.0")
from gi.repository import Adw, Gio, GLib, Gtk, Soup, WebKit  # noqa: E402

from . import api, humble  # noqa: E402

APP_ID = "io.github.cardfarm.CardFarm"  # kept from the Card Farm days: the menu entry, icon and settings use it
NAME = "Card Farmer's Market"
HOME = "cardfarm://app/"


class CardFarmApp(Adw.Application):
    def __init__(self, make_farm, background=False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.make_farm, self.start_hidden = make_farm, background
        self.farm = None
        self.window = self.view = None
        self.held = False

    def do_startup(self):
        Adw.Application.do_startup(self)
        GLib.set_application_name(NAME)
        _install_icon()
        Gtk.Window.set_default_icon_name(APP_ID)
        self.farm = self.make_farm()
        self.farm.notify = lambda title, body: GLib.idle_add(self._notify, title, body)
        self.farm.on_quit = lambda: GLib.idle_add(self.quit)
        self.farm.open_humble = lambda url, check: GLib.idle_add(self._sign_in_window, url, check)
        self.farm.humble_page = self._humble_page
        threading.Thread(target=self.farm.run, daemon=True).start()
        GLib.timeout_add_seconds(2, self._scan_flag)
        WebKit.WebContext.get_default().register_uri_scheme("cardfarm", self._answer)
        for name, accels, act in (("quit", ["<Control>q"], self.quit),
                                  ("reload", ["<Control>r"], lambda: self.view and self.view.reload())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *a, act=act: act())
            self.add_action(action)
            self.set_accels_for_action(f"app.{name}", accels)

    def do_activate(self):
        if self.start_hidden:  # `card-farm --background`: farm now, show the window when asked
            self.start_hidden = False
            self._hold()
            return
        if self.window:
            self.window.present()
            return
        self.window = Adw.ApplicationWindow(application=self, title=NAME, default_width=1100, default_height=820)
        self.window.connect("close-request", self._on_close)

        menu = Gio.Menu()
        menu.append("Reload", "app.reload")
        menu.append("Quit", "app.quit")
        header = Adw.HeaderBar()
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))

        self.view = WebKit.WebView()
        self.view.connect("decide-policy", self._stay_inside)
        self.view.connect("context-menu", lambda *a: True)  # no browser menu (Back, Inspect, ...)
        self.view.load_uri(HOME)

        body = Adw.ToolbarView()
        body.add_top_bar(header)
        body.set_content(self.view)
        self.window.set_content(body)
        self.window.present()
        if os.environ.get("CARD_FARM_SELFTEST"):
            GLib.timeout_add_seconds(1, self._selftest, os.environ["CARD_FARM_SELFTEST"], [60])

    def _selftest(self, out, tries):
        """CARD_FARM_SELFTEST=<file>: once the page has drawn the farm's state, write what it shows
        to <file> and quit (how builds are checked)."""
        def done(view, result):
            try:
                shown = json.loads(view.evaluate_javascript_finish(result).to_string())
            except Exception as e:
                shown = {"state": False, "error": repr(e)}
            if shown.get("state") or tries[0] <= 0:
                with open(out, "w") as f:
                    json.dump(dict(shown, ok=bool(shown.get("state"))), f)
                self.quit()
            else:
                tries[0] -= 1
                GLib.timeout_add_seconds(1, self._selftest, out, tries)
        self.view.evaluate_javascript("JSON.stringify({state: !!state, signin: !document.querySelector('#signin').hidden, "
                                      "button: document.querySelector('#start-signin').textContent})", -1, None, None, None, done)
        return False

    def _answer(self, request):
        """A request from the page: read it here, answer it on a worker thread (the farm may take a
        moment), and hand the answer back on GTK's thread."""
        method, path = request.get_http_method() or "GET", request.get_path() or "/"
        body, stream = b"", request.get_http_body()
        while stream:
            chunk = stream.read_bytes(65536, None).get_data()
            if not chunk:
                break
            body += chunk

        def work():
            status, kind, data = api.answer(self.farm, method, path, body)
            GLib.idle_add(self._finish, request, status, kind, data)
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _finish(request, status, kind, data):
        response = WebKit.URISchemeResponse.new(Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data)), len(data))
        response.set_status(status, None)
        response.set_content_type(kind)
        request.finish_with_response(response)
        return False

    def _stay_inside(self, view, decision, kind):
        """Links that leave Card Farm open in the normal browser."""
        if kind not in (WebKit.PolicyDecisionType.NAVIGATION_ACTION, WebKit.PolicyDecisionType.NEW_WINDOW_ACTION):
            return False
        uri = decision.get_navigation_action().get_request().get_uri()
        if uri.startswith(HOME) or uri.startswith("about:"):
            return False
        Gtk.UriLauncher.new(uri).launch(self.window, None, None, None)
        decision.ignore()
        return True

    def _sign_in_window(self, url, check):
        """Another site's sign-in pages (Humble's) in a window of their own, with cookies kept apart
        from the app's page. Every 2 seconds its cookies go to check(cookies) on a worker thread;
        once that says signed in, the window closes. Nothing is remembered by the window itself."""
        if getattr(self, "_signing_in", None):
            self._signing_in.present()
            return False
        session = WebKit.NetworkSession.new_ephemeral()
        view = WebKit.WebView(network_session=session)
        win = Gtk.Window(title="Sign in to Humble Bundle", transient_for=self.window, default_width=560, default_height=780)
        win.set_child(view)
        self._signing_in = win
        done = []

        def closed(*a):
            done.append(True)
            self._signing_in = None
            return False
        win.connect("close-request", closed)
        # a sign-in that opens a popup (Google's) goes on in this same window instead
        view.connect("create", lambda v, action: (v.load_request(action.get_request()), None)[1])
        view.load_uri(url)
        win.present()

        def got(manager, result):
            try:
                cookies = {c.get_name(): c.get_value() for c in manager.get_cookies_finish(result)}
            except GLib.Error:
                return
            threading.Thread(target=lambda: check(cookies) and GLib.idle_add(win.close), daemon=True).start()

        def poll():
            if done:
                return False
            if (view.get_uri() or "").startswith(url):  # back from the sign-in pages
                session.get_cookie_manager().get_cookies(url, None, got)
            return True
        GLib.timeout_add_seconds(2, poll)
        return False

    def _humble_page(self, url, work, show=False):
        """A page of Humble's (the library, an item) in a window of its own, minimised unless `show`
        (Humble ignores Reveal presses in a minimised window), holding the
        saved Humble sign-in; returns work(js), js(code) running code in that page and returning
        its value as text. The farm's thread waits here; the window's work happens on GTK's."""
        box, loaded = {}, threading.Event()

        def on_main(fn):
            """Run fn on GTK's thread and wait for what it hands to done(value)."""
            out, ready = {}, threading.Event()
            GLib.idle_add(lambda: (fn(lambda v=None, e=None: (out.update(v=v, e=e), ready.set())), False)[1])
            if not ready.wait(30):
                raise ValueError("Humble's window stopped answering")
            if out.get("e"):
                raise ValueError(out["e"])
            return out.get("v")

        def open_window(done):
            session = WebKit.NetworkSession.new_ephemeral()
            view = WebKit.WebView(network_session=session)
            win = Gtk.Window(title="Humble Bundle (Card Farmer's Market)", default_width=760, default_height=720)
            win.set_child(view)
            win.connect("close-request", lambda *a: box.update(closed=True) or False)
            view.connect("load-changed", lambda v, event: event == WebKit.LoadEvent.FINISHED and loaded.set())
            box.update(win=win, view=view)
            cookies = list((self.farm._humble().get("cookies") or {}).items())
            pending = [len(cookies)]

            def added(manager, result):
                manager.add_cookie_finish(result)
                pending[0] -= 1
                if not pending[0]:
                    view.load_uri(url)
                    win.present()
                    if not show:
                        win.minimize()
                    done()
            for name, value in cookies:
                c = Soup.Cookie.new(name, value, "www.humblebundle.com", "/", -1)
                c.set_secure(True)
                session.get_cookie_manager().add_cookie(c, None, added)
            if not cookies:
                done(e="not signed in to Humble")

        def js(code):
            if box.get("closed"):
                raise ValueError("the Humble window was closed")

            def run(done):
                def got(view, result):
                    try:
                        v = view.evaluate_javascript_finish(result)
                        done("" if v is None or v.is_undefined() or v.is_null() else v.to_string())
                    except GLib.Error as e:
                        done(e=e.message)
                box["view"].evaluate_javascript(code, -1, None, None, None, got)
            return on_main(run)

        try:
            on_main(open_window)
            if not loaded.wait(60):
                raise ValueError("Humble's library page didn't load")
            time.sleep(2)
            if not (js("location.href") or "").startswith(humble.SITE + "/home/library"):
                raise ValueError("Humble signed this app out - sign in to Humble again")
            return work(js)
        finally:
            # let go of the window and its view on GTK's thread: WebKit aborts the whole app when a
            # view's last reference goes on another thread (Python's garbage collector, here)
            def close():
                box.pop("view", None)
                win = box.pop("win", None)
                if win:
                    win.destroy()
                return False
            GLib.idle_add(close)

    def _scan_flag(self):
        """`touch humble-scan` in the data folder: scan the Humble library (from outside the window)."""
        flag = os.path.join(self.farm.data, "humble-scan")
        if os.path.exists(flag):
            try:
                os.remove(flag)
                self.farm.humble_scan()
            except Exception as e:
                self.farm.log(f"Couldn't start the Humble scan: {e}", "warn")
        return True

    def _hold(self):
        if not self.held:
            self.hold()  # keep running with no window
            self.held = True

    def _on_close(self, window):
        farming = self.farm.session.signed_in and (self.farm.settings["farming"] or self.farm.state["queue"])
        if not farming:
            self.quit()
            return False
        window.set_visible(False)
        if not self.held:
            self._hold()
            self._notify("Card Farmer's Market keeps farming", "It's running in the background. Open it again "
                                                    "to see it, or use Quit in its menu to stop.")
        return True

    def _notify(self, title, body):
        note = Gio.Notification.new(title)
        note.set_body(body)
        self.send_notification(None, note)
        return False

    def do_shutdown(self):
        if self.farm:
            self.farm.idler.stop_all()
            self.farm.save()
        Adw.Application.do_shutdown(self)


def _install_icon():
    """Put Card Farm's icon where the desktop looks for app icons (the menu entry and the window
    use it by name)."""
    icon = pkgutil.get_data("cardfarm", "ui/icon.svg")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    dest = os.path.join(base, "icons", "hicolor", "scalable", "apps", f"{APP_ID}.svg")
    try:
        if not os.path.exists(dest) or open(dest, "rb").read() != icon:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(icon)
    except OSError:
        pass


def run_window(make_farm, background=False):
    app = CardFarmApp(make_farm, background)
    app.register(None)
    if app.get_is_remote():  # already running: show that one (or leave it be, for --background)
        if not background:
            app.activate()
        return 0
    return app.run(None)
