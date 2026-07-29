"""Wires configured sources (from config.toml's [[sources]] tables, or ad-hoc
--source flags) into actual adapter instances."""
from __future__ import annotations

import tomllib
from typing import Any, Optional

from .adapters import REGISTRY
from .adapters.base import Adapter
from .config import CONFIG_PATH, Config


def load_source_configs() -> list[dict[str, Any]]:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            data = tomllib.load(f)
        return data.get("sources", []) or []
    return []


def build_adapters(cfg: Config, cli_sources: Optional[list[str]] = None) -> list[tuple[str, Adapter]]:
    """Returns [(label, adapter_instance), ...].

    cli_sources, if given, overrides config.toml entirely with simple 'type' or
    'type:arg' strings, e.g. ["chrome", "safari", "json:/path/pocket-export.json"].
    """
    if cli_sources:
        return [_build_from_spec(spec, cfg) for spec in cli_sources]

    configured = load_source_configs()
    if configured:
        adapters: list[tuple[str, Adapter]] = []
        for entry in configured:
            entry = dict(entry)
            stype = entry.pop("type", None)
            label = entry.pop("name", stype)
            cls = REGISTRY.get(stype)
            if not cls:
                raise ValueError(f"Unknown source type '{stype}' in config.toml [[sources]]")
            if stype == "raindrop":
                entry.setdefault("token", cfg.raindrop_token)
                entry.setdefault("unhoarded_tag", cfg.unhoarded_tag)
                entry.setdefault("unhoarded_collection_id", cfg.unhoarded_collection_id)
            adapters.append((label, cls(**entry)))
        return adapters

    # No [[sources]] table configured at all -- fall back to raindrop-only,
    # which is the simplest possible setup (just a token, no config file needed).
    if cfg.raindrop_token:
        return [(
            "raindrop",
            REGISTRY["raindrop"](
                cfg.raindrop_token, cfg.collection_id,
                unhoarded_tag=cfg.unhoarded_tag, unhoarded_collection_id=cfg.unhoarded_collection_id,
            ),
        )]
    return []


def find_adapter_for_source(cfg: Config, source: str) -> Optional[Adapter]:
    """Best-effort lookup of a configured adapter instance matching an item's
    stored `source` (e.g. 'raindrop', or 'json:pocket'). Used for optional
    write-back on `mark --unhoarded` -- returns None if nothing matches or
    adapters can't be built at all; callers should treat that as 'no write-back
    available', not an error."""
    try:
        adapters = build_adapters(cfg)
    except ValueError:
        return None
    for _, adapter in adapters:
        if getattr(adapter, "name", None) == source or getattr(adapter, "source_label", None) == source:
            return adapter
    return None


def _build_from_spec(spec: str, cfg: Config) -> tuple[str, Adapter]:
    stype, _, arg = spec.partition(":")
    cls = REGISTRY.get(stype)
    if not cls:
        raise ValueError(f"Unknown source type '{stype}' (known: {', '.join(REGISTRY)})")
    if stype == "raindrop":
        return ("raindrop", cls(
            cfg.raindrop_token, int(arg) if arg else cfg.collection_id,
            unhoarded_tag=cfg.unhoarded_tag, unhoarded_collection_id=cfg.unhoarded_collection_id,
        ))
    if stype == "chrome":
        return ("chrome", cls(profile_dir=arg or None))
    if stype == "safari":
        return ("safari", cls(plist_path=arg or None))
    if stype == "json":
        if not arg:
            raise ValueError("json source needs a path or URL: --source json:/path/to/file.json")
        return (f"json:{arg}", cls(source_path=arg))
    raise ValueError(f"Don't know how to build adapter for '{spec}'")

