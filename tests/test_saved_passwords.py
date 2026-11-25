import asyncio
import json
import os

import pytest

from brickmote.credentials import SavedPasswords, protect, unprotect
from brickmote.crypto.handshake import HandshakeError, initiator_handshake, responder_handshake
from brickmote.crypto.identity import Identity
from brickmote.crypto.password import PasswordVerifier

from .conftest import stream_pair
from .test_core import FakeUI, make_core
from .test_handshake import PASSWORD, make_host, unattended
from .test_session import Recorder


def test_store_roundtrip_and_permissions(tmp_path):
    store = SavedPasswords(tmp_path / "saved.json")
    params = {"salt": "AAAAAAAAAAAAAAAAAAAAAA==", "n": 16384, "r": 8, "p": 1}
    store.save("123456789", params, b"k" * 32)
    again = SavedPasswords(tmp_path / "saved.json")
    assert again.get("123456789") == (params, b"k" * 32)
    assert again.devices() == {"123456789"}
    if os.name == "posix":
        assert (tmp_path / "saved.json").stat().st_mode & 0o077 == 0
    raw = (tmp_path / "saved.json").read_text()
    assert "password" not in raw.lower() and PASSWORD not in raw
    again.forget("123456789")
    assert SavedPasswords(tmp_path / "saved.json").get("123456789") is None


def test_corrupted_entries_are_ignored(tmp_path):
    (tmp_path / "s.json").write_text(json.dumps({"1": {"params": {}, "secret": "plain:@@"}, "2": "x"}))
    store = SavedPasswords(tmp_path / "s.json")
    assert store.get("1") is None and store.get("2") is None
    assert unprotect(protect(b"z" * 32)) == b"z" * 32
    assert unprotect("dpapi:AAAA") is None or os.name == "nt"


async def _pair(policy, **kwargs):
    (vr, vw), (hr, hw) = await stream_pair()
    host = asyncio.create_task(responder_handshake(hr, hw, policy, "t"))
    try:
        v = await initiator_handshake(vr, vw, Identity.generate(), "Viewer", **kwargs)
    except HandshakeError:
        vw.close()
        try:
            await asyncio.wait_for(host, 5)
        except (HandshakeError, asyncio.TimeoutError):
            pass
        raise
    return v, await host


async def test_ask_mode_stops_before_the_host_prompt(tmp_path):
    prompts = []

    async def prompt(peer, sas):
        prompts.append(peer)
        return {"control": True}

    pv = PasswordVerifier.create(PASSWORD, n=2**14)
    _, _, policy = make_host(tmp_path, prompt=prompt, unattended_enabled=True, password=pv.to_dict())
    with pytest.raises(HandshakeError) as err:
        await _pair(policy, want="ask")
    assert err.value.code == "password_offered"
    assert err.value.peer is not None and err.value.interactive_ok is True
    await asyncio.sleep(0.2)
    assert prompts == [], "the person at the remote computer must not be disturbed"


async def test_saved_secret_replaces_the_password(tmp_path):
    ident, s, policy = unattended(tmp_path)
    v, _ = await _pair(policy, password=PASSWORD, want="password")
    saved = v.extra["pw_secret"]
    assert len(saved[1]) == 32 and "secret" not in saved[0]
    v2, _ = await _pair(policy, want="ask", saved_secret=saved)
    assert v2.method == "password"
    # the host's password changes: the remembered key no longer matches
    s.password = PasswordVerifier.create("Nouveau-Mot-2027", n=2**14).to_dict()
    with pytest.raises(HandshakeError) as err:
        await _pair(policy, want="ask", saved_secret=saved)
    assert err.value.code == "password_offered"


class AskingUI(FakeUI):
    def __init__(self, answers):
        super().__init__()
        self.answers = list(answers)
        self.asked = 0

    async def ask_password(self, peer, interactive_ok):
        self.asked += 1
        return self.answers.pop(0) if self.answers else None


async def test_core_prompts_remembers_and_reuses(tmp_path):
    pv = PasswordVerifier.create(PASSWORD, n=2**14)
    host, host_ui = make_core(tmp_path, "host", unattended_enabled=True, password=pv.to_dict())
    viewer, _ = make_core(tmp_path, "viewer")
    ui = AskingUI([{"password": PASSWORD, "remember": True}])
    viewer.ui = ui
    await host.start()
    await viewer.start()
    target = f"127.0.0.1:{host.direct_port}"

    s1 = await viewer.connect(target, None, Recorder())
    assert s1.method == "password" and ui.asked == 1 and host_ui.prompts == []
    assert viewer.saved_passwords.has(host.identity.device_id)
    await s1.close()

    s2 = await viewer.connect(host.identity.device_id, None, Recorder())
    assert s2.method == "password" and ui.asked == 1, "remembered: no question the second time"
    await s2.close()

    # the user chooses "ask for authorisation" instead of typing a password
    viewer.saved_passwords.forget(host.identity.device_id)
    ui.answers = [{"interactive": True}]
    s3 = await viewer.connect(target, None, Recorder())
    assert s3.method == "interactive" and len(host_ui.prompts) == 1
    await s3.close()

    ui.answers = [None]
    from brickmote.core import ConnectError

    with pytest.raises(ConnectError, match="annulée"):
        await viewer.connect(target, None, Recorder())
    await viewer.stop()
    await host.stop()
