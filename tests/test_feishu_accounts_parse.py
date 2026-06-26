"""飞书 accounts 列表解析与 app_id 派生键名。"""

from pathlib import Path

from smartclaw.config.loader import (
    ConfigLoader,
    feishu_account_key_from_app_id,
)


def test_feishu_account_key_last_eight_and_collision() -> None:
    used: set[str] = set()
    k1 = feishu_account_key_from_app_id("cli_a979670bd4j19bb7", used)
    assert k1 == "acc_d4j19bb7"
    k2 = feishu_account_key_from_app_id("cli_zzzzzzzzd4j19bb7", used)
    assert k2 == "acc_d4j19bb7_2"
    assert feishu_account_key_from_app_id("short", used) == "acc_short"


def test_feishu_accounts_array_in_toml(tmp_path: Path) -> None:
    toml = b"""
[channels.feishu]
enabled = true

[[channels.feishu.accounts]]
app_id = "cli_11111111abcdefgh"
app_secret = "sec-one"

[[channels.feishu.accounts]]
app_id = "cli_22222222abcdefgh"
app_secret = "sec-two"
"""
    p = tmp_path / "config.toml"
    p.write_bytes(toml)
    cfg = ConfigLoader(config_path=p).load()
    assert cfg.channels.feishu.enabled
    keys = sorted(cfg.channels.feishu.accounts.keys())
    assert keys == ["acc_abcdefgh", "acc_abcdefgh_2"]
    assert cfg.channels.feishu.accounts["acc_abcdefgh"].app_secret == "sec-one"


def test_flat_single_bot_still_default() -> None:
    raw = {
        "channels": {
            "feishu": {
                "enabled": True,
                "app_id": "cli_flatid12",
                "app_secret": "flatsec",
            }
        }
    }
    cfg = ConfigLoader()._parse_config(raw)
    assert "default" in cfg.channels.feishu.accounts
    assert cfg.channels.feishu.accounts["default"].app_id == "cli_flatid12"
