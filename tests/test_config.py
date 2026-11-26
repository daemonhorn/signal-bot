import pytest

from signal_bot.config import ConfigError, load_config, normalize_identity


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+15551234567", "+15551234567"),
        ("+1 (555) 123-4567", "+15551234567"),
        ("0044 7700 900123", "+447700900123"),
        ("0F0E0D0C-0B0A-0908-0706-050403020100", "uuid:0f0e0d0c-0b0a-0908-0706-050403020100"),
        ("uuid:0f0e0d0c-0b0a-0908-0706-050403020100", "uuid:0f0e0d0c-0b0a-0908-0706-050403020100"),
    ],
)
def test_normalize(raw, expected):
    assert normalize_identity(raw) == expected


@pytest.mark.parametrize("raw", ["5551234567", "+0123", "bob", ""])
def test_normalize_rejects(raw):
    with pytest.raises(ConfigError):
        normalize_identity(raw)


def test_load(cfg, tmp_path):
    assert cfg.signal.account == "+15550000000"
    assert cfg.access.allowed == {"+15551112222", "uuid:0f0e0d0c-0b0a-0908-0706-050403020100"}
    assert cfg.bot.state_dir == tmp_path / "state"
    assert cfg.claude.permission_mode == "dontAsk"


def test_refuses_bypass(config_file):
    config_file.write_text(config_file.read_text() + 'permission_mode = "bypassPermissions"\n')
    with pytest.raises(ConfigError, match="bypassPermissions"):
        load_config(config_file)


def test_requires_whitelist(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[signal]\naccount = "+15550000000"\n[access]\nallowed = []\n')
    with pytest.raises(ConfigError, match="allowed"):
        load_config(p)
