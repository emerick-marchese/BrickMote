"""Windows lock screen support (like AnyDesk).

The lock screen, Ctrl+Alt+Del and UAC prompts are drawn on a protected
desktop ("Winlogon") that only SYSTEM can capture or control. BrickMote runs as
the user, so three pieces work together:

* the **desktop service** (``--desktop-service``), a SYSTEM scheduled task
  started with Windows. It does nothing but keep a helper running in the
  session of the person logged on at the console;
* the **helper** (``--desktop-helper``), a SYSTEM process in that session. It
  follows the input desktop (lock screen or normal desktop) and, on request,
  captures the screen or replays mouse / keyboard events. It answers only on
  a named pipe restricted to that session's user (pipe ACL), and only to the
  BrickMote program installed on this computer (checked for every client);
* the **application**, which uses the helper only while the secure desktop
  is shown (``SecureDesktopClient``); everything else stays as it was.

The helper never clicks on a UAC prompt: otherwise any program of the user
could use it to grant itself administrator rights. The prompt can be seen,
and must be answered at the computer.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
PIPE_PREFIX = "\\\\.\\pipe\\BrickMote-secure-"
MAX_HEADER = 64 * 1024
MAX_PAYLOAD = 512 * 1024 * 1024
INPUT_METHODS = {"mouse_move", "mouse_button", "scroll", "key", "release_all"}
HEADER = struct.Struct(">I")


class SecureDeskError(Exception):
    pass


# ------------------------------------------------------------------ protocol
def write_msg(stream, header: dict, payload: bytes = b"") -> None:
    data = json.dumps({**header, "n": len(payload)}, separators=(",", ":")).encode()
    stream.write(HEADER.pack(len(data)) + data)
    if payload:
        stream.write(payload)


def _read_exact(stream, n: int) -> bytes:
    chunks, got = [], 0
    while got < n:
        chunk = stream.read(n - got)
        if not chunk:
            raise SecureDeskError("canal fermé")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def read_msg(stream) -> tuple[dict, bytes]:
    (size,) = HEADER.unpack(_read_exact(stream, HEADER.size))
    if size > MAX_HEADER:
        raise SecureDeskError("en-tête trop grand")
    header = json.loads(_read_exact(stream, size))
    if not isinstance(header, dict):
        raise SecureDeskError("message invalide")
    n = header.get("n", 0)
    if not isinstance(n, int) or not 0 <= n <= MAX_PAYLOAD:
        raise SecureDeskError("message invalide")
    return header, _read_exact(stream, n) if n else b""


# ------------------------------------------------------------- the agent
class DesktopAgent:
    """Runs in the helper, on a single thread: follows the input desktop,
    captures it and replays input on it."""

    def __init__(
        self,
        capture_factory: Optional[Callable] = None,
        injector_factory: Optional[Callable] = None,
        desktop: Optional[Callable[[], str]] = None,
        uac: Optional[Callable[[], bool]] = None,
    ):
        if capture_factory is None:
            from .capture import MSSCapture as capture_factory  # noqa: N813
        if injector_factory is None:
            from .inputs import InputInjector as injector_factory  # noqa: N813
        self._capture_factory = capture_factory
        self._injector_factory = injector_factory
        self._desktop = desktop or follow_input_desktop
        self._uac = uac or uac_prompt_shown
        self._name = None
        self._capture = None
        self._injector = None
        self._monitor = None

    def _sync(self) -> str:
        name = self._desktop()
        if name != self._name:
            # new desktop (locked / unlocked): GDI and input handles belong to
            # the previous one, start again
            self._name = name
            self._close()
        if self._capture is None:
            self._capture = self._capture_factory()
        if self._injector is None:
            self._injector = self._injector_factory()
            if self._monitor:
                self._injector.set_monitor(self._monitor)
        return name

    def _close(self) -> None:
        if self._capture is not None:
            try:
                self._capture.close()
            except Exception:  # noqa: BLE001
                pass
        self._capture = None
        self._injector = None

    def handle(self, header: dict, payload: bytes) -> tuple[dict, bytes]:
        op = header.get("op")
        try:
            name = self._sync()
        except Exception as exc:  # noqa: BLE001 - desktop switching right now: next request
            self._name = None
            self._close()
            return {"desktop": "", "uac": True, "error": f"bureau : {exc}"}, b""
        uac = bool(self._uac()) if name.lower() != "default" else False
        state = {"desktop": name, "uac": uac}
        if op == "state":
            return state, b""
        if op == "grab":
            index = header.get("index")
            if not isinstance(index, int) or isinstance(index, bool):
                return {**state, "error": "index"}, b""
            try:
                width, height, raw = self._capture.grab(index)
            except Exception as exc:  # noqa: BLE001 - desktop switched meanwhile...
                self._close()
                return {**state, "error": f"capture: {exc}"}, b""
            return {**state, "w": width, "h": height}, raw
        if op == "input":
            method, args = header.get("m"), header.get("a")
            if method not in INPUT_METHODS or not isinstance(args, list) or len(args) > 5:
                return {**state, "error": "refused"}, b""
            if uac and method != "release_all":
                return {**state, "error": "uac"}, b""  # never answer a UAC prompt remotely
            monitor = header.get("mon")
            if isinstance(monitor, dict) and monitor != self._monitor and _valid_monitor(monitor):
                self._monitor = dict(monitor)
                self._injector.set_monitor(self._monitor)
            try:
                getattr(self._injector, method)(*args)
            except Exception as exc:  # noqa: BLE001
                return {**state, "error": f"input: {exc}"}, b""
            return {**state, "ok": True}, b""
        return {**state, "error": "unknown"}, b""


def _valid_monitor(mon: dict) -> bool:
    return all(isinstance(mon.get(k), int) and not isinstance(mon.get(k), bool) for k in ("left", "top", "width", "height")) and (
        0 < mon["width"] <= 65536 and 0 < mon["height"] <= 65536
    )


def serve(stream, agent: DesktopAgent) -> None:
    """Answer one client until it goes away."""
    while True:
        try:
            header, payload = read_msg(stream)
        except (SecureDeskError, ValueError, OSError):
            return
        reply, data = agent.handle(header, payload)
        try:
            write_msg(stream, reply, data)
        except OSError:
            return


# -------------------------------------------------------------- the client
class SecureDesktopClient:
    """Used by the application while the secure desktop is displayed."""

    def __init__(self, connect: Optional[Callable] = None):
        self._connect = connect or (lambda: PipeStream.connect(pipe_name(current_session_id())))
        self._stream = None
        self._lock = threading.Lock()
        self._retry_at = 0.0
        self.monitor: Optional[dict] = None
        self.state: dict = {}

    def _request(self, header: dict) -> tuple[dict, bytes]:
        with self._lock:
            if self._stream is None:
                if time.monotonic() < self._retry_at:
                    raise SecureDeskError("assistant indisponible")
                try:
                    self._stream = self._connect()
                except (OSError, SecureDeskError) as exc:
                    self._retry_at = time.monotonic() + 3.0
                    raise SecureDeskError(f"assistant indisponible : {exc}") from exc
            try:
                write_msg(self._stream, header)
                reply, data = read_msg(self._stream)
            except (OSError, SecureDeskError, ValueError) as exc:
                self._drop()
                raise SecureDeskError(f"assistant injoignable : {exc}") from exc
            self.state = {"desktop": reply.get("desktop", ""), "uac": bool(reply.get("uac"))}
            return reply, data

    def _drop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
        self._stream = None

    def close(self) -> None:
        with self._lock:
            self._drop()

    def grab(self, index: int) -> tuple[int, int, bytes]:
        reply, data = self._request({"op": "grab", "index": int(index)})
        w, h = reply.get("w"), reply.get("h")
        if reply.get("error") or not isinstance(w, int) or not isinstance(h, int) or len(data) != w * h * 4:
            raise SecureDeskError(str(reply.get("error") or "image invalide"))
        return w, h, data

    def set_monitor(self, monitor: dict) -> None:
        self.monitor = dict(monitor)

    def _input(self, method: str, *args) -> None:
        header = {"op": "input", "m": method, "a": list(args)}
        if self.monitor:
            header["mon"] = self.monitor
        try:
            self._request(header)
        except SecureDeskError:
            pass  # lost input while the helper restarts: never block the session

    def mouse_move(self, nx, ny):
        self._input("mouse_move", nx, ny)

    def mouse_button(self, button, down, nx=None, ny=None):
        self._input("mouse_button", button, down, nx, ny)

    def scroll(self, dx, dy):
        self._input("scroll", dx, dy)

    def key(self, down, key=None, char=None):
        self._input("key", down, key, char)

    def release_all(self):
        self._input("release_all")


# =========================================================================
# Windows plumbing (ctypes). Everything below only runs on Windows.
# =========================================================================
def pipe_name(session_id: int) -> str:
    return f"{PIPE_PREFIX}{int(session_id)}"


def available() -> bool:
    """Lock screen support is possible: installed Windows copy."""
    if not IS_WINDOWS or not getattr(sys, "frozen", False):
        return False
    return (Path(sys.executable).resolve().parent / "unins000.exe").exists()


if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _u32 = ctypes.WinDLL("user32", use_last_error=True)

    INVALID_HANDLE = wintypes.HANDLE(-1).value
    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    OPEN_EXISTING = 3
    SECURITY_SQOS_PRESENT, SECURITY_IDENTIFICATION = 0x00100000, 0x00010000
    PIPE_ACCESS_DUPLEX, FILE_FLAG_FIRST_PIPE_INSTANCE = 0x3, 0x00080000
    PIPE_TYPE_BYTE, PIPE_READMODE_BYTE, PIPE_WAIT, PIPE_REJECT_REMOTE_CLIENTS = 0, 0, 0, 0x8
    ERROR_PIPE_CONNECTED, ERROR_MORE_DATA = 535, 234
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    MAXIMUM_ALLOWED = 0x02000000
    WAIT_TIMEOUT = 0x102
    CREATE_NO_WINDOW, CREATE_UNICODE_ENVIRONMENT = 0x08000000, 0x00000400
    # read/write the pipe, but never FILE_CREATE_PIPE_INSTANCE (0x4): the user
    # cannot create a fake helper pipe next to ours
    CLIENT_RIGHTS = 0x00120183
    # what the client asks for: read + write data, nothing else (GENERIC_WRITE
    # would include FILE_CREATE_PIPE_INSTANCE, which the pipe never grants)
    CLIENT_ACCESS = 0x00100083  # FILE_READ_DATA | FILE_WRITE_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID), ("bInheritHandle", wintypes.BOOL)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Luid", LUID), ("Attributes", wintypes.DWORD)]

    def _sig(dll, name, restype, argtypes):
        fn = getattr(dll, name)
        fn.restype, fn.argtypes = restype, argtypes
        return fn

    CreateNamedPipeW = _sig(_k32, "CreateNamedPipeW", wintypes.HANDLE, [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES)])
    ConnectNamedPipe = _sig(_k32, "ConnectNamedPipe", wintypes.BOOL, [wintypes.HANDLE, wintypes.LPVOID])
    DisconnectNamedPipe = _sig(_k32, "DisconnectNamedPipe", wintypes.BOOL, [wintypes.HANDLE])
    CreateFileW = _sig(_k32, "CreateFileW", wintypes.HANDLE, [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
        wintypes.HANDLE])
    ReadFile = _sig(_k32, "ReadFile", wintypes.BOOL, [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID])
    WriteFile = _sig(_k32, "WriteFile", wintypes.BOOL, [
        wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID])
    CloseHandle = _sig(_k32, "CloseHandle", wintypes.BOOL, [wintypes.HANDLE])
    GetNamedPipeClientProcessId = _sig(_k32, "GetNamedPipeClientProcessId", wintypes.BOOL,
                                       [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)])
    GetNamedPipeServerProcessId = _sig(_k32, "GetNamedPipeServerProcessId", wintypes.BOOL,
                                       [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)])
    OpenProcess = _sig(_k32, "OpenProcess", wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD])
    QueryFullProcessImageNameW = _sig(_k32, "QueryFullProcessImageNameW", wintypes.BOOL, [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)])
    ProcessIdToSessionId = _sig(_k32, "ProcessIdToSessionId", wintypes.BOOL,
                                [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)])
    GetCurrentProcessId = _sig(_k32, "GetCurrentProcessId", wintypes.DWORD, [])
    GetCurrentProcess = _sig(_k32, "GetCurrentProcess", wintypes.HANDLE, [])
    WTSGetActiveConsoleSessionId = _sig(_k32, "WTSGetActiveConsoleSessionId", wintypes.DWORD, [])
    WaitForSingleObject = _sig(_k32, "WaitForSingleObject", wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD])
    TerminateProcess = _sig(_k32, "TerminateProcess", wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT])
    LocalFree = _sig(_k32, "LocalFree", wintypes.HLOCAL, [wintypes.HLOCAL])

    ConvertStringSecurityDescriptorToSecurityDescriptorW = _sig(
        _adv, "ConvertStringSecurityDescriptorToSecurityDescriptorW", wintypes.BOOL,
        [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.ULONG)])
    OpenProcessToken = _sig(_adv, "OpenProcessToken", wintypes.BOOL,
                            [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)])
    DuplicateTokenEx = _sig(_adv, "DuplicateTokenEx", wintypes.BOOL, [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, ctypes.c_int, ctypes.c_int, ctypes.POINTER(wintypes.HANDLE)])
    SetTokenInformation = _sig(_adv, "SetTokenInformation", wintypes.BOOL,
                               [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD])
    GetTokenInformation = _sig(_adv, "GetTokenInformation", wintypes.BOOL, [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)])
    ConvertSidToStringSidW = _sig(_adv, "ConvertSidToStringSidW", wintypes.BOOL,
                                  [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)])
    CreateProcessAsUserW = _sig(_adv, "CreateProcessAsUserW", wintypes.BOOL, [
        wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.LPVOID, wintypes.LPVOID, wintypes.BOOL,
        wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION)])

    LookupPrivilegeValueW = _sig(_adv, "LookupPrivilegeValueW", wintypes.BOOL,
                                 [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)])
    AdjustTokenPrivileges = _sig(_adv, "AdjustTokenPrivileges", wintypes.BOOL, [
        wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES), wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID])

    OpenInputDesktop = _sig(_u32, "OpenInputDesktop", wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD])
    SetThreadDesktop = _sig(_u32, "SetThreadDesktop", wintypes.BOOL, [wintypes.HANDLE])
    CloseDesktop = _sig(_u32, "CloseDesktop", wintypes.BOOL, [wintypes.HANDLE])
    GetUserObjectInformationW = _sig(_u32, "GetUserObjectInformationW", wintypes.BOOL, [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)])

    try:
        _wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        WTSQueryUserToken = _sig(_wts, "WTSQueryUserToken", wintypes.BOOL,
                                 [wintypes.ULONG, ctypes.POINTER(wintypes.HANDLE)])
    except OSError:  # pragma: no cover
        WTSQueryUserToken = None

    def _err(what: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(code, f"{what} : {ctypes.FormatError(code).strip()}")

    class PipeStream:
        """File-like access to a pipe handle (read / write / close)."""

        def __init__(self, handle):
            self.handle = handle

        @classmethod
        def connect(cls, name: str) -> "PipeStream":
            h = CreateFileW(name, CLIENT_ACCESS, 0, None, OPEN_EXISTING,
                            SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, None)
            if h in (None, INVALID_HANDLE):
                raise _err("connexion à l'assistant")
            stream = cls(h)
            try:
                pid = wintypes.ULONG()
                if not GetNamedPipeServerProcessId(h, ctypes.byref(pid)):
                    raise _err("assistant")
                if not same_program(pid.value):
                    raise SecureDeskError("le canal n'appartient pas à BrickMote")
            except Exception:
                stream.close()
                raise
            return stream

        def read(self, n: int) -> bytes:
            buf = ctypes.create_string_buffer(min(n, 4 * 1024 * 1024))
            got = wintypes.DWORD()
            ok = ReadFile(self.handle, buf, len(buf), ctypes.byref(got), None)
            if not ok and ctypes.get_last_error() != ERROR_MORE_DATA:
                return b""
            return buf.raw[: got.value]

        def write(self, data: bytes) -> None:
            view = memoryview(data)
            while view:
                done = wintypes.DWORD()
                chunk = bytes(view[: 4 * 1024 * 1024])
                if not WriteFile(self.handle, chunk, len(chunk), ctypes.byref(done), None):
                    raise _err("écriture")
                view = view[done.value:]

        def close(self) -> None:
            if self.handle:
                CloseHandle(self.handle)
                self.handle = None

    def process_image(pid: int) -> str:
        h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if not QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return ""
            return buf.value
        finally:
            CloseHandle(h)

    def same_program(pid: int) -> bool:
        """Is ``pid`` this very program (same executable file)?"""
        image = process_image(pid)
        if not image:
            return False
        try:
            return os.path.normcase(os.path.realpath(image)) == os.path.normcase(os.path.realpath(sys.executable))
        except OSError:
            return False

    def session_of(pid: int) -> Optional[int]:
        sid = wintypes.DWORD()
        return sid.value if ProcessIdToSessionId(pid, ctypes.byref(sid)) else None

    def current_session_id() -> int:
        s = session_of(GetCurrentProcessId())
        if s is None:
            raise _err("session")
        return s

    def follow_input_desktop() -> str:
        """Attach this thread to the desktop receiving input; return its name."""
        desk = OpenInputDesktop(0, False, MAXIMUM_ALLOWED)
        if not desk:
            raise _err("bureau actif")
        name = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        GetUserObjectInformationW(desk, 2, name, ctypes.sizeof(name), ctypes.byref(needed))  # UOI_NAME
        previous = getattr(follow_input_desktop, "_current", None)
        if previous is not None and previous[1] == name.value:
            CloseDesktop(desk)
            return name.value
        if not SetThreadDesktop(desk):
            CloseDesktop(desk)
            raise _err("changement de bureau")
        if previous is not None:
            CloseDesktop(previous[0])
        follow_input_desktop._current = (desk, name.value)
        return name.value

    def uac_prompt_shown() -> bool:
        try:
            import psutil
        except ImportError:
            return True  # cannot tell: be safe
        for proc in psutil.process_iter(["name"]):
            try:
                if (proc.info["name"] or "").lower() == "consent.exe":
                    return True
            except psutil.Error:
                continue
        return False

    def _security_attributes(sddl: str):
        sd = wintypes.LPVOID()
        if not ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None):
            raise _err("droits du canal")
        sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), sd, False)
        return sa, sd

    def serve_pipe(name: str, user_sid: str, agent: DesktopAgent, session_id: int,
                   stop: Optional[threading.Event] = None, check_client: Callable[[int], bool] = None) -> None:
        """Helper loop: one client at a time, forever (or until ``stop``)."""
        sddl = f"D:P(A;;GA;;;SY)(A;;0x{CLIENT_RIGHTS:x};;;{user_sid})"
        check_client = check_client or same_program
        first = True
        while stop is None or not stop.is_set():
            sa, sd = _security_attributes(sddl)
            try:
                flags = PIPE_ACCESS_DUPLEX | (FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0)
                h = CreateNamedPipeW(name, flags, PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                                     1, 1 << 20, 1 << 16, 0, ctypes.byref(sa))
            finally:
                LocalFree(sd)
            if h in (None, INVALID_HANDLE):
                raise _err("création du canal")
            first = False
            stream = PipeStream(h)
            try:
                if not ConnectNamedPipe(h, None) and ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
                    continue
                pid = wintypes.ULONG()
                if not GetNamedPipeClientProcessId(h, ctypes.byref(pid)):
                    continue
                if session_of(pid.value) != session_id or not check_client(pid.value):
                    log.warning("client refusé (pid %s)", pid.value)
                    continue
                serve(stream, agent)
            finally:
                DisconnectNamedPipe(h)
                stream.close()

    def token_user_sid(token) -> str:
        size = wintypes.DWORD()
        GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        buf = ctypes.create_string_buffer(size.value)
        if not GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
            raise _err("utilisateur de la session")
        sid = SID_AND_ATTRIBUTES.from_buffer(buf).Sid
        text = wintypes.LPWSTR()
        if not ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise _err("identifiant de l'utilisateur")
        try:
            return text.value
        finally:
            LocalFree(ctypes.cast(text, ctypes.c_void_p))

    def console_user(session_id: int) -> Optional[str]:
        """SID of the user logged on in ``session_id`` (None: nobody)."""
        if WTSQueryUserToken is None:
            return None
        token = wintypes.HANDLE()
        if not WTSQueryUserToken(session_id, ctypes.byref(token)):
            return None
        try:
            return token_user_sid(token)
        finally:
            CloseHandle(token)

    def enable_privileges(*names: str) -> None:
        """SYSTEM holds these privileges; make sure they are switched on."""
        token = wintypes.HANDLE()
        if not OpenProcessToken(GetCurrentProcess(), 0x0020 | 0x0008, ctypes.byref(token)):  # ADJUST | QUERY
            raise _err("jeton")
        try:
            for name in names:
                tp = TOKEN_PRIVILEGES()
                tp.PrivilegeCount = 1
                tp.Attributes = 0x2  # SE_PRIVILEGE_ENABLED
                if LookupPrivilegeValueW(None, name, ctypes.byref(tp.Luid)):
                    AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
        finally:
            CloseHandle(token)

    def launch_helper(session_id: int, user_sid: str):
        """Start ``BrickMote.exe --desktop-helper`` as SYSTEM in the user's session."""
        own = wintypes.HANDLE()
        if not OpenProcessToken(GetCurrentProcess(), MAXIMUM_ALLOWED, ctypes.byref(own)):
            raise _err("jeton SYSTEM")
        token = wintypes.HANDLE()
        try:
            if not DuplicateTokenEx(own, MAXIMUM_ALLOWED, None, 2, 1, ctypes.byref(token)):  # impersonation, primary
                raise _err("copie du jeton")
        finally:
            CloseHandle(own)
        try:
            sid = wintypes.DWORD(session_id)
            if not SetTokenInformation(token, 12, ctypes.byref(sid), ctypes.sizeof(sid)):  # TokenSessionId
                raise _err("session du jeton")
            exe = str(Path(sys.executable).resolve())
            cmd = ctypes.create_unicode_buffer(f'"{exe}" --desktop-helper {user_sid}')
            si = STARTUPINFOW()
            si.cb = ctypes.sizeof(si)
            si.lpDesktop = "winsta0\\default"
            pi = PROCESS_INFORMATION()
            if not CreateProcessAsUserW(token, exe, cmd, None, None, False, CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                                        None, str(Path(exe).parent), ctypes.byref(si), ctypes.byref(pi)):
                raise _err("lancement de l'assistant")
            CloseHandle(pi.hThread)
            return pi.hProcess
        finally:
            CloseHandle(token)

    def process_alive(handle) -> bool:
        return bool(handle) and WaitForSingleObject(handle, 0) == WAIT_TIMEOUT

    def stop_process(handle) -> None:
        if handle:
            TerminateProcess(handle, 0)
            CloseHandle(handle)

else:  # pragma: no cover - only Windows has a secure desktop
    PipeStream = None

    def current_session_id() -> int:
        raise SecureDeskError("Windows uniquement")

    def follow_input_desktop() -> str:
        return "Default"

    def uac_prompt_shown() -> bool:
        return False


# ----------------------------------------------------------- entry points
def _log_to(path: Path) -> None:
    logging.basicConfig(filename=str(path), level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def run_helper(user_sid: str) -> int:
    """``--desktop-helper SID``: SYSTEM, in the user's session."""
    if not IS_WINDOWS:
        return 1
    import re

    if not re.fullmatch(r"S-1-5-21(-\d+){3,5}", user_sid or ""):
        return 2  # only a real user account, never SYSTEM or a group
    session = current_session_id()
    agent = DesktopAgent()
    try:
        serve_pipe(pipe_name(session), user_sid, agent, session)
    except Exception:  # noqa: BLE001 - the service starts us again
        log.exception("desktop helper stopped")
        return 1
    return 0


def run_desktop_service() -> int:
    """``--desktop-service``: SYSTEM scheduled task started with Windows. Keeps
    one helper in the session of whoever is logged on at the console."""
    if not available():
        return 0
    app_dir = Path(sys.executable).resolve().parent
    updates = app_dir / "updates"  # inside Program Files: only admins can write here
    updates.mkdir(exist_ok=True)
    _log_to(updates / "desktop.log")
    try:
        enable_privileges("SeTcbPrivilege", "SeAssignPrimaryTokenPrivilege", "SeIncreaseQuotaPrivilege")
    except OSError as exc:
        log.warning("privilèges : %s", exc)
    helper, helper_session, helper_user, failures = None, None, None, 0
    while True:
        try:
            session = WTSGetActiveConsoleSessionId()
            user = console_user(session) if session != 0xFFFFFFFF else None
            if helper is not None and (session != helper_session or user != helper_user or not process_alive(helper)):
                stop_process(helper)
                helper = None
            if helper is None and user is not None:
                helper = launch_helper(session, user)
                helper_session, helper_user = session, user
                log.info("assistant lancé dans la session %s", session)
            failures = 0
        except Exception as exc:  # noqa: BLE001
            failures += 1
            if failures in (1, 10, 100):
                log.warning("assistant : %s", exc)
            helper = None
        time.sleep(2 if failures < 10 else 30)
