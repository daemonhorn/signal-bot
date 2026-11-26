import asyncio

import pytest

from signal_bot.bot import Bot, chunk_text, parse_envelope, strip_trigger
from signal_bot.claude_runner import ClaudeRunner
from signal_bot.sessions import SessionStore

ACCOUNT = "+15550000000"
UUID = "0f0e0d0c-0b0a-0908-0706-050403020100"


class FakeSignal:
    def __init__(self):
        self.sent = []
        self.reactions = []

    async def send(self, recipient, text):
        self.sent.append((recipient, text))

    async def typing(self, recipient, on):
        pass

    async def react(self, recipient, author, ts, emoji):
        self.reactions.append((recipient, ts, emoji))


def dm(text, number="+15551112222", uuid="11111111-2222-3333-4444-555555555555", group=False):
    data = {"timestamp": 42, "message": text}
    if group:
        data["groupInfo"] = {"groupId": "g"}
    return {"envelope": {"sourceNumber": number, "sourceUuid": uuid, "timestamp": 42, "dataMessage": data},
            "account": ACCOUNT}


def note_to_self(text):
    return {"envelope": {"sourceNumber": ACCOUNT, "sourceUuid": UUID, "timestamp": 7,
                         "syncMessage": {"sentMessage": {"destinationNumber": ACCOUNT, "timestamp": 7,
                                                         "message": text}}},
            "account": ACCOUNT}


@pytest.fixture
def bot(cfg):
    return Bot(cfg, FakeSignal(), ClaudeRunner(cfg.claude), SessionStore(cfg.bot.state_dir))


@pytest.mark.parametrize(
    "text,expected",
    [
        ("@claude hi", "hi"),
        ("  @Claude: hi there", "hi there"),
        ("@CLAUDE, /new", "/new"),
        ("@claude", ""),
        ("hi @claude", None),
        ("@claudette hi", None),
        ("claude hi", None),
    ],
)
def test_strip_trigger(text, expected):
    assert strip_trigger(text, "@claude") == expected


def test_chunk_text():
    assert chunk_text("short", 100) == ["short"]
    text = "\n\n".join(["para " + "x" * 80] * 10)
    chunks = chunk_text(text, 200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 + 10 for c in chunks)
    assert chunks[1].startswith("(2/")
    assert chunk_text("y" * 450, 200)[0] == "y" * 200


def test_parse_envelope_kinds():
    m, ids = parse_envelope(dm("x"), ACCOUNT)
    assert m.sender == "+15551112222" and ids[1].startswith("uuid:")
    assert parse_envelope(dm("x", group=True), ACCOUNT)[0] is None
    assert parse_envelope({"envelope": {"sourceNumber": "+15551112222", "receiptMessage": {}}}, ACCOUNT)[0] is None
    m, _ = parse_envelope(note_to_self("@claude hi"), ACCOUNT)
    assert m.sender == ACCOUNT and m.reply_to == ACCOUNT
    # A sync of a message the owner sent to someone else is not for us.
    other = note_to_self("@claude hi")
    other["envelope"]["syncMessage"]["sentMessage"]["destinationNumber"] = "+15559999999"
    assert parse_envelope(other, ACCOUNT)[0] is None


async def _drain(bot):
    while bot._tasks:
        await asyncio.gather(*list(bot._tasks))


async def test_whitelisted_with_trigger(bot, fake_claude):
    bot.handle_payload(dm("@claude hello"))
    await _drain(bot)
    assert bot.signal.sent == [("+15551112222", "echo: hello")]
    assert bot.signal.reactions == [("+15551112222", 42, "👀")]
    assert bot.sessions.get("+15551112222") == "sess-1"


async def test_no_trigger_ignored(bot, fake_claude):
    _, log = fake_claude
    assert bot.handle_payload(dm("hello")) is None
    await _drain(bot)
    assert bot.signal.sent == [] and not log.exists()


async def test_unknown_sender_ignored(bot, fake_claude):
    _, log = fake_claude
    assert bot.handle_payload(dm("@claude hi", number="+15553334444")) is None
    await _drain(bot)
    assert bot.signal.sent == [] and not log.exists()


async def test_unknown_sender_reply_option(bot, fake_claude):
    bot.cfg.access.reply_to_unknown = True
    bot.handle_payload(dm("@claude hi", number="+15553334444"))
    await _drain(bot)
    assert "not authorised" in bot.signal.sent[0][1]


async def test_uuid_whitelist(bot, fake_claude):
    bot.handle_payload(dm("@claude hi", number=None, uuid=UUID.upper()))
    await _drain(bot)
    assert bot.signal.sent == [("uuid:" + UUID, "echo: hi")]


async def test_new_and_resume(bot, fake_claude):
    _, log = fake_claude
    bot.handle_payload(dm("@claude one"))
    await _drain(bot)
    bot.handle_payload(dm("@claude two"))
    await _drain(bot)
    assert "--resume" in log.read_text().splitlines()[1]
    bot.handle_payload(dm("@claude /new"))
    await _drain(bot)
    assert bot.sessions.get("+15551112222") is None
    assert bot.signal.sent[-1][1] == "Started a new conversation."


async def test_stale_session_retries(bot, fake_claude):
    bot.sessions.set("+15551112222", "stale")
    bot.handle_payload(dm("@claude hi"))
    await _drain(bot)
    assert bot.sessions.get("+15551112222") == "sess-1"
    assert "could not be resumed" in bot.signal.sent[-1][1]


async def test_help_and_denied(bot, fake_claude):
    bot.handle_payload(dm("@claude"))
    await _drain(bot)
    assert "/new" in bot.signal.sent[-1][1]
    bot.handle_payload(dm("@claude write"))
    await _drain(bot)
    assert "[blocked tools: Write]" in bot.signal.sent[-1][1]


async def test_queue_and_cancel(bot, fake_claude):
    bot.handle_payload(dm("@claude sleep"))
    await asyncio.sleep(0.5)
    bot.handle_payload(dm("@claude after"))
    await asyncio.sleep(0.2)
    assert any("Queued" in t for _, t in bot.signal.sent)
    bot.handle_payload(dm("@claude /cancel"))
    await asyncio.wait_for(_drain(bot), 10)
    texts = [t for _, t in bot.signal.sent]
    assert "Cancelled." in texts and "echo: after" in texts


async def test_note_to_self_requires_owner_whitelisted(bot, fake_claude):
    bot.handle_payload(note_to_self("@claude hi"))
    await _drain(bot)
    assert bot.signal.sent == []  # owner not in whitelist
    bot.cfg.access = type(bot.cfg.access)(allowed=frozenset({ACCOUNT}))
    bot.handle_payload(note_to_self("@claude hi"))
    await _drain(bot)
    assert bot.signal.sent == [(ACCOUNT, "echo: hi")]
