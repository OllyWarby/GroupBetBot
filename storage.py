"""Small helper for reading/writing the JSON data files safely.

Each file gets its own asyncio.Lock so concurrent commands/poller ticks
don't interleave writes and corrupt the JSON. This is a single-process
bot, so a simple in-memory lock per path is enough — no file locking
across processes needed.
"""

import json
import os
import asyncio
from collections import defaultdict

_locks = defaultdict(asyncio.Lock)


def _ensure_parent(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)


async def read_json(path: str, default):
    """Read a JSON file, returning `default` (and creating the file) if missing."""
    async with _locks[path]:
        if not os.path.exists(path):
            _ensure_parent(path)
            with open(path, "w") as f:
                json.dump(default, f, indent=2)
            return json.loads(json.dumps(default))  # deep-ish copy
        with open(path, "r") as f:
            return json.load(f)


async def write_json(path: str, data):
    """Write a JSON file atomically (write to temp file, then rename)."""
    async with _locks[path]:
        _ensure_parent(path)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
