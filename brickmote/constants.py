"""Protocol-wide constants.

Ports are the same as the ones used by AnyDesk so that BrickMote works through
the same firewall rules:

* TCP 6568  - relay / rendezvous server (with fallback on TCP 443 then TCP 80)
* TCP 7070  - direct peer-to-peer connections (LAN or port-forwarded)
* UDP 50001 - LAN discovery broadcasts
"""

PROTOCOL_NAME = "BrickMote/1"

RELAY_PORTS = (6568, 443, 80)
DIRECT_PORT = 7070
DISCOVERY_PORT = 50001

# Frame size limits (bytes). Anything above is treated as an attack.
MAX_HANDSHAKE_FRAME = 64 * 1024
MAX_CONTROL_FRAME = 64 * 1024
MAX_SESSION_FRAME = 16 * 1024 * 1024

# Timeouts (seconds)
HANDSHAKE_STEP_TIMEOUT = 20.0
INTERACTIVE_ACCEPT_TIMEOUT = 45.0
CREDENTIAL_PROMPT_TIMEOUT = 120.0
SESSION_KEEPALIVE = 10.0
SESSION_IDLE_TIMEOUT = 45.0

# File transfers
FILE_CHUNK_SIZE = 64 * 1024
MAX_FILE_SIZE = 8 * 1024 * 1024 * 1024  # 8 GiB

# Permissions that a host can grant to a viewer.
PERMISSION_KEYS = ("control", "clipboard", "files")
