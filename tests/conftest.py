import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BRICKMOTE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    os.makedirs(tmp_path / "userhome", exist_ok=True)


async def stream_pair():
    """Return ((reader_a, writer_a), (reader_b, writer_b)) connected over TCP."""
    accepted = asyncio.get_running_loop().create_future()

    async def on_conn(r, w):
        accepted.set_result((r, w))

    server = await asyncio.start_server(on_conn, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    a = await asyncio.open_connection("127.0.0.1", port)
    b = await accepted
    server.close()
    return a, b
