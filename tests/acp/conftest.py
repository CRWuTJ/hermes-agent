"""ACP test guards.

The ACP adapter is optional. Keep the base test suite collectible when the
``hermes-agent[acp]`` extra is not installed.
"""

import importlib.util
import re


def pytest_ignore_collect(collection_path, config):
    if importlib.util.find_spec("acp") is not None:
        return False

    path = getattr(collection_path, "path", collection_path)
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return False
    return re.search(r"(?m)^(?:import acp(?:\s|$)|from acp(?:\.|\s+import\b))", text) is not None
