from pathlib import Path
from types import SimpleNamespace

import pytest

from unhoard.adapters.chrome import ChromeAdapter
from unhoard.adapters.generic_json import GenericJSONAdapter
from unhoard.adapters.raindrop import RaindropAdapter
from unhoard.adapters.safari import SafariAdapter
from unhoard.config import Config, load_config
from unhoard.sources import build_adapters, find_adapter_for_source


def test_build_from_spec_raindrop_with_explicit_collection(cfg: Config) -> None:
    cfg.raindrop_token = "tok"
    label, adapter = build_adapters(cfg, ["raindrop:99"])[0]
    assert label == "raindrop"
    assert isinstance(adapter, RaindropAdapter)
    assert adapter.collection_id == 99


def test_build_from_spec_raindrop_defaults_to_cfg_collection(cfg: Config) -> None:
    cfg.raindrop_token = "tok"
    cfg.collection_id = 5
    label, adapter = build_adapters(cfg, ["raindrop"])[0]
    assert isinstance(adapter, RaindropAdapter)
    assert adapter.collection_id == 5


def test_build_from_spec_chrome_and_safari_pass_through_arg(cfg: Config) -> None:
    (_, chrome), (_, safari) = build_adapters(cfg, ["chrome:/a/profile", "safari:/a/plist"])
    assert isinstance(chrome, ChromeAdapter)
    assert chrome.profile_dir == Path("/a/profile")
    assert isinstance(safari, SafariAdapter)
    assert safari.plist_path == Path("/a/plist")


def test_build_from_spec_json_requires_an_argument(cfg: Config) -> None:
    with pytest.raises(ValueError, match="needs a path or URL"):
        build_adapters(cfg, ["json"])


def test_build_from_spec_json_builds_generic_adapter(cfg: Config) -> None:
    label, adapter = build_adapters(cfg, ["json:/tmp/export.json"])[0]
    assert label == "json:/tmp/export.json"
    assert isinstance(adapter, GenericJSONAdapter)
    assert adapter.source_path == "/tmp/export.json"


def test_build_from_spec_unknown_type_raises(cfg: Config) -> None:
    with pytest.raises(ValueError, match="Unknown source type"):
        build_adapters(cfg, ["carrier-pigeon"])


def test_build_adapters_reads_sources_table_from_config_file(isolated_paths: SimpleNamespace) -> None:
    isolated_paths.config_dir.mkdir(parents=True)
    isolated_paths.config_path.write_text(
        "[[sources]]\n"
        'type = "chrome"\n\n'
        "[[sources]]\n"
        'type = "json"\n'
        'name = "pocket"\n'
        'source_path = "/tmp/pocket.json"\n'
    )
    cfg = load_config()

    adapters = build_adapters(cfg)

    assert [label for label, _ in adapters] == ["chrome", "pocket"]
    assert isinstance(adapters[0][1], ChromeAdapter)
    assert isinstance(adapters[1][1], GenericJSONAdapter)


def test_build_adapters_applies_raindrop_defaults_from_cfg(isolated_paths: SimpleNamespace) -> None:
    isolated_paths.config_dir.mkdir(parents=True)
    isolated_paths.config_path.write_text('[[sources]]\ntype = "raindrop"\ncollection_id = 3\n')
    cfg = load_config()
    cfg.raindrop_token = "tok-from-cfg"
    cfg.unhoarded_tag = "custom-tag"

    _, adapter = build_adapters(cfg)[0]

    assert isinstance(adapter, RaindropAdapter)
    assert adapter.collection_id == 3
    assert adapter.unhoarded_tag == "custom-tag"


def test_build_adapters_unknown_type_in_config_raises(isolated_paths: SimpleNamespace) -> None:
    isolated_paths.config_dir.mkdir(parents=True)
    isolated_paths.config_path.write_text('[[sources]]\ntype = "carrier-pigeon"\n')
    cfg = load_config()

    with pytest.raises(ValueError, match="Unknown source type"):
        build_adapters(cfg)


def test_build_adapters_falls_back_to_raindrop_only_when_unconfigured(cfg: Config) -> None:
    cfg.raindrop_token = "tok"
    cfg.collection_id = 0

    adapters = build_adapters(cfg)

    assert len(adapters) == 1
    label, adapter = adapters[0]
    assert label == "raindrop"
    assert isinstance(adapter, RaindropAdapter)


def test_build_adapters_returns_empty_when_nothing_configured(cfg: Config) -> None:
    assert build_adapters(cfg) == []


def test_find_adapter_for_source_matches_by_name(cfg: Config) -> None:
    cfg.raindrop_token = "tok"

    adapter = find_adapter_for_source(cfg, "raindrop")

    assert isinstance(adapter, RaindropAdapter)


def test_find_adapter_for_source_matches_by_source_label(isolated_paths: SimpleNamespace) -> None:
    isolated_paths.config_dir.mkdir(parents=True)
    isolated_paths.config_path.write_text(
        '[[sources]]\ntype = "json"\nname = "pocket"\nsource_path = "/tmp/pocket.json"\n'
    )
    cfg = load_config()

    adapter = find_adapter_for_source(cfg, "json:pocket")

    assert isinstance(adapter, GenericJSONAdapter)


def test_find_adapter_for_source_returns_none_when_unmatched(cfg: Config) -> None:
    assert find_adapter_for_source(cfg, "nonexistent") is None


def test_find_adapter_for_source_returns_none_on_misconfiguration(isolated_paths: SimpleNamespace) -> None:
    isolated_paths.config_dir.mkdir(parents=True)
    isolated_paths.config_path.write_text('[[sources]]\ntype = "carrier-pigeon"\n')
    cfg = load_config()

    assert find_adapter_for_source(cfg, "raindrop") is None
