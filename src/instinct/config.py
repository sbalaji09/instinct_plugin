"""Configuration: ~/.instinct/config.toml (or $INSTINCT_CONFIG) plus env overrides.

Secrets never live in the config file. The Canvas token comes from
$INSTINCT_CANVAS_TOKEN or the macOS Keychain; the Anthropic key from
$ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

INSTINCT_HOME = Path(os.environ.get("INSTINCT_HOME", "~/.instinct")).expanduser()
KEYCHAIN_SERVICE = "instinct"
KEYCHAIN_CANVAS_ACCOUNT = "canvas-token"
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


@dataclass
class MessagesConfig:
    db_path: Path = Path("~/Library/Messages/chat.db").expanduser()
    resolve_contacts: bool = True


@dataclass
class CanvasConfig:
    base_url: str = ""
    # "auto": token if one is configured, else the logged-in background browser profile.
    # "api": token only. "browser": background profile only.
    backend: str = "auto"
    max_retries: int = 4


@dataclass
class BrowserConfig:
    profile_dir: Path = INSTINCT_HOME / "chrome-profile"
    chrome_path: str = DEFAULT_CHROME
    cdp_port: int = 9333
    headless: bool = True
    response_timeout_s: float = 180.0


@dataclass
class ClaudeConfig:
    default_mode: str = "api"
    model: str = "claude-opus-5"
    max_tokens: int = 16000
    claude_cli: str = "claude"


@dataclass
class GuiConfig:
    cua_driver_path: str = ""  # empty = look up on PATH / known install locations


@dataclass
class SafetyConfig:
    pending_ttl_s: int = 600
    log_message_bodies: bool = False


@dataclass
class BridgeConfig:
    # Conversations to watch: the phone number / email / contact name / "chat:N" of the
    # assistant you text (e.g. Instinct AI). Empty = bridge disabled.
    chats: list[str] = field(default_factory=list)
    trigger: str = "@mac"           # a message must start with this to become a task
    reply_prefix: str = "🖥️ "       # every bridge message starts with this (and is ignored as input)
    poll_s: float = 2.0
    approval_timeout_s: int = 600
    max_tasks_per_hour: int = 20
    session_idle_min: int = 30      # follow-ups within this window continue the same Claude session
    model: str = ""                 # empty = your Claude Code default
    max_turns: int = 40


@dataclass
class Config:
    messages: MessagesConfig = field(default_factory=MessagesConfig)
    canvas: CanvasConfig = field(default_factory=CanvasConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    gui: GuiConfig = field(default_factory=GuiConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    home: Path = INSTINCT_HOME
    source: Path | None = None

    def ensure_home(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self.home


def config_path() -> Path:
    return Path(os.environ.get("INSTINCT_CONFIG", INSTINCT_HOME / "config.toml")).expanduser()


def _apply(section: object, values: dict) -> None:
    for key, value in values.items():
        if not hasattr(section, key):
            raise ValueError(f"unknown config key {type(section).__name__}.{key}")
        current = getattr(section, key)
        if isinstance(current, Path):
            value = Path(str(value)).expanduser()
        setattr(section, key, value)


def load_config(path: Path | None = None) -> Config:
    cfg = Config()
    path = path or config_path()
    if path.exists():
        data = tomllib.loads(path.read_text())
        for name, values in data.items():
            if name in {"home", "source"} or not hasattr(cfg, name):
                raise ValueError(f"unknown config section [{name}] in {path}")
            _apply(getattr(cfg, name), values)
        cfg.source = path

    env = os.environ
    if url := env.get("INSTINCT_CANVAS_BASE_URL"):
        cfg.canvas.base_url = url
    if backend := env.get("INSTINCT_CANVAS_BACKEND"):
        cfg.canvas.backend = backend
    if mode := env.get("INSTINCT_CLAUDE_MODE"):
        cfg.claude.default_mode = mode
    if db := env.get("INSTINCT_MESSAGES_DB"):
        cfg.messages.db_path = Path(db).expanduser()
    cfg.canvas.base_url = cfg.canvas.base_url.rstrip("/")
    return cfg


def keychain_get(account: str, service: str = KEYCHAIN_SERVICE) -> str | None:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def canvas_token() -> str | None:
    return os.environ.get("INSTINCT_CANVAS_TOKEN") or keychain_get(KEYCHAIN_CANVAS_ACCOUNT)
