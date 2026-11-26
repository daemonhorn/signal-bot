"""Message dispatch: whitelist, trigger prefix, commands, per-user serialisation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass, field

from .claude_runner import ClaudeResult, ClaudeRunner, kill_process_group
from .config import Config, ConfigError, normalize_identity
from .sessions import SessionStore

log = logging.getLogger(__name__)

HELP = """Start every message with {trigger}, for example:
  {trigger} what's using disk space in ~/projects?

Commands:
  {trigger} /new     start a fresh conversation
  {trigger} /status  show session and queue state
  {trigger} /cancel  stop the running request
  {trigger} /help    this text"""


@dataclass
class Incoming:
    sender: str          # normalised identity, e.g. "+15551234567" or "uuid:…"
    reply_to: str        # who to send the reply to
    text: str
    timestamp: int       # message timestamp, used to react to it
    author: str          # target_author for reactions


@dataclass
class UserState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: int = 0
    proc: asyncio.subprocess.Process | None = None


def _identity(number: str | None, uuid: str | None) -> list[str]:
    ids = []
    for raw in (number, uuid):
        if raw:
            with contextlib.suppress(ConfigError):
                ids.append(normalize_identity(raw))
    return ids


def parse_envelope(payload: dict, account: str) -> tuple[Incoming | None, list[str]]:
    """Extract a 1:1 text message from a signal-cli receive payload.

    Returns (message or None, candidate sender identities). Group messages,
    receipts, typing events, reactions etc. yield None.
    """
    env = payload.get("envelope")
    if not isinstance(env, dict):
        return None, []

    number = env.get("sourceNumber")
    if not number and isinstance(env.get("source"), str) and env["source"].startswith("+"):
        number = env["source"]
    uuid = env.get("sourceUuid")
    ids = _identity(number, uuid)

    dm = env.get("dataMessage")
    if isinstance(dm, dict):
        if dm.get("groupInfo") or dm.get("groupV2"):
            return None, ids
        text = dm.get("message")
        if not isinstance(text, str) or not ids:
            return None, ids
        ts = int(dm.get("timestamp") or env.get("timestamp") or 0)
        return Incoming(sender=ids[0], reply_to=ids[0], text=text, timestamp=ts, author=ids[0]), ids

    # Linked-device mode: messages the account owner sends to "Note to Self"
    # arrive as sync messages from our own number.
    sync = env.get("syncMessage")
    if isinstance(sync, dict) and isinstance(sync.get("sentMessage"), dict):
        sent = sync["sentMessage"]
        dest = sent.get("destinationNumber") or sent.get("destination")
        if dest != account or sent.get("groupInfo") or sent.get("groupV2"):
            return None, ids
        if account not in ids and number != account:
            return None, ids
        text = sent.get("message")
        if not isinstance(text, str):
            return None, ids
        ts = int(sent.get("timestamp") or env.get("timestamp") or 0)
        return Incoming(sender=account, reply_to=account, text=text, timestamp=ts, author=account), [account]

    return None, ids


def strip_trigger(text: str, trigger: str) -> str | None:
    """Return the text after the trigger, or None if the message doesn't start with it.

    Matching is case-insensitive and tolerates leading whitespace and a
    following ``:`` or ``,`` (``@Claude: hi``). ``@claudette`` does not match.
    """
    m = re.match(rf"\s*{re.escape(trigger)}(?![\w-])[\s:,]*", text, re.IGNORECASE)
    if not m:
        return None
    return text[m.end():].strip()


def chunk_text(text: str, size: int) -> list[str]:
    """Split text into pieces <= size, preferring paragraph, then line, then word breaks."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks: list[str] = []
    while len(text) > size:
        window = text[:size]
        cut = -1
        for sep in ("\n\n", "\n", " "):
            cut = window.rfind(sep)
            if cut >= size // 3:
                break
        if cut < size // 3:
            cut = size
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    if len(chunks) > 1:
        n = len(chunks)
        chunks = [f"({i}/{n}) {c}" if i > 1 else c for i, c in enumerate(chunks, 1)]
    return chunks


class Bot:
    def __init__(self, cfg: Config, signal, runner: ClaudeRunner, sessions: SessionStore):
        self.cfg = cfg
        self.signal = signal
        self.runner = runner
        self.sessions = sessions
        self.users: dict[str, UserState] = {}
        self.sem = asyncio.Semaphore(cfg.bot.max_concurrent)
        self._tasks: set[asyncio.Task] = set()

    # ---- entry point ------------------------------------------------------------------
    async def run(self) -> None:
        async for payload in self.signal.receive():
            try:
                self.handle_payload(payload)
            except Exception:  # never let one bad frame kill the loop
                log.exception("error handling payload")

    def handle_payload(self, payload: dict) -> asyncio.Task | None:
        msg, ids = parse_envelope(payload, self.cfg.signal.account)
        if msg is None:
            return None
        if not any(i in self.cfg.access.allowed for i in ids):
            log.warning("ignoring message from non-whitelisted sender %s", "/".join(ids) or "?")
            if self.cfg.access.reply_to_unknown and strip_trigger(msg.text, self.cfg.bot.trigger) is not None:
                return self._spawn(self.signal.send(msg.reply_to, "Sorry, you are not authorised to use this bot."))
            return None
        # Use the whitelist entry as the stable user key.
        msg.sender = next(i for i in ids if i in self.cfg.access.allowed)
        body = strip_trigger(msg.text, self.cfg.bot.trigger)
        if body is None:
            log.debug("no trigger from %s; ignoring", msg.sender)
            return None
        return self._spawn(self.handle_message(msg, body))

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    # ---- message handling ---------------------------------------------------------------
    async def reply(self, msg: Incoming, text: str) -> None:
        for chunk in chunk_text(text, self.cfg.bot.chunk_size):
            try:
                await self.signal.send(msg.reply_to, chunk)
            except Exception:
                log.exception("failed to send reply to %s", msg.reply_to)
                return

    async def handle_message(self, msg: Incoming, body: str) -> None:
        user = self.users.setdefault(msg.sender, UserState())
        cmd = body.split(maxsplit=1)[0].lower() if body else "/help"

        if cmd in ("/help", "help"):
            await self.reply(msg, HELP.format(trigger=self.cfg.bot.trigger))
            return
        if cmd == "/new":
            if user.lock.locked():
                await self.reply(msg, f"A request is still running; send {self.cfg.bot.trigger} /cancel first.")
                return
            self.sessions.clear(msg.sender)
            await self.reply(msg, "Started a new conversation.")
            return
        if cmd == "/status":
            sid = self.sessions.get(msg.sender) or "none"
            state = "busy" if user.lock.locked() else "idle"
            await self.reply(
                msg,
                f"Session: {sid}\nState: {state} ({user.pending} queued)\nWorkdir: {self.cfg.claude.workdir}",
            )
            return
        if cmd == "/cancel":
            if user.proc and user.proc.returncode is None:
                kill_process_group(user.proc)
                await self.reply(msg, "Cancelling…")
            else:
                await self.reply(msg, "Nothing is running.")
            return

        busy = user.lock.locked()
        user.pending += 1
        if busy:
            await self.reply(msg, "Queued; I'll start on this once the current request finishes.")
        try:
            async with user.lock:
                user.pending -= 1
                await self._run_claude(msg, body, user)
        finally:
            user.proc = None

    async def _run_claude(self, msg: Incoming, prompt: str, user: UserState) -> None:
        await self.signal.react(msg.reply_to, msg.author, msg.timestamp, "👀")
        typing = asyncio.create_task(self._keep_typing(msg.reply_to))

        def on_start(proc):
            user.proc = proc

        try:
            async with self.sem:
                sid = self.sessions.get(msg.sender)
                result = await self.runner.run(prompt, sid, on_start=on_start)
                if sid and result.is_error and not result.session_id and not result.cancelled and not result.timed_out:
                    # Stale/missing session (e.g. pruned): retry once from scratch.
                    log.info("resume of %s failed for %s; starting fresh", sid, msg.sender)
                    self.sessions.clear(msg.sender)
                    result = await self.runner.run(prompt, None, on_start=on_start)
                    if not result.is_error:
                        result.text = "(previous session could not be resumed; started a new one)\n\n" + result.text
        except Exception as e:
            log.exception("claude run failed")
            result = ClaudeResult(text=f"Internal error: {e}", is_error=True)
        finally:
            typing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing
            await self.signal.typing(msg.reply_to, False)

        if result.session_id:
            self.sessions.set(msg.sender, result.session_id)
        text = result.text
        if result.denied_tools:
            text += "\n\n[blocked tools: " + ", ".join(result.denied_tools) + "]"
        log.info(
            "reply to %s: %d chars, error=%s, cost=%s",
            msg.sender, len(text), result.is_error, result.cost_usd,
        )
        await self.reply(msg, text)

    async def _keep_typing(self, recipient: str) -> None:
        while True:
            await self.signal.typing(recipient, True)
            await asyncio.sleep(10)
