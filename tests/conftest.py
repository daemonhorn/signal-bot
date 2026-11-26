import os
import stat
import textwrap
from pathlib import Path

import pytest

from signal_bot.config import load_config

FAKE_CLAUDE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    # Stand-in for `claude -p`: behaviour is selected by the prompt text on stdin.
    import json, os, sys, time
    prompt = sys.stdin.read()
    args = sys.argv[1:]
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as fh:
        fh.write(json.dumps({"args": args, "prompt": prompt, "cwd": os.getcwd()}) + "\\n")
    resume = args[args.index("--resume") + 1] if "--resume" in args else None
    if prompt == "sleep":
        time.sleep(30)
    if prompt == "garbage":
        print("not json"); sys.exit(1)
    if resume == "stale":
        print(json.dumps({"type": "result", "is_error": True, "subtype": "error",
                          "result": "No conversation found"})); sys.exit(1)
    denials = [{"tool_name": "Write"}] if prompt == "write" else []
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": f"echo: {prompt}", "session_id": resume or "sess-1",
                      "total_cost_usd": 0.01, "permission_denials": denials}))
    """
)


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLAUDE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    return path, log


@pytest.fixture
def config_file(tmp_path, fake_claude):
    binary, _ = fake_claude
    p = tmp_path / "config.toml"
    p.write_text(
        textwrap.dedent(
            f"""\
            [signal]
            account = "+15550000000"
            [access]
            allowed = ["+1 555 111 2222", "uuid:0F0E0D0C-0B0A-0908-0706-050403020100"]
            [bot]
            trigger = "@claude"
            [claude]
            binary = "{binary}"
            workdir = "{tmp_path / 'work'}"
            allowed_tools = ["Read", "Grep"]
            timeout_seconds = 10
            """
        )
    )
    return p


@pytest.fixture
def cfg(config_file):
    return load_config(config_file)
