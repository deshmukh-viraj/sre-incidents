"""
config/loader.py — P10 fintech config pack (TASK D).

Flag: CONFIG_PACK_ENABLED (default 0; MULTI_AGENT=1 enables all upgrades).
Reads src/config/fintech.yaml once per process; returns {} when disabled or
when the file/yaml is unavailable — coded defaults keep working either way.
"""

import os
from functools import lru_cache

_PACK_PATH = os.path.join(os.path.dirname(__file__), "fintech.yaml")


def _pack_enabled() -> bool:
    return os.getenv("MULTI_AGENT", "0") == "1" or \
        os.getenv("CONFIG_PACK_ENABLED", "0") == "1"


@lru_cache(maxsize=1)
def config_pack() -> dict:
    if not _pack_enabled():
        return {}
    try:
        import yaml
        with open(_PACK_PATH, "r", encoding="utf-8") as f:
            pack = yaml.safe_load(f) or {}
        print(f"[config-pack] loaded {_PACK_PATH} "
              f"(sections: {', '.join(pack.keys())})")
        return pack
    except Exception as e:
        print(f"[config-pack] unavailable ({e}); coded defaults stay active")
        return {}
