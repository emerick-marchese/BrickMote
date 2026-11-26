"""Secure automatic updates.

Every release published by the CI contains ``SHA256SUMS.txt`` and
``SHA256SUMS.txt.sig``: an Ed25519 signature of the checksum list, made with a
private key kept in a GitHub secret. Installed copies of BrickMote pin the
public key below and refuse any update that is not signed by it, even if the
GitHub account or the release files were tampered with.

* The file to install must be listed in the *signed* checksum list under the
  exact name of the new version, so an old signed release cannot be replayed
  as a newer one (no downgrade attack).
* Its SHA-256 must match before it is ever executed.
* Windows (installed version): a scheduled task created by the installer runs
  ``BrickMote.exe --update-service`` as SYSTEM at every boot. It downloads
  into the Program Files folder (not writable by normal users), verifies and
  silently runs the new installer. No user interaction, no UAC prompt.
* Other platforms: the application shows a notification with a download link.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import ssl
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import __version__

log = logging.getLogger(__name__)

REPO = "emerick-marchese/BrickMote"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"

# Public keys allowed to sign releases (several to allow a key rotation).
UPDATE_PUBLIC_KEYS = ("H6Ib9vpCnxuqcbzxLyr1uXU5vzd0xzDTknr4hfVF4Do=",)

SUMS_NAME = "SHA256SUMS.txt"
SIG_NAME = "SHA256SUMS.txt.sig"
MAX_DOWNLOAD = 400 * 1024 * 1024
MAX_METADATA = 1024 * 1024
TASK_NAME = "BrickMote\\Update"

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

Getter = Callable[[str, int], bytes]


class UpdateError(Exception):
    pass


def parse_version(text: object) -> Optional[tuple[int, int, int]]:
    m = _VERSION.match(text.strip()) if isinstance(text, str) else None
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(remote: str, local: str = __version__) -> bool:
    r, l_ = parse_version(remote), parse_version(local)
    return bool(r and l_ and r > l_)


# --------------------------------------------------------------------- http
def _ssl_context() -> ssl.SSLContext:
    """System certificate store (Windows store, corporate proxies...) plus
    the certifi bundle (needed by frozen macOS/Linux builds). Certificate and
    host name verification always stay enabled."""
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError):
        pass
    return ctx


def _open(url: str, timeout: float = 30):
    if not url.startswith("https://"):
        raise UpdateError("adresse de téléchargement non sécurisée")
    req = urllib.request.Request(url, headers={"User-Agent": f"BrickMote-Updater/{__version__}"})
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


def http_get(url: str, limit: int) -> bytes:
    with _open(url) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("réponse trop volumineuse")
    return data


# ------------------------------------------------------------------ release
@dataclass
class Release:
    version: str  # "1.2.3"
    tag: str
    page: str
    assets: dict = field(default_factory=dict)  # name -> (url, size)
    notes: str = ""  # release notes (NOT signed: display as plain text only)


def fetch_latest(get: Getter = http_get) -> Optional[Release]:
    data = json.loads(get(API_LATEST, MAX_METADATA))
    tag = data.get("tag_name")
    version = parse_version(tag)
    if version is None or data.get("draft") or data.get("prerelease"):
        return None
    assets = {}
    for a in data.get("assets") or []:
        name, url, size = a.get("name"), a.get("browser_download_url"), a.get("size")
        if isinstance(name, str) and isinstance(url, str) and url.startswith("https://") and isinstance(size, int):
            assets[name] = (url, size)
    page = data.get("html_url") if isinstance(data.get("html_url"), str) else RELEASES_PAGE
    notes = data.get("body") if isinstance(data.get("body"), str) else ""
    return Release(".".join(map(str, version)), tag, page, assets, notes[:20000])


def release_highlights(notes: str, limit: int = 6, width: int = 220) -> list[str]:
    """Bullet points of the release notes, as short plain text (no markup,
    no links): the notes are not covered by the signature."""
    out = []
    for line in notes.splitlines():
        line = line.strip()
        if line.startswith("## ") and out:
            break  # only the first section ("Nouveautés")
        if not line.startswith(("- ", "* ")):
            continue
        text = line[2:]
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [label](url) -> label
        text = re.sub(r"[*_`#>]", "", text)
        text = "".join(ch for ch in text if ch.isprintable()).strip()
        first, sep, _rest = text.partition(". ")
        if sep:
            text = first + "."  # first sentence only: readable at a glance
        if text:
            out.append(text if len(text) <= width else text[: width - 1].rstrip() + "…")
        if len(out) >= limit:
            break
    return out


def install_kind() -> str:
    """How this copy was installed: decides what kind of update is possible."""
    if not getattr(sys, "frozen", False):
        return "source"
    exe = Path(sys.executable).resolve()
    if sys.platform.startswith("win"):
        return "windows-installed" if (exe.parent / "unins000.exe").exists() else "windows-portable"
    if sys.platform == "darwin":
        return "macos"
    return "linux-deb" if str(exe).startswith("/opt/brickmote/") else "linux-portable"


def asset_name(version: str, kind: str) -> Optional[str]:
    return {
        "windows-installed": f"BrickMote-Setup-{version}-Windows-x64.exe",
        "windows-portable": f"BrickMote-{version}-Windows-x64-portable.exe",
        "macos": f"BrickMote-{version}-macOS-AppleSilicon.dmg",
        "linux-deb": f"BrickMote-{version}-Linux-x64.deb",
        "linux-portable": f"BrickMote-{version}-Linux-x64.tar.gz",
    }.get(kind)


# ------------------------------------------------------------- verification
def verify_sums(sums: bytes, signature: bytes, keys: Optional[tuple] = None) -> dict:
    """Check the Ed25519 signature of the checksum list and parse it."""
    keys = keys or UPDATE_PUBLIC_KEYS
    try:
        sig = base64.b64decode(signature.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UpdateError("signature illisible") from exc
    for key in keys:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(sig, sums)
            break
        except (InvalidSignature, ValueError):
            continue
    else:
        raise UpdateError("signature invalide : mise à jour refusée")
    hashes = {}
    for line in sums.decode("utf-8", "strict").splitlines():
        m = re.fullmatch(r"([0-9a-f]{64})\s+\*?(\S+)", line.strip())
        if m:
            hashes[m.group(2)] = m.group(1)
    return hashes


def download_verified(release: Release, name: str, dest_dir: Path, get: Getter = http_get) -> Path:
    """Download ``name`` from ``release`` and return its path once verified."""
    for needed in (SUMS_NAME, SIG_NAME, name):
        if needed not in release.assets:
            raise UpdateError(f"fichier absent de la release : {needed}")
    sums = get(release.assets[SUMS_NAME][0], MAX_METADATA)
    signature = get(release.assets[SIG_NAME][0], 4096)
    hashes = verify_sums(sums, signature)
    expected = hashes.get(name)
    if expected is None:
        raise UpdateError("fichier non couvert par la signature")
    url, size = release.assets[name]
    if size > MAX_DOWNLOAD:
        raise UpdateError("fichier trop volumineux")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / name
    partial = dest_dir / (name + ".part")
    digest = hashlib.sha256()
    total = 0
    try:
        with _open(url, timeout=60) as resp, open(partial, "wb") as out:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD:
                    raise UpdateError("fichier trop volumineux")
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != expected:
            raise UpdateError("empreinte SHA-256 incorrecte : fichier altéré, mise à jour refusée")
        os.replace(partial, final)
    finally:
        if partial.exists():
            partial.unlink()
    return final


def check_for_update(get: Getter = http_get) -> Optional[Release]:
    """Return the latest release if it is newer than this version."""
    release = fetch_latest(get)
    if release is not None and is_newer(release.version):
        return release
    return None


# ------------------------------------------------------------- Windows side
def _windows_installer_args(relaunch: bool) -> list[str]:
    args = ["/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"]
    if relaunch:
        return ["/SILENT", *args, "/RELAUNCH=1"]
    return ["/VERYSILENT", *args]


def launch_installer_elevated(path: Path) -> None:
    """User clicked 'Installer maintenant': Windows asks for confirmation (UAC)."""
    import ctypes

    params = " ".join(_windows_installer_args(relaunch=True))
    rc = ctypes.windll.shell32.ShellExecuteW(None, "open", str(path), params, None, 1)
    if rc <= 32:
        raise UpdateError("installation annulée")


def app_running() -> bool:
    """Is a BrickMote window/tray instance running (other than this process)?"""
    try:
        import psutil
    except ImportError:
        return False
    me = os.getpid()
    exe = os.path.normcase(os.path.realpath(sys.executable))
    for proc in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            if proc.info["pid"] == me or not proc.info["exe"]:
                continue
            if os.path.normcase(os.path.realpath(proc.info["exe"])) != exe:
                continue
            if "--update-service" in (proc.info["cmdline"] or []):
                continue
            return True
        except (psutil.Error, OSError):
            continue
    return False


def wait_for_app_exit(timeout: float = 120.0, step: float = 2.0, running=None) -> bool:
    """The application asked for its own update and is closing: give it time."""
    import time as _time

    running = running or app_running
    deadline = _time.monotonic() + timeout
    while running():
        if _time.monotonic() >= deadline:
            return False
        _time.sleep(step)
    return True


INNO_APP_ID = "{8F3C2A61-6B0E-4C7B-9C52-3E1F0B7D2A94}_is1"
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def request_service_update() -> bool:
    """Ask the SYSTEM update task to install now (no UAC prompt). Works when
    the installer allowed users to start it (1.0.9 and later)."""
    try:
        result = subprocess.run(
            ["schtasks", "/Run", "/TN", TASK_NAME],
            capture_output=True,
            timeout=20,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("update task not started: %s", exc)
        return False
    if result.returncode != 0:
        log.info("update task refused (%s)", result.returncode)
    return result.returncode == 0


def setup_running() -> bool:
    """Is a BrickMote installer running (update being installed)?"""
    try:
        import psutil
    except ImportError:
        return False
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info["name"] or "").lower().startswith("brickmote-setup"):
                return True
        except psutil.Error:
            continue
    return False


def relauncher_script(version: Optional[str], exe: str, minimized: bool, timeout_min: int = 15) -> str:
    """PowerShell run hidden, as the user: waits until the update service has
    installed ``version``, then starts BrickMote again (also after a timeout,
    so the computer never stays without BrickMote)."""
    if version is not None and not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise UpdateError("version invalide")
    installed = f"$v -eq '{version}' -and " if version else ""
    exe_ps = exe.replace("'", "''")
    args = "'--minimized'" if minimized else "@()"
    keys = ", ".join(
        f"'{root}\\SOFTWARE\\{view}Microsoft\\Windows\\CurrentVersion\\Uninstall\\{INNO_APP_ID}'"
        for root in ("HKLM:",)
        for view in ("", "WOW6432Node\\")
    )
    return (
        "$ErrorActionPreference='SilentlyContinue';"
        f"$deadline=(Get-Date).AddMinutes({int(timeout_min)});"
        "Start-Sleep -Seconds 8;"
        "while((Get-Date) -lt $deadline){"
        f"$v=($({keys}) | ForEach-Object {{ (Get-ItemProperty -Path $_).DisplayVersion }} | Where-Object {{ $_ }} | Select-Object -First 1);"
        "$setup=Get-Process -Name 'BrickMote-Setup*';"
        f"if({installed}-not $setup){{break}};"
        "Start-Sleep -Seconds 3};"
        "Start-Sleep -Seconds 2;"
        f"if(-not (Get-Process -Name 'BrickMote' | Where-Object {{ $_.SessionId -eq (Get-Process -Id $PID).SessionId }})){{"
        f"Start-Process -FilePath '{exe_ps}' -ArgumentList {args}}}"
    )


def spawn_relauncher(version: Optional[str], minimized: bool) -> None:
    exe = str(Path(sys.executable).resolve())
    script = relauncher_script(version, exe, minimized)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", encoded],
        creationflags=_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        close_fds=True,
    )


def _wait_for_release_check(attempts: int = 12, delay: float = 15.0) -> Optional[Release]:
    """At boot the network may not be up yet: retry for a few minutes."""
    import time as _time

    for i in range(attempts):
        try:
            return check_for_update()
        except (OSError, UpdateError, ValueError) as exc:
            if i == attempts - 1:
                raise
            log.info("réseau pas encore prêt (%s), nouvel essai", exc.__class__.__name__)
            _time.sleep(delay)
    return None


def run_update_service() -> int:
    """Entry point of the SYSTEM scheduled task (``--update-service``)."""
    if install_kind() != "windows-installed":
        return 0
    app_dir = Path(sys.executable).resolve().parent
    updates = app_dir / "updates"  # inside Program Files: only admins can write here
    updates.mkdir(exist_ok=True)
    logging.basicConfig(
        filename=str(updates / "update.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        for old in updates.glob("BrickMote-Setup-*.exe"):
            old.unlink(missing_ok=True)
        release = _wait_for_release_check()
        if release is None:
            log.info("BrickMote %s est à jour", __version__)
            return 0
        name = asset_name(release.version, "windows-installed")
        installer = download_verified(release, name, updates)
        if not wait_for_app_exit():
            # BrickMote is open and did not ask for this update: installing now
            # would close it without restarting it, leaving this computer
            # unreachable. The application installs it itself when it starts
            # (mandatory update), which closes it and restarts it afterwards.
            log.info("BrickMote est déjà ouvert : mise à jour %s reportée", release.version)
            return 0
        log.info("installation de la version %s (signature et empreinte vérifiées)", release.version)
        subprocess.Popen(
            [str(installer), *_windows_installer_args(relaunch=False), f"/LOG={updates / 'install.log'}"],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            close_fds=True,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - never crash at boot; retry next boot
        log.warning("mise à jour impossible : %s", exc)
        return 1
