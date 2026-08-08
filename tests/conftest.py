"""Shared fixtures. unhoard's config module computes its directory constants
(CONFIG_DIR, CONFIG_PATH, DEFAULT_STATE_DIR, DEFAULT_OUTPUT_DIR) once at
import time from the environment, so setting UNHOARD_HOME/UNHOARD_DATA in a
test doesn't retroactively change them -- isolated_paths patches the module
attributes directly instead. sources.py also does `from .config import
CONFIG_PATH`, which binds its own copy of the name at import time -- patching
unhoard.config.CONFIG_PATH alone doesn't reach unhoard.sources.CONFIG_PATH,
so both must be patched."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from unhoard import config as config_module
from unhoard import sources as sources_module
from unhoard.schema import Item
from unhoard.state import StateStore

# Real-world env vars that must never leak into a test's Config, since a
# developer's own shell may have RAINDROP_TOKEN/ANTHROPIC_API_KEY set.
_ENV_VARS_TO_CLEAR = (
    "RAINDROP_TOKEN", "ANTHROPIC_API_KEY", "UNHOARD_COLLECTION_ID",
    "UNHOARD_AGING_DAYS", "UNHOARD_STALE_DAYS", "UNHOARD_MAX_NEW",
    "UNHOARD_MAX_AGING", "UNHOARD_MAX_STALE", "UNHOARD_MODEL", "UNHOARD_FAST_MODEL",
    "UNHOARD_CONTEXT", "UNHOARD_UNHOARDED_TAG",
    "UNHOARD_UNHOARDED_COLLECTION_ID", "UNHOARD_OUTPUT_DIR",
)


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Points unhoard's config/state/output directories at tmp_path and
    clears real-world env vars, so tests never touch ~/.config/unhoard,
    ~/.local/share/unhoard, ~/unhoard-digests, or a real API token."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "data"
    output_dir = tmp_path / "digests"
    config_path = config_dir / "config.toml"

    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config_module, "DEFAULT_STATE_DIR", state_dir)
    monkeypatch.setattr(config_module, "DEFAULT_OUTPUT_DIR", output_dir)
    monkeypatch.setattr(sources_module, "CONFIG_PATH", config_path)
    for env_var in _ENV_VARS_TO_CLEAR:
        monkeypatch.delenv(env_var, raising=False)

    return SimpleNamespace(
        config_dir=config_dir, state_dir=state_dir, output_dir=output_dir, config_path=config_path,
    )


@pytest.fixture
def cfg(isolated_paths: SimpleNamespace) -> config_module.Config:
    """A Config loaded fresh against isolated tmp paths."""
    return config_module.load_config()


@pytest.fixture
def state_store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.db")


@pytest.fixture
def make_item() -> Callable[..., Item]:
    """Factory for Item instances with sensible defaults, override via kwargs."""
    def _make(**overrides: Any) -> Item:
        defaults: dict[str, Any] = {
            "source": "test", "source_id": "1", "title": "Test Item", "url": "https://example.com/1",
        }
        defaults.update(overrides)
        return Item(**defaults)
    return _make
