import json
import os
import asyncio
from collections import defaultdict

_locks = defaultdict(asyncio.Lock)


def _ensure_parent(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)


async def read_json(path: str, default):
    async with _locks[path]:
        if not os.path.exists(path):
            _ensure_parent(path)
            with open(path, "w") as f:
                json.dump(default, f, indent=2)
            return json.loads(json.dumps(default))  # deep-ish copy
        with open(path, "r") as f:
            return json.load(f)


async def write_json(path: str, data):
    async with _locks[path]:
        _ensure_parent(path)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
