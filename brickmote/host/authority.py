"""Host-side access policy: who may connect, how, and with which rights."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Awaitable, Callable, Optional

from ..config import AuditLog, Settings
from ..crypto.handshake import METHOD_INTERACTIVE, METHOD_PASSWORD, PeerInfo
from ..crypto.identity import Identity
from ..crypto.password import PasswordVerifier, TotpVerifier
from ..util import RateLimiter

# Brute-force protection for unattended access.
FAIL_WINDOW = 15 * 60
FAIL_PER_PEER = 5  # failures per viewer ID within the window -> lock this ID
LOCK_PER_PEER = 15 * 60
FAIL_GLOBAL = 20  # failures from all IDs within the window -> lock password access
LOCK_GLOBAL = 5 * 60

PromptFn = Callable[[PeerInfo, str], Awaitable[Optional[dict]]]


class HostPolicy:
    def __init__(
        self,
        identity: Identity,
        settings: Callable[[], Settings],
        audit: AuditLog,
        prompt: Optional[PromptFn] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.identity = identity
        self._settings = settings
        self.audit = audit
        self.prompt = prompt
        self._clock = clock
        self._failures: dict[str, deque] = defaultdict(deque)
        self._locked_until: dict[str, float] = {}
        self._global_failures: deque = deque()
        self._global_locked_until = 0.0
        self._pending_prompts = 0
        self._prompt_limiter = RateLimiter(limit=3, window=60)
        self._totp: Optional[TotpVerifier] = None
        self._totp_secret: Optional[str] = None

    # ------------------------------------------------------------ settings
    @property
    def name(self) -> str:
        return self._settings().device_name

    def offered_methods(self) -> list[str]:
        s = self._settings()
        methods = []
        if s.interactive_enabled and self.prompt is not None:
            methods.append(METHOD_INTERACTIVE)
        if s.unattended_enabled and s.password_verifier() is not None:
            methods.append(METHOD_PASSWORD)
        return methods

    def password_verifier(self) -> Optional[PasswordVerifier]:
        return self._settings().password_verifier()

    def totp_required(self) -> bool:
        return bool(self._settings().totp_secret)

    def check_totp(self, code: object) -> bool:
        secret = self._settings().totp_secret
        if not secret:
            return True
        if self._totp is None or self._totp_secret != secret:
            self._totp = TotpVerifier(secret)
            self._totp_secret = secret
        return self._totp.verify(code)

    def unattended_permissions(self) -> dict:
        return dict(self._settings().unattended_permissions)

    # ------------------------------------------------------------- checks
    def _is_locked(self, device_id: str) -> bool:
        now = self._clock()
        if now < self._global_locked_until:
            return True
        return now < self._locked_until.get(device_id, 0.0)

    async def precheck(self, peer: PeerInfo, method: str) -> Optional[str]:
        s = self._settings()
        if peer.device_id in s.blocked:
            self.audit.write("refused", peer=peer.device_id, ip=peer.address, reason="blocked")
            return "blocked"
        if s.acl_enabled and peer.device_id not in {e.get("id") for e in s.acl if isinstance(e, dict)}:
            self.audit.write("refused", peer=peer.device_id, ip=peer.address, reason="acl")
            return "acl"
        if method == METHOD_PASSWORD and self._is_locked(peer.device_id):
            self.audit.write("refused", peer=peer.device_id, ip=peer.address, reason="locked")
            return "locked"
        if method == METHOD_INTERACTIVE:
            if self._pending_prompts > 0:
                return "busy"
            if not self._prompt_limiter.allow(peer.device_id):
                return "rate_limited"
        return None

    async def request_interactive(self, peer: PeerInfo, sas: str) -> Optional[dict]:
        if self.prompt is None:
            return None
        self._pending_prompts += 1
        self.audit.write("request", peer=peer.device_id, name=peer.name, ip=peer.address)
        try:
            return await self.prompt(peer, sas)
        finally:
            self._pending_prompts -= 1

    def record_failure(self, peer: PeerInfo, reason: str) -> None:
        now = self._clock()
        q = self._failures[peer.device_id]
        q.append(now)
        while q and now - q[0] > FAIL_WINDOW:
            q.popleft()
        if len(q) >= FAIL_PER_PEER:
            self._locked_until[peer.device_id] = now + LOCK_PER_PEER
        g = self._global_failures
        g.append(now)
        while g and now - g[0] > FAIL_WINDOW:
            g.popleft()
        if len(g) >= FAIL_GLOBAL:
            self._global_locked_until = now + LOCK_GLOBAL
        self.audit.write("auth_failure", peer=peer.device_id, ip=peer.address, reason=reason)

    def record_success(self, peer: PeerInfo, method: str) -> None:
        self._failures.pop(peer.device_id, None)
        self.audit.write("accepted", peer=peer.device_id, name=peer.name, ip=peer.address, method=method)
