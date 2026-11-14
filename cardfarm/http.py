"""HTTP through curl: Steam's market answers curl but rate-limits Python's own HTTP client."""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse

QUIET = {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # no console window flashing up
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"


class HttpError(Exception):
    def __init__(self, status, body="", url=""):
        where = f" from {urllib.parse.urlsplit(url).path}" if url else ""
        if status == 429:
            text = "Steam says too many requests (HTTP 429); waiting before trying again"
        elif status == 0:
            text = "couldn't reach Steam (no connection, or it took too long); trying again soon"
        elif status in (401, 403):
            text = f"Steam turned the request away as signed out (HTTP {status}); if it keeps happening, sign in again"
        elif status >= 500:
            text = f"Steam's servers are having trouble (HTTP {status}); trying again soon"
        else:
            text = f"HTTP {status}{where}"
        super().__init__(text)
        self.status, self.body, self.url = status, body, url


# After Steam says "too many requests", every request to that Steam site (the community, the store,
# the Web API: each has its own limit) is turned away here for a while, 5 minutes, then 10, up to 30,
# instead of each part of the app asking again and keeping the limit tripped.
PAUSING = ("steamcommunity.com", "store.steampowered.com", "api.steampowered.com")
_pause, _pause_lock = {}, threading.Lock()  # host -> {"until": when, "minutes": the last pause}
on_pause = None  # called with (host, minutes) when a pause starts, to tell the page


def paused_until(url):
    host = urllib.parse.urlsplit(url).hostname
    with _pause_lock:
        until = _pause.get(host, {}).get("until", 0)
    return until if until > time.time() else 0


def _note(url, status):
    host = urllib.parse.urlsplit(url).hostname
    if host not in PAUSING:
        return
    with _pause_lock:
        p = _pause.setdefault(host, {"until": 0, "minutes": 0})
        if status == 429:
            p["minutes"] = min(p["minutes"] * 2 or 5, 30)
            p["until"] = time.time() + p["minutes"] * 60
            minutes = p["minutes"]
        else:
            if status == 200:
                p["minutes"] = 0
            return
    if on_pause:
        on_pause(host, minutes)


def _quoted(text):
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def request(url, data=None, cookies=None, headers=None, timeout=30, ua=UA, raw=None):
    """Returns (status, body text). data (a dict) makes it a form POST; raw (a str) is sent as the
    body exactly as given - say a multipart form, with its Content-Type in headers.

    Cookies, headers and the form go to curl as a config on stdin, never on its command line,
    which any program on the PC can read. While Steam has asked that site to slow down, this
    answers 429 itself without asking. ua=None sends curl's own User-Agent (see steam.market_page)."""
    if paused_until(url):
        return 429, ""
    config = [f"url = {_quoted(url)}"]
    if cookies:
        config.append(f"cookie = {_quoted('; '.join(f'{k}={v}' for k, v in cookies.items()))}")
    for k, v in (headers or {}).items():
        config.append(f"header = {_quoted(f'{k}: {v}')}")
    if data is not None:
        config.append(f"data-binary = {_quoted(urllib.parse.urlencode(data))}")
    elif raw is not None:
        # curl's config reads \r and \n inside quotes as line breaks: a multipart body needs them
        config.append(f"data-binary = {_quoted(raw).replace(chr(13), chr(92) + 'r').replace(chr(10), chr(92) + 'n')}")
    # -c: keep cookies Steam sets on the way (the inventory page redirects through a cookie check)
    cmd = ["curl", "-s", "-L", "--compressed", "-c", os.devnull, "--max-time", str(timeout), *(["-A", ua] if ua else []), "-w", "\n%{http_code}", "-K", "-"]
    out = subprocess.run(cmd, input="\n".join(config).encode() + b"\n",
                         capture_output=True, timeout=timeout + 10, **QUIET).stdout.decode("utf-8", "replace")
    body, _, status = out.rpartition("\n")
    _note(url, int(status or 0))
    return int(status or 0), body


def post_for_cookies(url, data, timeout=30):
    """POST a form and return (status, body, {name: value} of the cookies the reply sets)."""
    config = f"url = {_quoted(url)}\ndata-binary = {_quoted(urllib.parse.urlencode(data))}\n"
    cmd = ["curl", "-s", "-D", "-", "--compressed", "--max-time", str(timeout), "-A", UA, "-K", "-"]
    out = subprocess.run(cmd, input=config.encode(), capture_output=True, timeout=timeout + 10,
                         **QUIET).stdout.decode("utf-8", "replace")
    head, _, body = out.replace("\r\n", "\n").partition("\n\n")
    lines = head.split("\n")
    status = int(lines[0].split()[1]) if lines and len(lines[0].split()) > 1 else 0
    cookies = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "set-cookie":
            k, _, v = value.strip().split(";", 1)[0].partition("=")
            cookies[k] = v
    return status, body, cookies


def download(url, path, timeout=600):
    """Save a file (following redirects); True when it arrived whole."""
    cmd = ["curl", "-s", "-L", "-f", "--max-time", str(timeout), "-A", UA, "-o", path, "-K", "-"]
    done = subprocess.run(cmd, input=f"url = {_quoted(url)}\n".encode(), capture_output=True,
                          timeout=timeout + 10, **QUIET)
    return done.returncode == 0 and os.path.exists(path)


def get_json(url, **kw):
    status, body = request(url, **kw)
    if status != 200:
        raise HttpError(status, body[:300], url)
    return json.loads(body)


def post_json(url, data, **kw):
    status, body = request(url, data=data, **kw)
    try:
        parsed = json.loads(body)
    except ValueError:
        raise HttpError(status, body[:300], url)
    if status != 200 and not isinstance(parsed, dict):
        raise HttpError(status, body[:300], url)
    return parsed
