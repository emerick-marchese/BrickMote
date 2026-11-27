"""Lock screen support: protocol and helper logic everywhere, Windows plumbing
on Windows (run by the CI on a real Windows machine)."""

import io
import socket
import sys
import threading

import numpy as np
import pytest

from brickmote.host import securedesk as sd


class Duplex:
    """A socket used like the pipe stream (read / write / close)."""

    def __init__(self, sock):
        self.sock = sock

    def read(self, n):
        return self.sock.recv(n)

    def write(self, data):
        self.sock.sendall(data)

    def close(self):
        self.sock.close()


class Screen:
    def __init__(self):
        self.closed = 0

    def grab(self, index):
        return 4, 2, bytes(range(32))

    def close(self):
        self.closed += 1


class Keys:
    def __init__(self):
        self.calls = []
        self.monitor = None

    def set_monitor(self, mon):
        self.monitor = mon

    def __getattr__(self, name):
        if name in sd.INPUT_METHODS:
            return lambda *a: self.calls.append((name, *a))
        raise AttributeError(name)


def make_agent(desktop="Winlogon", uac=False):
    keys = Keys()
    state = {"desktop": desktop, "uac": uac}
    agent = sd.DesktopAgent(Screen, lambda: keys, desktop=lambda: state["desktop"], uac=lambda: state["uac"])
    return agent, keys, state


def test_protocol_round_trip_and_limits():
    buf = io.BytesIO()
    sd.write_msg(buf, {"op": "grab", "index": 1}, b"\x00\x01")
    buf.seek(0)
    header, payload = sd.read_msg(buf)
    assert header["op"] == "grab" and payload == b"\x00\x01"
    bad = io.BytesIO(sd.HEADER.pack(sd.MAX_HEADER + 1))
    with pytest.raises(sd.SecureDeskError):
        sd.read_msg(bad)
    with pytest.raises(sd.SecureDeskError):
        sd.read_msg(io.BytesIO(b"\x00\x00"))  # cut short


def test_agent_captures_and_replays_input():
    agent, keys, state = make_agent()
    reply, data = agent.handle({"op": "grab", "index": 1}, b"")
    assert (reply["w"], reply["h"], reply["desktop"]) == (4, 2, "Winlogon") and len(data) == 32
    mon = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    reply, _ = agent.handle({"op": "input", "m": "key", "a": [True, "enter", None], "mon": mon}, b"")
    assert reply["ok"] and keys.calls == [("key", True, "enter", None)] and keys.monitor == mon


def test_agent_refuses_anything_but_input_methods():
    agent, keys, _ = make_agent()
    for method in ("set_monitor", "_ensure", "__class__", "cursor", None):
        reply, _ = agent.handle({"op": "input", "m": method, "a": []}, b"")
        assert reply.get("error") == "refused"
    reply, _ = agent.handle({"op": "input", "m": "key", "a": list(range(9))}, b"")
    assert reply.get("error") == "refused"
    assert keys.calls == []


def test_agent_never_answers_a_uac_prompt():
    agent, keys, state = make_agent(uac=True)
    reply, _ = agent.handle({"op": "input", "m": "mouse_button", "a": ["left", True, 10, 10]}, b"")
    assert reply["error"] == "uac" and reply["uac"] is True
    agent.handle({"op": "input", "m": "release_all", "a": []}, b"")  # releasing is always allowed
    assert keys.calls == [("release_all",)]
    reply, data = agent.handle({"op": "grab", "index": 1}, b"")
    assert data, "the prompt can still be seen"


def test_agent_restarts_capture_when_the_desktop_changes():
    agent, keys, state = make_agent()
    agent.handle({"op": "grab", "index": 1}, b"")
    first = agent._capture
    state["desktop"] = "Default"
    agent.handle({"op": "grab", "index": 1}, b"")
    assert agent._capture is not first and first.closed == 1


def test_client_and_server_over_a_stream():
    a, b = socket.socketpair()
    agent, keys, _ = make_agent()
    threading.Thread(target=sd.serve, args=(Duplex(b), agent), daemon=True).start()
    client = sd.SecureDesktopClient(connect=lambda: Duplex(a))
    client.set_monitor({"left": 0, "top": 0, "width": 800, "height": 600})
    assert client.grab(1)[:2] == (4, 2)
    assert client.state == {"desktop": "Winlogon", "uac": False}
    client.mouse_move(10, 20)
    client.key(False, "enter")
    assert keys.calls == [("mouse_move", 10, 20), ("key", False, "enter", None)]
    client.close()


def test_client_reports_an_absent_helper_and_retries_later():
    calls = []

    def refuse():
        calls.append(1)
        raise OSError("pas d'assistant")

    client = sd.SecureDesktopClient(connect=refuse)
    with pytest.raises(sd.SecureDeskError):
        client.grab(1)
    with pytest.raises(sd.SecureDeskError):
        client.grab(1)  # within the retry delay: not even tried
    assert calls == [1]
    client.mouse_move(1, 1)  # input never raises


def test_helper_refuses_non_user_accounts():
    if sys.platform != "win32":
        assert sd.run_helper("S-1-5-18") == 1
    else:
        assert sd.run_helper("S-1-5-18") == 2  # SYSTEM itself
        assert sd.run_helper("S-1-1-0") == 2  # Everyone


# ------------------------------------------------------------ Windows only
windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows uniquement")


@windows
def test_windows_identity_helpers():
    import ctypes
    from ctypes import wintypes

    token = wintypes.HANDLE()
    assert sd.OpenProcessToken(sd.GetCurrentProcess(), 0x0008, ctypes.byref(token))
    sid = sd.token_user_sid(token)
    sd.CloseHandle(token)
    assert sid.startswith("S-1-5-")
    assert sd.current_session_id() >= 0
    assert sd.same_program(sd.GetCurrentProcessId())
    assert not sd.same_program(4)  # the System process
    sd.enable_privileges("SeChangeNotifyPrivilege")  # held by everyone: must not fail
    assert sd.console_user(0xFFFFFFFF) is None


@windows
def test_windows_pipe_round_trip_with_acl():
    import ctypes
    from ctypes import wintypes

    token = wintypes.HANDLE()
    assert sd.OpenProcessToken(sd.GetCurrentProcess(), 0x0008, ctypes.byref(token))
    sid = sd.token_user_sid(token)
    sd.CloseHandle(token)
    session = sd.current_session_id()
    name = sd.pipe_name(session) + "-test"
    agent, keys, _ = make_agent()
    stop = threading.Event()
    threading.Thread(target=sd.serve_pipe, args=(name, sid, agent, session, stop), daemon=True).start()
    import time

    error = None
    for _ in range(50):
        try:
            stream = sd.PipeStream.connect(name)
            break
        except OSError as exc:
            error = exc
            time.sleep(0.1)
    else:
        pytest.fail(f"pipe not reachable: {error!r}")
    client = sd.SecureDesktopClient(connect=lambda: stream)
    w, h, data = client.grab(1)
    assert (w, h, len(data)) == (4, 2, 32)
    client.key(True, None, "a")
    assert keys.calls == [("key", True, None, "a")]
    stop.set()
    client.close()


@windows
def test_windows_input_desktop_and_capture():
    try:
        name = sd.follow_input_desktop()
    except OSError as exc:
        pytest.skip(f"no interactive desktop on this machine: {exc}")
    assert name
    agent = sd.DesktopAgent()
    reply, data = agent.handle({"op": "grab", "index": 1}, b"")
    if reply.get("error"):
        pytest.skip(f"capture unavailable here: {reply['error']}")
    assert len(data) == reply["w"] * reply["h"] * 4
    assert np.frombuffer(data, dtype=np.uint8).size == len(data)
