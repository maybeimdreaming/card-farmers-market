"""When farming may run: whether the computer is on battery power, and the hours you chose."""
import glob
import os
import subprocess
import sys
import time


def on_battery():
    """True when the computer runs off its battery (a laptop away from its charger). A desktop, or
    a system that won't say, counts as plugged in."""
    try:
        if sys.platform == "win32":
            return _windows()
        if sys.platform == "darwin":
            out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10).stdout
            return "'Battery Power'" in out
        return _linux()
    except Exception:
        return False


def _linux(root="/sys/class/power_supply"):
    supplies = [p for p in glob.glob(os.path.join(root, "*")) if _read(p, "scope") != "Device"]  # not a mouse's battery
    kinds = {p: _read(p, "type") for p in supplies}
    if any(k == "Mains" and _read(p, "online") == "1" for p, k in kinds.items()):
        return False
    batteries = [p for p, k in kinds.items() if k == "Battery"]
    return any(_read(p, "status") == "Discharging" for p in batteries)


def _read(folder, name):
    try:
        with open(os.path.join(folder, name)) as f:
            return f.read().strip()
    except OSError:
        return ""


def _windows():
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte), ("BatteryLifePercent", ctypes.c_byte),
                    ("SystemStatusFlag", ctypes.c_byte), ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    s = Status()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
        return False
    return s.ACLineStatus == 0  # 0 offline, 1 online, 255 unknown


def in_hours(start, end, now=None):
    """Whether the hour of the day falls in [start, end); start == end means all day, and a span
    past midnight (22 to 7) wraps around."""
    if start == end:
        return True
    hour = time.localtime(now).tm_hour
    return start <= hour < end if start < end else hour >= start or hour < end
