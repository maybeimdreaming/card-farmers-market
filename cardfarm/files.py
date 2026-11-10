"""Card Farm's saved files (JSON in its data folder): written whole or not at all, and a damaged one
is set aside instead of keeping the app from opening."""
import json
import os
import shutil
import time


def write_json(path, data, private=False, **kw):
    """Write to a file beside it, then swap it in: a crash or a full disk mid-write leaves the old
    file. private: only this user can read it (the Steam sign-in)."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, **kw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path, default=None, backup=False, say=print):
    """The file's contents, or `default` when it isn't there. One that can't be read is renamed to
    <name>.broken (kept, so nothing is lost for good) and the last good copy (<name>.bak, made by
    backup=True on every good read) is used instead when there is one."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        say(f"{os.path.basename(path)} couldn't be read ({e}); it's kept as {os.path.basename(path)}.broken")
        try:
            os.replace(path, path + ".broken")
        except OSError:
            pass
        if backup and os.path.exists(path + ".bak"):
            try:
                with open(path + ".bak") as f:
                    data = json.load(f)
                say(f"Went back to the last good copy of {os.path.basename(path)}")
                return data
            except (OSError, ValueError):
                pass
        return default
    if backup:
        try:
            shutil.copyfile(path, path + ".bak")
        except OSError:
            pass
    return data


LOG_SIZE = 1_000_000  # the log starts over (keeping the one before as .1) past this many bytes


def log_line(path, text):
    """Add a line to the log file: what the Activity list shows and what went wrong behind it, for
    looking into a problem afterwards (a packaged app has no terminal to print to)."""
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_SIZE:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
    except OSError:
        pass
