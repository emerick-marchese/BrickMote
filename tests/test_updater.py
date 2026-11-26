import base64
import hashlib
import io
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from brickmote import updater
from brickmote.updater import Release, UpdateError

ROOT = Path(__file__).resolve().parents[1]


def keypair():
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, base64.b64encode(pub).decode()


def sums_for(files: dict) -> bytes:
    return "".join(f"{hashlib.sha256(d).hexdigest()}  {n}\n" for n, d in files.items()).encode()


class FakeServer:
    """Serves release files instead of GitHub."""

    def __init__(self, files: dict):
        self.files = files

    def get(self, url, limit):
        data = self.files[url.rsplit("/", 1)[-1]]
        if len(data) > limit:
            raise UpdateError("too big")
        return data

    @contextmanager
    def open(self, url, timeout=30):
        yield io.BytesIO(self.files[url.rsplit("/", 1)[-1]])


def make_release(version, files):
    return Release(
        version, f"v{version}", "https://example.invalid",
        {n: (f"https://example.invalid/{n}", len(d)) for n, d in files.items()},
    )


@pytest.fixture
def signed(monkeypatch):
    key, pub = keypair()
    monkeypatch.setattr(updater, "UPDATE_PUBLIC_KEYS", (pub,))

    def build(version, payload=b"installer bytes", tamper=None, sign_with=None):
        name = updater.asset_name(version, "windows-installed")
        files = {name: payload}
        sums = sums_for(files)
        sig = base64.b64encode((sign_with or key).sign(sums))
        files = {**files, updater.SUMS_NAME: sums, updater.SIG_NAME: sig}
        if tamper:
            tamper(files)
        server = FakeServer(files)
        monkeypatch.setattr(updater, "_open", server.open)
        return make_release(version, files), server, name

    return build


def test_versions():
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("1.0") is None and updater.parse_version(None) is None
    assert updater.is_newer("v1.0.10", "1.0.9")
    assert not updater.is_newer("v1.0.1", "1.0.1")
    assert not updater.is_newer("v0.9.0", "1.0.1")


def test_asset_names():
    assert updater.asset_name("1.2.3", "windows-installed") == "BrickMote-Setup-1.2.3-Windows-x64.exe"
    assert updater.asset_name("1.2.3", "linux-deb") == "BrickMote-1.2.3-Linux-x64.deb"
    assert updater.asset_name("1.2.3", "source") is None


def test_verified_download(signed, tmp_path):
    release, server, name = signed("9.0.0", payload=b"genuine installer")
    path = updater.download_verified(release, name, tmp_path, get=server.get)
    assert path.read_bytes() == b"genuine installer"


def test_tampered_file_is_refused(signed, tmp_path):
    def swap(files):
        files["BrickMote-Setup-9.0.0-Windows-x64.exe"] = b"malware"

    release, server, name = signed("9.0.0", tamper=swap)
    dest = tmp_path / "downloads"
    with pytest.raises(UpdateError, match="altéré"):
        updater.download_verified(release, name, dest, get=server.get)
    assert not list(dest.iterdir()), "nothing may be left on disk"


def test_forged_checksums_are_refused(signed, tmp_path):
    def forge(files):
        files[updater.SUMS_NAME] = sums_for({"BrickMote-Setup-9.0.0-Windows-x64.exe": b"malware"})
        files["BrickMote-Setup-9.0.0-Windows-x64.exe"] = b"malware"

    release, server, name = signed("9.0.0", tamper=forge)
    with pytest.raises(UpdateError, match="signature invalide"):
        updater.download_verified(release, name, tmp_path, get=server.get)


def test_other_key_is_refused(signed, tmp_path):
    attacker, _ = keypair()
    release, server, name = signed("9.0.0", sign_with=attacker)
    with pytest.raises(UpdateError, match="signature invalide"):
        updater.download_verified(release, name, tmp_path, get=server.get)


def test_unsigned_release_is_refused(signed, tmp_path):
    release, server, name = signed("9.0.0", tamper=lambda f: f.pop(updater.SIG_NAME))
    assert updater.SIG_NAME not in release.assets
    with pytest.raises(UpdateError, match="absent"):
        updater.download_verified(release, name, tmp_path, get=server.get)


def test_old_signed_release_cannot_be_replayed_as_new(signed, tmp_path):
    old, server, old_name = signed("1.0.0", payload=b"old vulnerable version")
    # the attacker publishes v9.9.9 re-using the genuine, signed files of 1.0.0
    fake = Release("9.9.9", "v9.9.9", "x", dict(old.assets))
    fake.assets[updater.asset_name("9.9.9", "windows-installed")] = old.assets[old_name]
    with pytest.raises(UpdateError, match="non couvert"):
        updater.download_verified(fake, updater.asset_name("9.9.9", "windows-installed"), tmp_path, get=server.get)


def test_fetch_latest_parsing():
    payload = {
        "tag_name": "v2.0.0",
        "html_url": "https://github.com/x/y/releases/tag/v2.0.0",
        "assets": [
            {"name": "ok.exe", "browser_download_url": "https://a/ok.exe", "size": 10},
            {"name": "plain.exe", "browser_download_url": "http://a/plain.exe", "size": 10},
        ],
    }
    rel = updater.fetch_latest(lambda url, limit: json.dumps(payload).encode())
    assert rel.version == "2.0.0" and list(rel.assets) == ["ok.exe"]
    for bad in ({**payload, "prerelease": True}, {**payload, "tag_name": "nightly"}):
        assert updater.fetch_latest(lambda url, limit, b=bad: json.dumps(b).encode()) is None
    same = {**payload, "tag_name": f"v{updater.__version__}"}
    assert updater.check_for_update(lambda url, limit: json.dumps(same).encode()) is None


def test_sign_release_script(tmp_path, monkeypatch):
    key, pub = keypair()
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    sums = tmp_path / "SHA256SUMS.txt"
    sums.write_bytes(sums_for({"a.exe": b"x"}))
    env = {**os.environ, "BRICKMOTE_UPDATE_SIGNING_KEY": base64.b64encode(seed).decode()}
    script = ROOT / "packaging" / "sign_release.py"
    # a key that is not pinned in the application must be refused
    out = subprocess.run([sys.executable, str(script), str(sums)], env=env, capture_output=True, text=True)
    assert out.returncode == 1 and "ne correspond" in out.stderr
    # with the key pinned, the signature verifies
    monkeypatch.setattr(updater, "UPDATE_PUBLIC_KEYS", (pub,))
    sys.path.insert(0, str(ROOT / "packaging"))
    import sign_release

    monkeypatch.setenv("BRICKMOTE_UPDATE_SIGNING_KEY", base64.b64encode(seed).decode())
    assert sign_release.main(str(sums)) == 0
    assert updater.verify_sums(sums.read_bytes(), (tmp_path / "SHA256SUMS.txt.sig").read_bytes()) == {
        "a.exe": hashlib.sha256(b"x").hexdigest()
    }


def test_release_highlights_are_plain_and_short():
    notes = (ROOT / "release-notes" / "v1.0.3.md").read_text()
    items = updater.release_highlights(notes)
    assert 1 <= len(items) <= 6
    assert all("**" not in i and "`" not in i for i in items)
    hostile = "- Cliquez [ici](https://evil.example) <img src=x onerror=alert(1)> **vite**\n" + "- x" * 0
    item = updater.release_highlights(hostile)[0]
    assert "evil.example" not in item and item.startswith("Cliquez ici")
    assert len(updater.release_highlights("- " + "a" * 1000)[0]) <= 220


def test_fetch_latest_keeps_notes():
    payload = {"tag_name": "v9.0.0", "body": "- Nouveauté", "assets": []}
    rel = updater.fetch_latest(lambda url, limit: json.dumps(payload).encode())
    assert rel.notes == "- Nouveauté"


def test_service_postpones_when_the_app_stays_open(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "install_kind", lambda: "windows-installed")
    monkeypatch.setattr(updater.sys, "executable", str(tmp_path / "BrickMote.exe"))
    monkeypatch.setattr(updater, "_wait_for_release_check", lambda: Release("9.9.9", "v9.9.9", "x", {}))
    monkeypatch.setattr(updater, "download_verified", lambda *a, **k: tmp_path / "setup.exe")
    monkeypatch.setattr(updater, "wait_for_app_exit", lambda: False)
    launched = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **k: launched.append(a))
    assert updater.run_update_service() == 0
    assert launched == [], "never close a running BrickMote that did not ask for it"


def test_service_installs_once_the_app_has_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "install_kind", lambda: "windows-installed")
    monkeypatch.setattr(updater.sys, "executable", str(tmp_path / "BrickMote.exe"))
    monkeypatch.setattr(updater, "_wait_for_release_check", lambda: Release("9.9.9", "v9.9.9", "x", {}))
    monkeypatch.setattr(updater, "download_verified", lambda *a, **k: tmp_path / "setup.exe")
    monkeypatch.setattr(updater, "wait_for_app_exit", lambda: True)
    launched = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda args, **k: launched.append(args))
    assert updater.run_update_service() == 0
    assert launched and launched[0][0].endswith("setup.exe") and "/VERYSILENT" in launched[0]


def test_wait_for_app_exit():
    states = iter([True, True, False])
    assert updater.wait_for_app_exit(timeout=5, step=0.01, running=lambda: next(states))
    assert not updater.wait_for_app_exit(timeout=0.05, step=0.01, running=lambda: True)


def test_relauncher_waits_for_the_new_version_and_is_injection_proof():
    script = updater.relauncher_script("1.2.3", r"C:\Program Files\Brick'Mote\BrickMote.exe", minimized=True)
    assert "-eq '1.2.3'" in script and "'--minimized'" in script
    assert "Brick''Mote" in script  # quotes escaped
    assert updater.INNO_APP_ID in script
    for bad in ("1.2.3'; Remove-Item C:\\ -Recurse; '", "latest", ""):
        with pytest.raises(UpdateError):
            updater.relauncher_script(bad, "x.exe", minimized=False)


def test_request_service_update(monkeypatch):
    class Result:
        def __init__(self, rc):
            self.returncode = rc

    calls = []
    monkeypatch.setattr(updater.subprocess, "run", lambda args, **k: calls.append(args) or Result(0))
    assert updater.request_service_update()
    assert calls[0][:4] == ["schtasks", "/Run", "/TN", updater.TASK_NAME]
    monkeypatch.setattr(updater.subprocess, "run", lambda args, **k: Result(1))  # access denied (old install)
    assert not updater.request_service_update()

    def boom(*a, **k):
        raise OSError("schtasks absent")

    monkeypatch.setattr(updater.subprocess, "run", boom)
    assert not updater.request_service_update()


def test_service_retries_until_the_network_is_up(monkeypatch):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise OSError("network down")
        return None

    monkeypatch.setattr(updater, "check_for_update", flaky)
    assert updater._wait_for_release_check(attempts=5, delay=0) is None
    assert len(calls) == 3


def test_relauncher_without_version_only_waits_for_the_installer():
    script = updater.relauncher_script(None, "C:\\BrickMote.exe", minimized=False)
    assert "-eq '" not in script and "if(-not $setup)" in script and "@()" in script
