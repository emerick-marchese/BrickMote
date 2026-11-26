"""Live pointers of everyone connected to this computer.

Each viewer gets a color. Its pointer position (where its mouse is over the
remote screen, whether it controls the computer or only watches) is shared
with the other viewers, along with the real cursor of the computer, about 60
times per second and only when something moved. The local user can also see
the viewers' pointers (``on_change``, used by the GUI overlay).

Positions travel in absolute desktop pixels inside the host and are converted
to each viewer's own monitor (0..COORD_MAX), so viewers watching different
monitors still see each other when their pointers are on the same screen.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

from .inputs import COORD_MAX

log = logging.getLogger(__name__)

PALETTE_SIZE = 8
TICK = 1 / 60
DRIVE_HOLD = 0.6  # s: after a viewer moved the real cursor, the cursor is "theirs"
MAX_BUFFERED = 256 * 1024  # a viewer whose link is saturated skips pointer updates
NAME_MAX = 32


def clean_name(name: object) -> str:
    text = "".join(ch for ch in str(name or "") if ch.isprintable()).strip()
    return text[:NAME_MAX] or "Utilisateur"


def to_abs(mon: dict, nx: int, ny: int) -> tuple[int, int]:
    nx = min(max(nx, 0), COORD_MAX)
    ny = min(max(ny, 0), COORD_MAX)
    return (
        mon["left"] + round(nx * (mon["width"] - 1) / COORD_MAX),
        mon["top"] + round(ny * (mon["height"] - 1) / COORD_MAX),
    )


def to_norm(mon: dict, x: int, y: int) -> Optional[tuple[int, int]]:
    dx, dy = x - mon["left"], y - mon["top"]
    if not (0 <= dx < mon["width"] and 0 <= dy < mon["height"]):
        return None
    return (
        round(dx * COORD_MAX / max(1, mon["width"] - 1)),
        round(dy * COORD_MAX / max(1, mon["height"] - 1)),
    )


@dataclass
class Participant:
    color: int
    name: str
    pos: Optional[tuple[int, int]] = None  # absolute desktop pixels, None = not over the screen
    drove_at: float = 0.0  # last time this viewer moved the real cursor
    last_sent: object = None


class CursorHub:
    def __init__(
        self,
        reader_factory: Optional[Callable] = None,
        on_change: Optional[Callable[[list], None]] = None,
        local=None,
    ):
        self.local = local  # LocalActivity: the person at the computer has priority
        self._local_was_active = False
        self._reader_factory = reader_factory
        self._reader = None  # only touched from the hub's thread
        self.on_change = on_change
        self.members: dict = {}  # HostSession -> Participant
        self._task: Optional[asyncio.Task] = None
        self._exec: Optional[ThreadPoolExecutor] = None
        self.system: Optional[tuple[int, int]] = None
        self._last_local: object = None

    # ------------------------------------------------------------ members
    def join(self, session, name: object) -> int:
        used = {p.color for p in self.members.values()}
        color = next((c for c in range(PALETTE_SIZE) if c not in used), len(self.members) % PALETTE_SIZE)
        self.members[session] = Participant(color, clean_name(name))
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())
        return color

    def leave(self, session) -> None:
        if self.members.pop(session, None) is not None:
            self._notify_local()

    def point(self, session, monitor: Optional[dict], nx: object, ny: object, drove: bool = False) -> None:
        """A viewer's pointer moved (normalized to the monitor it watches)."""
        p = self.members.get(session)
        if p is None:
            return
        if monitor is None or not isinstance(nx, int) or not isinstance(ny, int) or isinstance(nx, bool):
            return
        p.pos = to_abs(monitor, nx, ny)
        if drove:
            p.drove_at = time.monotonic()

    def hide(self, session) -> None:
        p = self.members.get(session)
        if p is not None:
            p.pos = None

    # --------------------------------------------------------------- loop
    def _read_system(self) -> Optional[tuple[int, int]]:
        """Runs in the hub's thread."""
        if self._reader_factory is None:
            return None
        if self._reader is None:
            self._reader = self._reader_factory()
        read = getattr(self._reader, "position", None)
        return read() if read else None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        self._exec = ThreadPoolExecutor(1, thread_name_prefix="bm-pointers")
        try:
            while self.members:
                started = loop.time()
                try:
                    self.system = await loop.run_in_executor(self._exec, self._read_system)
                except Exception:  # noqa: BLE001 - no display, permissions...
                    self.system = None
                if self.local is not None:
                    self.local.observe(self.system)  # did the person at the computer move it?
                await self.broadcast()
                await asyncio.sleep(max(0.004, TICK - (loop.time() - started)))
        finally:
            self._exec.shutdown(wait=False)
            self._exec = None
            self._reader = None
            if self.local is not None:
                self.local.reset()
            self._notify_local()

    def driver(self) -> Optional[Participant]:
        """The viewer currently moving the real cursor, if any."""
        now = time.monotonic()
        active = [p for p in self.members.values() if p.pos is not None and now - p.drove_at < DRIVE_HOLD]
        return max(active, key=lambda p: p.drove_at, default=None)

    def view_for(self, session) -> Optional[dict]:
        monitor = session.current_monitor()
        if monitor is None:
            return None
        others = []
        for other, p in self.members.items():
            if other is session or p.pos is None:
                continue
            n = to_norm(monitor, *p.pos)
            if n is not None:
                others.append([p.color, n[0], n[1], p.name])
        system = None
        if self.system is not None and (self._local_was_active or self.driver() is None):
            # while a viewer drives, its colored pointer already shows where
            # the real cursor is: no second arrow on top of it
            n = to_norm(monitor, *self.system)
            system = list(n) if n else None
        msg = {"t": "cursors", "sys": system, "p": others}
        if self._local_was_active:
            msg["local"] = True  # viewers: your input waits, the person at the computer is using it
        return msg

    async def broadcast(self) -> None:
        local = bool(self.local is not None and self.local.active())
        if local and not self._local_was_active:
            for session in list(self.members):
                release = getattr(session, "release_remote_input", None)
                if release is not None:
                    release()
        self._local_was_active = local
        for session, me in list(self.members.items()):
            if session.closed:
                continue
            msg = self.view_for(session)
            if msg is None or msg == me.last_sent:
                continue
            if session.channel.buffered_bytes() > MAX_BUFFERED:
                continue  # never queue pointers behind a saturated link: the next tick has fresher ones
            me.last_sent = msg
            await session.send_json(msg)
        self._notify_local()

    def _notify_local(self) -> None:
        if self.on_change is None:
            return
        local = sorted((p.color, p.name, p.pos[0], p.pos[1]) for p in self.members.values() if p.pos is not None)
        if local != self._last_local:
            self._last_local = local
            try:
                self.on_change(local)
            except Exception:  # noqa: BLE001 - the GUI must never break the session
                log.exception("pointer overlay update failed")
