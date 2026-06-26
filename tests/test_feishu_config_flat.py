"""飞书渠道配置解析（扁平 app_id / app_secret）。"""

from smartclaw.config.loader import ConfigLoader


def test_feishu_flat_top_level_credentials():
    loader = ConfigLoader(config_path=None)
    raw = {
        "channels": {
            "feishu": {
                "app_id": "cli_flat_test",
                "app_secret": "secret_flat",
                "encrypt_key": "enc_demo",
            }
        },
    }
    cfg = loader._parse_config(raw)
    assert "default" in cfg.channels.feishu.accounts
    acc = cfg.channels.feishu.accounts["default"]
    assert acc.app_id == "cli_flat_test"
    assert acc.app_secret == "secret_flat"
    assert acc.encrypt_key == "enc_demo"


def test_feishu_explicit_accounts_override_flat():
    loader = ConfigLoader(config_path=None)
    raw = {
        "channels": {
            "feishu": {
                "app_id": "cli_ignored",
                "app_secret": "ignored",
                "accounts": {
                    "bot1": {"app_id": "cli_real", "app_secret": "real_secret"},
                },
            }
        },
    }
    cfg = loader._parse_config(raw)
    assert "bot1" in cfg.channels.feishu.accounts
    assert cfg.channels.feishu.accounts["bot1"].app_id == "cli_real"
    assert "cli_ignored" not in [a.app_id for a in cfg.channels.feishu.accounts.values()]


def test_mcp_config_is_parsed_and_serialized():
    loader = ConfigLoader(config_path=None)
    raw = {
        "mcp": {
            "enabled": True,
            "servers": {
                "factory": {
                    "name": "factory",
                    "transport": "sse",
                    "url": "http://127.0.0.1:18081/sse",
                    "enabled": True,
                    "timeout_ms": 30000,
                    "risk_level": "low",
                    "tenant_scope": "tenant",
                    "requires_confirmation": False,
                }
            },
        }
    }

    cfg = loader._parse_config(raw)
    assert cfg.mcp.enabled is True
    assert "factory" in cfg.mcp.servers
    assert cfg.mcp.servers["factory"].url == "http://127.0.0.1:18081/sse"

    out = loader._serialize_config(cfg)
    assert out["mcp"]["enabled"] is True
    assert out["mcp"]["servers"]["factory"]["url"] == "http://127.0.0.1:18081/sse"
