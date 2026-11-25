"""Safe handling of received file names (no path traversal, no overwrite)."""

from __future__ import annotations

import os
import re
from pathlib import Path

_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)
}
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


def safe_filename(name: object, max_len: int = 180) -> str:
    """Reduce an untrusted file name to a harmless base name."""
    if not isinstance(name, str):
        name = ""
    # keep only the last path component, whatever the separator
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable())
    name = _FORBIDDEN.sub("_", name).strip().strip(".")
    if not name:
        name = "fichier"
    stem, dot, ext = name.partition(".")
    if stem.upper() in _WINDOWS_RESERVED:
        name = "_" + name
    if len(name) > max_len:
        base, ext = os.path.splitext(name)
        ext = ext[:20]
        name = base[: max_len - len(ext)] + ext
    return name


def open_unique(directory: Path, name: str) -> tuple[Path, int]:
    """Create a new file in ``directory`` without ever overwriting.

    Returns the path and an OS file descriptor opened for writing.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    base, ext = os.path.splitext(name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for i in range(0, 1000):
        candidate = directory / (name if i == 0 else f"{base} ({i}){ext}")
        try:
            fd = os.open(candidate, flags, 0o600)
        except FileExistsError:
            continue
        # Final sanity check: the file must really be inside ``directory``.
        if candidate.resolve().parent != directory.resolve():
            os.close(fd)
            os.unlink(candidate)
            raise ValueError("unsafe destination")
        return candidate, fd
    raise FileExistsError("too many files with the same name")
