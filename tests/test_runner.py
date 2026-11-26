import asyncio
import json

from signal_bot.claude_runner import ClaudeRunner, kill_process_group, parse_output


def _calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_argv(cfg):
    argv = ClaudeRunner(cfg.claude).build_argv("abc")
    assert argv[1:5] == ["-p", "--output-format", "json", "--permission-mode"]
    assert argv[argv.index("--allowedTools") + 1] == "Read,Grep"
    assert argv[-2:] == ["--resume", "abc"]
    assert "--resume" not in ClaudeRunner(cfg.claude).build_argv(None)


async def test_run_and_resume(cfg, fake_claude):
    _, log = fake_claude
    r = ClaudeRunner(cfg.claude)
    res = await r.run("-rf hello", None)  # leading dash must not be parsed as a flag
    assert res.text == "echo: -rf hello" and res.session_id == "sess-1" and not res.is_error
    res = await r.run("again", "sess-1")
    assert res.session_id == "sess-1"
    calls = _calls(log)
    assert calls[0]["prompt"] == "-rf hello"
    assert "--resume" in calls[1]["args"]
    assert calls[0]["cwd"] == str(cfg.claude.workdir)


async def test_denials(cfg, fake_claude):
    res = await ClaudeRunner(cfg.claude).run("write", None)
    assert res.denied_tools == ["Write"]


async def test_garbage_output(cfg, fake_claude):
    res = await ClaudeRunner(cfg.claude).run("garbage", None)
    assert res.is_error and "not json" in res.text


async def test_timeout(cfg, fake_claude):
    cfg.claude.timeout_seconds = 1
    res = await ClaudeRunner(cfg.claude).run("sleep", None)
    assert res.timed_out and res.is_error


async def test_cancel(cfg, fake_claude):
    procs = []
    task = asyncio.create_task(ClaudeRunner(cfg.claude).run("sleep", None, on_start=procs.append))
    while not procs:
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.2)
    kill_process_group(procs[0])
    res = await asyncio.wait_for(task, 5)
    assert res.cancelled


def test_parse_error_json():
    res = parse_output('{"is_error": true, "subtype": "error_max_turns"}', "", 1)
    assert res.is_error and "error_max_turns" in res.text
