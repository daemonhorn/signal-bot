"""Entry point: `signal-bot --config /path/to/config.toml`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

import aiohttp

from .bot import Bot
from .claude_runner import ClaudeRunner
from .config import ConfigError, load_config
from .sessions import SessionStore
from .signal_client import SignalClient

log = logging.getLogger("signal_bot")


async def amain(config_path: str, check: bool) -> int:
    cfg = load_config(config_path)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        sig = SignalClient(cfg.signal.api_url, cfg.signal.account, http)
        try:
            about = await sig.about()
        except Exception as e:
            log.error("signal-cli REST API not reachable at %s: %s", cfg.signal.api_url, e)
            return 2
        log.info("signal-cli API %s (mode %s)", about.get("version"), about.get("mode"))
        if about.get("mode") not in ("json-rpc", "json-rpc-native"):
            log.error("signal-cli container must run with MODE=json-rpc (got %r)", about.get("mode"))
            return 2
        log.info(
            "account %s, %d whitelisted, trigger %r, tools %s, workdir %s",
            cfg.signal.account, len(cfg.access.allowed), cfg.bot.trigger,
            ",".join(cfg.claude.allowed_tools) or "(none)", cfg.claude.workdir,
        )
        if check:
            print("config OK, signal-cli reachable")
            return 0

        bot = Bot(cfg, sig, ClaudeRunner(cfg.claude), SessionStore(cfg.bot.state_dir))
        main_task = asyncio.create_task(bot.run())
        loop = asyncio.get_running_loop()
        for s in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(s, main_task.cancel)
        try:
            await main_task
        except asyncio.CancelledError:
            log.info("shutting down")
        return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="signal-bot", description="Signal -> Claude Code bridge")
    p.add_argument("--config", default=os.environ.get("SIGNAL_BOT_CONFIG", "config.toml"))
    p.add_argument("--check", action="store_true", help="validate config and API reachability, then exit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    try:
        sys.exit(asyncio.run(amain(args.config, args.check)))
    except ConfigError as e:
        log.error("config error: %s", e)
        sys.exit(2)


if __name__ == "__main__":
    main()
