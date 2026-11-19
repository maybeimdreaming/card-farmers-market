"""Updates from the project's GitHub releases: find a newer one, download this system's file,
check it against the SHA-256 GitHub publishes for it, and swap it in once Card Farm has closed.

The swap is done by a small helper started just before Card Farm quits (a running program can't
replace its own files on Windows, and a .pyz can't be swapped while it's being read)."""
import hashlib
import os
import platform
import re
import subprocess
import sys
import tempfile

from .http import download, get_json

REPO = "maybeimdreaming/card-farmers-market"
# each system's download, the Card Farmer's Market name first; the Card Farm names it had before are
# still found, so a release can switch to the new names once copies understand both
ASSETS = {"windows": ["Card-Farmers-Market-Windows.zip", "Card-Farm-Windows.zip"],
          "macos": ["Card-Farmers-Market-macOS.zip", "Card-Farm-macOS.zip"],
          # the Mac download runs on both kinds of Mac since 0.3.0-beta.10; the -Intel names come first
          # for releases from before that
          "macos-intel": ["Card-Farmers-Market-macOS-Intel.zip", "Card-Farm-macOS-Intel.zip",
                          "Card-Farmers-Market-macOS.zip", "Card-Farm-macOS.zip"],
          "pyz": ["card-farmers-market.pyz", "card-farm.pyz"]}

try:
    from ._version import VERSION  # written by the release build, e.g. "v0.2.0-beta.7"
except ImportError:
    VERSION = "dev"


def version_key(tag):
    """Sortable form of a tag: v0.2.0-beta.7 < v0.2.0-beta.8 < v0.2.0. None for anything else."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?", tag or "")
    if not m:
        return None
    major, minor, patch, beta = m.groups()
    return int(major), int(minor), int(patch), 0 if beta else 1, int(beta or 0)


def asset_names(kind, machine=None):
    """This computer's download, by its names (Macs with an Intel processor had their own)."""
    if kind == "macos" and (machine or platform.machine()) == "x86_64":
        return ASSETS["macos-intel"]
    return ASSETS[kind]


def install_kind():
    """How this copy was installed: 'windows', 'macos' or 'pyz' can update themselves; None
    (running from source or pip) can't."""
    if getattr(sys, "frozen", False):
        return "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else None
    return "pyz" if _pyz_path() else None


def _pyz_path():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return root if root.endswith(".pyz") and os.path.isfile(root) else None


def newest(releases, current=VERSION, kind=None):
    """The release to offer from GitHub's list, or None. Betas are offered to beta and dev copies."""
    mine = version_key(current)
    betas_ok = mine is None or mine[3] == 0
    best = None
    for r in releases:
        key = version_key(r.get("tag_name"))
        if r.get("draft") or key is None or (r.get("prerelease") and not betas_ok):
            continue
        if mine is not None and key <= mine:
            continue
        if best is None or key > best[0]:
            best = (key, r)
    if not best:
        return None
    r = best[1]
    by_name = {a["name"]: a for a in r.get("assets", [])}
    asset = next((by_name[n] for n in (asset_names(kind) if kind else []) if n in by_name), None)
    return {"version": r["tag_name"], "page": r["html_url"], "url": asset and asset["browser_download_url"],
            "sha256": asset and (asset.get("digest") or "").removeprefix("sha256:")}


def check():
    """What to offer right now (see newest), from GitHub. A copy run from source has no version
    to compare, so it's never offered anything."""
    if version_key(VERSION) is None:
        return None
    releases = get_json(f"https://api.github.com/repos/{REPO}/releases?per_page=20",
                        headers={"Accept": "application/vnd.github+json"})
    return newest(releases, VERSION, install_kind())


def fetch(offer):
    """Download the new version and check it; returns the file's path."""
    if not offer.get("url"):
        raise RuntimeError("this copy can't update itself; download the new version from GitHub")
    folder = tempfile.mkdtemp(prefix="card-farm-update-")
    path = os.path.join(folder, offer["url"].rsplit("/", 1)[-1])
    if not download(offer["url"], path):
        raise RuntimeError("the download didn't finish")
    with open(path, "rb") as f:
        got = hashlib.sha256(f.read()).hexdigest()
    if offer.get("sha256") and got != offer["sha256"]:
        raise RuntimeError("the download doesn't match the release (checksum differs); not installed")
    return path


WINDOWS_HELPER = r"""
param([int]$Wait, [string]$Zip, [string]$Target, [string]$Start)
Start-Transcript -Path (Join-Path (Split-Path $Zip) "update.log") | Out-Null
Wait-Process -Id $Wait -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("card-farm-new-" + [guid]::NewGuid())
Expand-Archive -LiteralPath $Zip -DestinationPath $tmp -Force
# the download holds one folder: "Card Farmer's Market" now, "Card Farm" before
$from = Get-ChildItem -LiteralPath $tmp -Directory | Select-Object -First 1
# /IS /IT: copy every file, even ones robocopy would call unchanged (same size, and zip times are
# rounded to 2 seconds)
robocopy $from.FullName $Target /E /IS /IT /R:5 /W:1 /NFL /NDL /NJH /NJS /NP
Write-Output "robocopy exit $LASTEXITCODE"
$exe = Get-ChildItem -LiteralPath $from.FullName -Filter *.exe | Select-Object -First 1
foreach ($old in Get-ChildItem -LiteralPath $Target -Filter *.exe) {  # a renamed program leaves its old .exe
  if ($old.Name -ne $exe.Name -and $old.Name -like "Card Farm*.exe") { Remove-Item -LiteralPath $old.FullName -Force }
}
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
if ($Start -ne "none") { Start-Process -FilePath (Join-Path $Target $exe.Name) }
Stop-Transcript | Out-Null
"""

POSIX_HELPER = r"""
while kill -0 "$1" 2>/dev/null; do sleep 1; done
case "$2" in
  macos)
    # the download holds one app ("Card Farmer's Market.app" now, "Card Farm.app" before); it goes where
    # the old one was, under its own name
    tmp=$(mktemp -d) && ditto -x -k "$3" "$tmp" || exit 1
    new=$(find "$tmp" -maxdepth 1 -name "*.app" | head -n 1)
    [ -n "$new" ] || exit 1
    dest="$(dirname "$4")/$(basename "$new")"
    rm -rf "$4.old" && mv "$4" "$4.old" && mv "$new" "$dest" && rm -rf "$4.old" "$tmp"
    [ -n "$5" ] && open "$dest" ;;
  pyz)
    chmod +x "$3" && mv -f "$3" "$4"
    [ -n "$5" ] && nohup "$4" >/dev/null 2>&1 & ;;
esac
"""


def install(path, relaunch=True, wait_for=None, target=None):
    """Start the helper that swaps in the downloaded version after this program (or wait_for)
    exits; then Card Farm should quit. target is where the app lives (found when not given)."""
    kind = install_kind() if target is None else ("windows" if sys.platform == "win32" else
                                                  "macos" if sys.platform == "darwin" else "pyz")
    pid = str(wait_for or os.getpid())
    if kind == "windows":
        target = target or os.path.dirname(sys.executable)
        script = os.path.join(os.path.dirname(path), "update.ps1")
        with open(script, "w", encoding="utf-8") as f:
            f.write(WINDOWS_HELPER)
        log = open(os.path.join(os.path.dirname(path), "update-start.log"), "wb")  # PowerShell's own complaints
        return subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                                 "-File", script, "-Wait", pid, "-Zip", path, "-Target", target, "-Start", "yes" if relaunch else "none"],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=0x00000200 | 0x08000000)  # own process group, no window
    if kind == "macos":
        target = target or re.sub(r"(\.app)/Contents/MacOS/.*$", r"\1", sys.executable)
    elif kind == "pyz":
        target = target or _pyz_path()
    else:
        raise RuntimeError("this copy can't update itself")
    return subprocess.Popen(["sh", "-c", POSIX_HELPER, "card-farm-update", pid, kind, path, target,
                             "1" if relaunch else ""], start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
