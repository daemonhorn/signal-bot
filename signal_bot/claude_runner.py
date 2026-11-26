"""Run Claude Code headless (`claude -p`) and parse its JSON result."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import Callable

from .config import ClaudeConfig

log = logging.getLogger(__name__)


@dataclass
class ClaudeResult:
    text: str
    session_id: str | None = None
    is_error: bool = False
    cost_usd: float | None = None
    denied_tools: list[str] = field(default_factory=list)
    cancelled: bool = False
    timed_out: bool = False


class ClaudeRunner:
    def __init__(self, cfg: ClaudeConfig):
        self.cfg = cfg

    def build_argv(self, session_id: str | None) -> list[str]:
        c = self.cfg
        argv = [c.binary, "-p", "--output-format", "json", "--permission-mode", c.permission_mode]
        if c.allowed_tools:
            argv += ["--allowedTools", ",".join(c.allowed_tools)]
        if c.model:
            argv += ["--model", c.model]
        if c.max_budget_usd > 0:
            argv += ["--max-budget-usd", str(c.max_budget_usd)]
        if c.append_system_prompt:
            argv += ["--append-system-prompt", c.append_system_prompt]
        argv += c.extra_args
        if session_id:
            argv += ["--resume", session_id]
        return argv

    async def run(
        self,
        prompt: str,
        session_id: str | None,
        on_start: Callable[[asyncio.subprocess.Process], None] | None = None,
    ) -> ClaudeResult:
        """Run one prompt. The prompt goes over stdin so it can never be parsed as a flag.

        ``on_start(proc)`` is called once the process exists, so callers can cancel it.
        """
        argv = self.build_argv(session_id)
        log.info("running claude (resume=%s)", session_id or "-")
        self.cfg.workdir.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.cfg.workdir),
            start_new_session=True,  # own process group, so we can kill tool subprocesses too
        )
        if on_start:
            on_start(proc)
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=self.cfg.timeout_seconds
            )
        except asyncio.TimeoutError:
            kill_process_group(proc)
            await proc.wait()
            return ClaudeResult(
                text=f"Timed out after {self.cfg.timeout_seconds}s.", is_error=True, timed_out=True
            )

        if proc.returncode is not None and proc.returncode < 0:
            return ClaudeResult(text="Cancelled.", is_error=True, cancelled=True)
        return parse_output(out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode)


def kill_process_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def parse_output(stdout: str, stderr: str, returncode: int | None) -> ClaudeResult:
    data = None
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if data is None:
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = None

    if not isinstance(data, dict):
        detail = (stderr.strip() or stdout.strip() or f"exit code {returncode}")[-500:]
        log.error("claude produced no JSON (rc=%s): %s", returncode, detail)
        return ClaudeResult(text=f"Claude failed: {detail}", is_error=True)

    denied = []
    for d in data.get("permission_denials") or []:
        if isinstance(d, dict) and d.get("tool_name"):
            denied.append(str(d["tool_name"]))

    text = data.get("result")
    if not isinstance(text, str) or not text.strip():
        text = "(no text in reply)" if not data.get("is_error") else f"Claude error: {data.get('subtype', 'unknown')}"
    return ClaudeResult(
        text=text,
        session_id=data.get("session_id"),
        is_error=bool(data.get("is_error")),
        cost_usd=data.get("total_cost_usd"),
        denied_tools=sorted(set(denied)),
    )
