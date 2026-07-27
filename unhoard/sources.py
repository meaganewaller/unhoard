"""Wires configured sources (from config.toml's [[sources]] tables, or ad-hoc
--source flags) into actual adapter instances."""
from __future__ import annotations

import tomllib
from typing import Optional

from .adapters import REGISTRY
from .config import CONFIG_PATH, Config


def load_source_configs() -> list[dict]:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            data = tomllib.load(f)
        return data.get("sources", []) or []
    return []


def build_adapters(cfg: Config, cli_sources: Optional[list[str]] = None) -> list[tuple[str, object]]:
    """Returns [(label, adapter_instance), ...].

    cli_sources, if given, overrides config.toml entirely with simple 'type' or
    'type:arg' strings, e.g. ["chrome", "safari", "json:/path/pocket-export.json"].
    """
    if cli_sources:
        return [_build_from_spec(spec, cfg) for spec in cli_sources]

    configured = load_source_configs()
    if configured:
        adapters = []
        for entry in configured:
            entry = dict(entry)
            stype = entry.pop("type", None)
            label = entry.pop("name", stype)
            cls = REGISTRY.get(stype)
            if not cls:
                raise ValueError(f"Unknown source type '{stype}' in config.toml [[sources]]")
            if stype == "raindrop" and "token" not in entry:
                entry["token"] = cfg.raindrop_token
            adapters.append((label, cls(**entry)))
        return adapters

    # No [[sources]] table configured at all -- fall back to raindrop-only,
    # which is the simplest possible setup (just a token, no config file needed).
    if cfg.raindrop_token:
        return [("raindrop", REGISTRY["raindrop"](cfg.raindrop_token, cfg.collection_id))]
    return []


def _build_from_spec(spec: str, cfg: Config) -> tuple[str, object]:
    stype, _, arg = spec.partition(":")
    cls = REGISTRY.get(stype)
    if not cls:
        raise ValueError(f"Unknown source type '{stype}' (known: {', '.join(REGISTRY)})")
    if stype == "raindrop":
        return ("raindrop", cls(cfg.raindrop_token, int(arg) if arg else cfg.collection_id))
    if stype == "chrome":
        return ("chrome", cls(profile_dir=arg or None))
    if stype == "safari":
        return ("safari", cls(plist_path=arg or None))
    if stype == "json":
        if not arg:
            raise ValueError("json source needs a path or URL: --source json:/path/to/file.json")
        return (f"json:{arg}", cls(source_path=arg))
    raise ValueError(f"Don't know how to build adapter for '{spec}'")

