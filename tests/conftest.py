import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """No test may touch ~/.instinct, the real chat.db, or real credentials."""
    monkeypatch.setenv("INSTINCT_CONFIG", str(tmp_path / "config.toml"))
    for var in ("INSTINCT_CANVAS_TOKEN", "INSTINCT_CANVAS_BASE_URL", "INSTINCT_CANVAS_BACKEND",
                "INSTINCT_CLAUDE_MODE", "INSTINCT_MESSAGES_DB", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path
