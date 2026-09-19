from pathlib import Path

import pytest

from instinct import config
from instinct.config import load_config


def test_defaults_without_file(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.source is None
    assert cfg.claude.default_mode == "api"
    assert cfg.canvas.backend == "auto"


def test_file_and_env_overrides(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text('[canvas]\nbase_url = "https://canvas.example.edu/"\n'
                 '[browser]\nprofile_dir = "~/x"\ncdp_port = 9999\n')
    cfg = load_config(p)
    assert cfg.canvas.base_url == "https://canvas.example.edu"
    assert cfg.browser.profile_dir == Path("~/x").expanduser()
    assert cfg.browser.cdp_port == 9999
    monkeypatch.setenv("INSTINCT_CANVAS_BASE_URL", "https://other.edu")
    monkeypatch.setenv("INSTINCT_CLAUDE_MODE", "cli")
    cfg = load_config(p)
    assert cfg.canvas.base_url == "https://other.edu"
    assert cfg.claude.default_mode == "cli"


def test_unknown_keys_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[canvas]\ntoken = 'nope'\n")
    with pytest.raises(ValueError, match="unknown config key"):
        load_config(p)


def test_canvas_token_env_wins_over_keychain(monkeypatch):
    monkeypatch.setattr(config, "keychain_get", lambda *a, **k: "from-keychain")
    assert config.canvas_token() == "from-keychain"
    monkeypatch.setenv("INSTINCT_CANVAS_TOKEN", "from-env")
    assert config.canvas_token() == "from-env"
