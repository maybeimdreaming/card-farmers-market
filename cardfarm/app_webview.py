"""Card Farm's window on Windows and macOS: the same page as on Linux, drawn by the system's own web
view (Edge WebView2 on Windows, WebKit on macOS) through the pywebview library. The page reaches
the farm through pywebview's in-process bridge; nothing listens on the network.

One copy at a time: starting Card Farm again brings the running copy's window back. Closing the
window while farming minimises it instead; the page's Quit stops everything."""
import json
import os
import pathlib
import threading
import time

import webview

from . import api

HERE = os.path.dirname(os.path.abspath(__file__))


class Bridge:
    """What the page calls as window.pywebview.api.request(...). pywebview runs each call on its
    own thread, so a slow farm call doesn't freeze the window."""

    def __init__(self, app):
        self._app = app

    def request(self, method, path, body=""):
        if path == "/api/quit":
            threading.Thread(target=self._app.quit, daemon=True).start()
            return [200, '{"ok": true}']
        status, _, data = api.answer(self._app.farm, method, path, (body or "").encode())
        return [status, data.decode()]


class WebviewApp:
    def __init__(self, make_farm, data_dir, background=False):
        self.make_farm, self.data_dir, self.background = make_farm, data_dir, background
        self.farm = self.window = None
        self.quitting = False

    def run(self):
        if _show_running_copy(self.data_dir, self.background):
            return 0
        self.farm = self.make_farm()
        self.farm.on_quit = self.quit
        self.farm.open_humble = lambda url, check: threading.Thread(target=self._sign_in_window, args=(url, check), daemon=True).start()
        threading.Thread(target=self.farm.run, daemon=True).start()
        # a file:// address: pywebview starts its built-in web server only for bare paths
        page = pathlib.Path(HERE, "ui", "index.html").as_uri()
        self.window = webview.create_window("Card Farmer's Market", page, js_api=Bridge(self),
                                            width=1100, height=820, min_size=(420, 500),
                                            minimized=self.background, background_color="#0f0e0c")
        self.window.events.closing += self._closing
        selftest = os.environ.get("CARD_FARM_SELFTEST")
        webview.start(self._watch, (selftest,), private_mode=False,
                      storage_path=os.path.join(self.data_dir, "webview"))
        self._shutdown()
        return 0

    def _sign_in_window(self, url, check):
        """Another site's sign-in pages (Humble's) in a window of their own. Every 2 seconds, once
        it's back from the sign-in pages, its cookies go to check(cookies); when that says signed
        in, the window closes."""
        if getattr(self, "_signing_in", None):
            return
        win = self._signing_in = webview.create_window("Sign in to Humble Bundle", url, width=560, height=780)
        closed = []
        win.events.closed += lambda: closed.append(True)
        try:
            while not closed and not self.quitting:
                time.sleep(2)
                try:
                    if not (win.get_current_url() or "").startswith(url):
                        continue
                    cookies = {}
                    for jar in win.get_cookies() or []:
                        cookies.update({name: morsel.value for name, morsel in jar.items()})
                except Exception:
                    continue
                if cookies and check(cookies):
                    win.destroy()
                    break
        finally:
            self._signing_in = None

    def _closing(self):
        """Closing while farming minimises: farming carries on. Returning False keeps the window."""
        if self.quitting:
            return True
        farming = self.farm.session.signed_in and (self.farm.settings["farming"] or self.farm.state["queue"])
        if not farming:
            return True
        self.window.minimize()
        return False

    def quit(self):
        self.quitting = True
        self.window.destroy()

    def _shutdown(self):
        self.farm.idler.stop_all()
        self.farm.save()
        _release(self.data_dir)

    def _watch(self, selftest):
        """Runs beside the window: brings it forward when Card Farm is started again, and runs the
        self-test the release builds are checked with."""
        if selftest:
            return _selftest(self, selftest)
        flag = os.path.join(self.data_dir, "show-window")
        while not self.quitting:
            if os.path.exists(flag):
                try:
                    os.remove(flag)
                except OSError:
                    pass
                self.window.restore()
                self.window.show()
            time.sleep(1)


# --- one copy at a time, without any network port ----------------------------------------------
def _lock_path(data_dir):
    return os.path.join(data_dir, "running.json")


def _show_running_copy(data_dir, background):
    """True when another Card Farm already runs for this data folder (and it's asked to show its
    window); otherwise this copy notes itself as the running one."""
    from .idler import _alive
    os.makedirs(data_dir, exist_ok=True)
    try:
        with open(_lock_path(data_dir)) as f:
            pid = json.load(f)["pid"]
        if pid != os.getpid() and _alive(pid):
            if not background:
                open(os.path.join(data_dir, "show-window"), "w").close()
            return True
    except (OSError, ValueError, KeyError):
        pass
    with open(_lock_path(data_dir), "w") as f:
        json.dump({"pid": os.getpid()}, f)
    return False


def _release(data_dir):
    try:
        os.remove(_lock_path(data_dir))
    except OSError:
        pass


def _selftest(app, out):
    """CARD_FARM_SELFTEST=<file>: wait for the page to draw itself from the farm's state through the
    bridge, then write what it shows to <file> and quit. Used to check every release build."""
    result = {"ok": False}
    for _ in range(60):
        time.sleep(1)
        try:
            shown = app.window.evaluate_js(
                "JSON.stringify({state: !!state, signin: !document.querySelector('#signin').hidden, "
                "button: document.querySelector('#start-signin').textContent})")
            result = dict(json.loads(shown), ok=True) if shown and json.loads(shown)["state"] else result
        except Exception as e:
            result["error"] = repr(e)
        if result.get("ok"):
            break
    with open(out, "w") as f:
        json.dump(result, f)
    app.quit()


def run_window(make_farm, data_dir, background=False):
    return WebviewApp(make_farm, data_dir, background).run()
