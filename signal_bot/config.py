"""Load and validate config.toml."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


class ConfigError(ValueError):
    pass


def normalize_identity(raw: str) -> str:
    """Normalise a whitelist entry / sender id.

    Phone numbers become E.164 (``+15551234567``); Signal ACI UUIDs become
    ``uuid:<lowercase uuid>``. Raises ConfigError for anything else.
    """
    s = raw.strip()
    if s.lower().startswith("uuid:"):
        s = s[5:].strip()
    if _UUID_RE.match(s.lower()):
        return "uuid:" + s.lower()
    num = re.sub(r"[\s\-().]", "", s)
    if num.startswith("00"):
        num = "+" + num[2:]
    if _E164_RE.match(num):
        return num
    raise ConfigError(f"not a valid E.164 phone number or Signal UUID: {raw!r}")


@dataclass
class SignalConfig:
    api_url: str
    account: str


@dataclass
class AccessConfig:
    allowed: frozenset[str]
    reply_to_unknown: bool = False


@dataclass
class BotConfig:
    trigger: str = "@claude"
    state_dir: Path = Path("state")
    max_concurrent: int = 2
    chunk_size: int = 1800


@dataclass
class ClaudeConfig:
    binary: str = "claude"
    workdir: Path = Path.home()
    allowed_tools: list[str] = field(default_factory=lambda: ["Read", "Glob", "Grep"])
    permission_mode: str = "dontAsk"
    model: str = ""
    timeout_seconds: int = 600
    max_budget_usd: float = 0.0
    append_system_prompt: str = ""
    extra_args: list[str] = field(default_factory=list)


@dataclass
class Config:
    signal: SignalConfig
    access: AccessConfig
    bot: BotConfig
    claude: ClaudeConfig
    path: Path


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from e

    base = path.parent

    sig = data.get("signal", {})
    if not sig.get("account"):
        raise ConfigError("[signal].account is required")
    signal = SignalConfig(
        api_url=str(sig.get("api_url", "http://127.0.0.1:8080")).rstrip("/"),
        account=normalize_identity(sig["account"]),
    )
    if signal.account.startswith("uuid:"):
        raise ConfigError("[signal].account must be a phone number")

    acc = data.get("access", {})
    allowed_raw = acc.get("allowed", [])
    if not isinstance(allowed_raw, list) or not allowed_raw:
        raise ConfigError("[access].allowed must be a non-empty list")
    access = AccessConfig(
        allowed=frozenset(normalize_identity(x) for x in allowed_raw),
        reply_to_unknown=bool(acc.get("reply_to_unknown", False)),
    )

    b = data.get("bot", {})
    trigger = str(b.get("trigger", "@claude")).strip()
    if not trigger:
        raise ConfigError("[bot].trigger must not be empty")
    state_dir = Path(b.get("state_dir", "state")).expanduser()
    bot = BotConfig(
        trigger=trigger,
        state_dir=state_dir if state_dir.is_absolute() else base / state_dir,
        max_concurrent=max(1, int(b.get("max_concurrent", 2))),
        chunk_size=max(200, int(b.get("chunk_size", 1800))),
    )

    c = data.get("claude", {})
    workdir = Path(c.get("workdir", str(Path.home()))).expanduser()
    tools = c.get("allowed_tools", ["Read", "Glob", "Grep"])
    if not isinstance(tools, list):
        raise ConfigError("[claude].allowed_tools must be a list")
    if c.get("permission_mode") == "bypassPermissions":
        raise ConfigError(
            "[claude].permission_mode = 'bypassPermissions' is refused; "
            "grant tools explicitly via allowed_tools instead"
        )
    claude = ClaudeConfig(
        binary=str(c.get("binary", "claude")),
        workdir=workdir,
        allowed_tools=[str(t) for t in tools],
        permission_mode=str(c.get("permission_mode", "dontAsk")),
        model=str(c.get("model", "")),
        timeout_seconds=max(10, int(c.get("timeout_seconds", 600))),
        max_budget_usd=float(c.get("max_budget_usd", 0.0)),
        append_system_prompt=str(c.get("append_system_prompt", "")),
        extra_args=[str(a) for a in c.get("extra_args", [])],
    )
    if any("dangerously-skip-permissions" in a for a in claude.extra_args):
        raise ConfigError("--dangerously-skip-permissions is refused in [claude].extra_args")

    return Config(signal=signal, access=access, bot=bot, claude=claude, path=path)
