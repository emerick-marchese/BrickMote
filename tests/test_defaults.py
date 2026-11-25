import importlib
import json
import sys
import types

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "packaging"))
import embed_relay  # noqa: E402

from brickmote import defaults
from brickmote.config import Settings

FP = "AC:CF:02:58:04:DE:5C:09:B5:75:B8:34:BB:5C:21:92:73:7F:2D:D1:75:40:C2:E5:FC:B5:82:19:DA:5A:1A:E0"


def _module(text):
    mod = types.ModuleType("brickmote._embedded")
    exec(text, mod.__dict__)
    return mod


def test_roundtrip_and_address_not_in_clear():
    cfg = embed_relay.parse("relais-secret.example.org:25001", FP)
    text = embed_relay.build_module(cfg)
    assert "relais-secret" not in text and "25001" not in text
    assert FP.replace(":", "").lower()[:16] not in text.lower()
    mod = _module(text)
    relay = defaults.decode(mod.K, mod.D)
    assert relay.host == "relais-secret.example.org"
    assert relay.ports == (25001,)
    assert relay.fingerprint == FP.replace(":", "").lower()
    assert "relais-secret" not in repr(relay) and "25001" not in repr(relay)


def test_each_build_scrambles_differently():
    cfg = embed_relay.parse("203.0.113.7:6568,443", FP)
    a, b = embed_relay.build_module(cfg), embed_relay.build_module(cfg)
    assert a != b
    assert "203.0.113.7" not in a
    assert defaults.decode(_module(a).K, _module(a).D).ports == (6568, 443)


@pytest.mark.parametrize(
    "value,fp",
    [("noport", FP), ("host:0", FP), ("host:abc", FP), ("bad host!:1", FP), ("host:1", "1234")],
)
def test_invalid_secrets_are_rejected(value, fp):
    with pytest.raises(ValueError):
        embed_relay.parse(value, fp)


def test_corrupted_data_is_ignored():
    assert defaults.decode("xx", "yy") is None
    assert defaults.decode("", "") is None


def test_no_embedded_module_means_no_default(monkeypatch):
    monkeypatch.setitem(sys.modules, "brickmote._embedded", None)
    assert defaults.builtin_relay() is None


def test_embedded_module_is_used(monkeypatch):
    text = embed_relay.build_module(embed_relay.parse("relay.example.net:7000", FP))
    monkeypatch.setitem(sys.modules, "brickmote._embedded", _module(text))
    relay = defaults.builtin_relay()
    assert relay is not None and relay.host == "relay.example.net"


def test_old_settings_with_a_relay_become_custom(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"relay_host": "mon.relais.fr"}))
    assert Settings.load(p).relay_mode == "custom"
    p.write_text(json.dumps({"device_name": "x"}))
    assert Settings.load(p).relay_mode == "default"
