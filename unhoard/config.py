"""Configuration for unhoard.

Precedence: environment variables > config file (~/.config/unhoard/config.toml) > defaults.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar, overload

CONFIG_DIR = Path(os.environ.get("UNHOARD_HOME", Path.home() / ".config" / "unhoard"))
CONFIG_PATH = CONFIG_DIR / "config.toml"
DEFAULT_STATE_DIR = Path(os.environ.get("UNHOARD_DATA", Path.home() / ".local" / "share" / "unhoard"))
DEFAULT_OUTPUT_DIR = Path.home() / "unhoard-digests"

_T = TypeVar("_T")


@dataclass
class Config:
    raindrop_token: str = ""
    anthropic_api_key: str = ""
    collection_id: int = 0            # 0 = Unsorted (Raindrop's default inbox), -1 = All
    state_db_path: Path = field(default_factory=lambda: DEFAULT_STATE_DIR / "state.db")
    output_dir: Path = field(default_factory=lambda: DEFAULT_OUTPUT_DIR)
    aging_days: int = 7               # items newer than this = "New"
    stale_days: int = 30              # items older than this = "Stale" (get AI summaries)
    max_new: int = 6
    max_aging: int = 6
    max_stale: int = 8
    model: str = "claude-sonnet-5"
    max_tokens_summary: int = 300
    context: str = ""                 # free-text notes on your projects/interests, given to the
                                       # summarizer so Action recommendations account for them
    unhoarded_tag: str = "unhoarded"  # tag applied to a Raindrop item on `mark --unhoarded`
    unhoarded_collection_id: Optional[int] = None  # if set, also move the item to this collection

    @property
    def anthropic_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


def _load_toml_file() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    return {}


def load_config() -> Config:
    file_cfg = _load_toml_file()
    cfg = Config()

    @overload
    def pick(key: str, env_name: str) -> str: ...
    @overload
    def pick(key: str, env_name: str, cast: Callable[[Any], _T]) -> _T: ...

    def pick(key: str, env_name: str, cast: Callable[[Any], Any] = str) -> Any:
        env_val = os.environ.get(env_name)
        if env_val is not None:
            return cast(env_val)
        if key in file_cfg:
            return cast(file_cfg[key])
        return getattr(cfg, key)

    cfg.raindrop_token = pick("raindrop_token", "RAINDROP_TOKEN")
    cfg.anthropic_api_key = pick("anthropic_api_key", "ANTHROPIC_API_KEY")
    cfg.collection_id = pick("collection_id", "UNHOARD_COLLECTION_ID", int)
    cfg.aging_days = pick("aging_days", "UNHOARD_AGING_DAYS", int)
    cfg.stale_days = pick("stale_days", "UNHOARD_STALE_DAYS", int)
    cfg.max_new = pick("max_new", "UNHOARD_MAX_NEW", int)
    cfg.max_aging = pick("max_aging", "UNHOARD_MAX_AGING", int)
    cfg.max_stale = pick("max_stale", "UNHOARD_MAX_STALE", int)
    cfg.model = pick("model", "UNHOARD_MODEL")
    cfg.context = pick("context", "UNHOARD_CONTEXT")
    cfg.unhoarded_tag = pick("unhoarded_tag", "UNHOARD_UNHOARDED_TAG")
    cfg.unhoarded_collection_id = pick(
        "unhoarded_collection_id", "UNHOARD_UNHOARDED_COLLECTION_ID", int
    )

    state_db = file_cfg.get("state_db_path")
    if state_db:
        cfg.state_db_path = Path(state_db).expanduser()
    output_dir = os.environ.get("UNHOARD_OUTPUT_DIR") or file_cfg.get("output_dir")
    if output_dir:
        cfg.output_dir = Path(output_dir).expanduser()

    cfg.state_db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def write_default_config(force: bool = False) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists() and not force:
        return CONFIG_PATH
    CONFIG_PATH.write_text(
        '# unhoard config\n'
        '# Secrets (raindrop_token, anthropic_api_key) are better left as env vars,\n'
        '# but you can put them here if you prefer a single file.\n\n'
        '# collection_id = 0    # 0 = Unsorted (default inbox), -1 = All raindrops\n'
        '# aging_days = 7       # newer than this -> "New" bucket, metadata only\n'
        '# stale_days = 30      # older than this -> "Stale" bucket, gets AI-summarized\n'
        '# max_new = 6\n'
        '# max_aging = 6\n'
        '# max_stale = 8\n'
        '# model = "claude-sonnet-5"\n'
        '# output_dir = "~/unhoard-digests"\n\n'
        '# context = """\n'
        '#   I collect and restore old-web / early-internet style sites: archived\n'
        '#   tutorials, GeoCities-era design patterns, old CSS tricks. These often read\n'
        '#   as "outdated" but are reference material I actively reuse -- don\'t\n'
        '#   recommend Delete for nostalgic/archival web content like this.\n'
        '# """\n\n'
        '# unhoarded_tag = "unhoarded"       # tag applied on `mark <key> --unhoarded` (Raindrop)\n'
        '# unhoarded_collection_id = 12345   # optional -- also move it to this collection\n'
    )
    return CONFIG_PATH

