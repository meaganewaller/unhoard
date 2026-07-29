from types import SimpleNamespace

import pytest

from unhoard.config import Config, load_config, write_default_config


def test_load_config_defaults_and_creates_directories(cfg: Config, isolated_paths: SimpleNamespace) -> None:
    assert cfg.raindrop_token == ""
    assert cfg.aging_days == 7
    assert cfg.stale_days == 30
    assert cfg.state_db_path == isolated_paths.state_dir / "state.db"
    assert cfg.output_dir == isolated_paths.output_dir
    # load_config() side-effects: both directories must exist for later writes.
    assert cfg.state_db_path.parent.is_dir()
    assert cfg.output_dir.is_dir()


def test_anthropic_enabled_reflects_api_key() -> None:
    assert Config(anthropic_api_key="").anthropic_enabled is False
    assert Config(anthropic_api_key="sk-...").anthropic_enabled is True


def test_load_config_env_var_overrides_defaults(
    isolated_paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAINDROP_TOKEN", "env-token")
    monkeypatch.setenv("UNHOARD_AGING_DAYS", "3")
    monkeypatch.setenv("UNHOARD_MAX_NEW", "10")

    cfg = load_config()

    assert cfg.raindrop_token == "env-token"
    assert cfg.aging_days == 3
    assert cfg.max_new == 10


def test_load_config_reads_file_values(isolated_paths: SimpleNamespace) -> None:
    isolated_paths.config_dir.mkdir(parents=True)
    isolated_paths.config_path.write_text(
        'raindrop_token = "file-token"\n'
        "aging_days = 5\n"
        'model = "claude-opus-5"\n'
    )

    cfg = load_config()

    assert cfg.raindrop_token == "file-token"
    assert cfg.aging_days == 5
    assert cfg.model == "claude-opus-5"


def test_load_config_env_var_takes_precedence_over_file(
    isolated_paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated_paths.config_dir.mkdir(parents=True)
    isolated_paths.config_path.write_text('raindrop_token = "file-token"\n')
    monkeypatch.setenv("RAINDROP_TOKEN", "env-token")

    cfg = load_config()

    assert cfg.raindrop_token == "env-token"


def test_load_config_state_db_path_and_output_dir_from_file(isolated_paths: SimpleNamespace) -> None:
    custom_state_db = isolated_paths.state_dir / "custom" / "custom-state.db"
    custom_output_dir = isolated_paths.output_dir / "custom-digests"
    isolated_paths.config_dir.mkdir(parents=True)
    isolated_paths.config_path.write_text(
        f'state_db_path = "{custom_state_db}"\n'
        f'output_dir = "{custom_output_dir}"\n'
    )

    cfg = load_config()

    assert cfg.state_db_path == custom_state_db
    assert cfg.output_dir == custom_output_dir
    assert custom_state_db.parent.is_dir()
    assert custom_output_dir.is_dir()


def test_load_config_output_dir_env_var_overrides_file(
    isolated_paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_output_dir = isolated_paths.output_dir / "from-file"
    env_output_dir = isolated_paths.output_dir / "from-env"
    isolated_paths.config_dir.mkdir(parents=True)
    isolated_paths.config_path.write_text(f'output_dir = "{file_output_dir}"\n')
    monkeypatch.setenv("UNHOARD_OUTPUT_DIR", str(env_output_dir))

    cfg = load_config()

    assert cfg.output_dir == env_output_dir


def test_write_default_config_creates_file(isolated_paths: SimpleNamespace) -> None:
    path = write_default_config()

    assert path == isolated_paths.config_path
    assert path.exists()
    assert "unhoard config" in path.read_text()


def test_write_default_config_does_not_overwrite_without_force(isolated_paths: SimpleNamespace) -> None:
    write_default_config()
    isolated_paths.config_path.write_text("# customized by hand\n")

    write_default_config(force=False)

    assert isolated_paths.config_path.read_text() == "# customized by hand\n"


def test_write_default_config_overwrites_with_force(isolated_paths: SimpleNamespace) -> None:
    write_default_config()
    isolated_paths.config_path.write_text("# customized by hand\n")

    write_default_config(force=True)

    assert "unhoard config" in isolated_paths.config_path.read_text()
