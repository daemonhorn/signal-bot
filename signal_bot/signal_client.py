"""Minimal async client for bbernhard/signal-cli-rest-api in json-rpc mode."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)


class SignalError(RuntimeError):
    pass


def _recipient(identity: str) -> str:
    """Turn a normalised identity (``+1555…`` or ``uuid:…``) into an API recipient."""
    return identity[5:] if identity.startswith("uuid:") else identity


class SignalClient:
    def __init__(self, api_url: str, account: str, session: aiohttp.ClientSession):
        self.api_url = api_url.rstrip("/")
        self.account = account
        self._http = session
        self._acct = quote(account, safe="")

    async def about(self) -> dict:
        async with self._http.get(f"{self.api_url}/v1/about") as r:
            r.raise_for_status()
            return await r.json()

    async def send(self, recipient: str, text: str) -> None:
        body = {"message": text, "number": self.account, "recipients": [_recipient(recipient)]}
        async with self._http.post(f"{self.api_url}/v2/send", json=body) as r:
            if r.status >= 300:
                raise SignalError(f"send failed: HTTP {r.status}: {(await r.text())[:300]}")

    async def typing(self, recipient: str, on: bool) -> None:
        method = self._http.put if on else self._http.delete
        try:
            async with method(
                f"{self.api_url}/v1/typing-indicator/{self._acct}",
                json={"recipient": _recipient(recipient)},
            ) as r:
                if r.status >= 300:
                    log.debug("typing indicator HTTP %s", r.status)
        except aiohttp.ClientError as e:
            log.debug("typing indicator failed: %s", e)

    async def react(self, recipient: str, target_author: str, timestamp: int, emoji: str) -> None:
        body = {
            "reaction": emoji,
            "recipient": _recipient(recipient),
            "target_author": _recipient(target_author),
            "timestamp": timestamp,
        }
        try:
            async with self._http.post(f"{self.api_url}/v1/reactions/{self._acct}", json=body) as r:
                if r.status >= 300:
                    log.debug("reaction HTTP %s", r.status)
        except aiohttp.ClientError as e:
            log.debug("reaction failed: %s", e)

    async def receive(self) -> AsyncIterator[dict]:
        """Yield envelopes forever, reconnecting the websocket with backoff."""
        ws_url = self.api_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        url = f"{ws_url}/v1/receive/{self._acct}"
        backoff = 1.0
        while True:
            try:
                async with self._http.ws_connect(url, heartbeat=30) as ws:
                    log.info("connected to %s", url)
                    backoff = 1.0
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                            except json.JSONDecodeError:
                                log.warning("non-JSON frame from signal-cli: %.200s", msg.data)
                                continue
                            if isinstance(data, dict):
                                yield data
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
                log.warning("websocket closed; reconnecting")
            except (aiohttp.ClientError, OSError) as e:
                log.warning("websocket error: %s; retrying in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
