"""配置路径与租户 LLM 解析。"""

from pathlib import Path

from smartclaw.config.loader import ConfigLoader, tenant_llm_config_as_merge_dict
from smartclaw.llm.base import merge_agent_llm_with_global, normalize_agent_llm_dict
from smartclaw.paths import get_config_search_paths


def test_config_search_paths_prefers_user_config_over_repo(tmp_path: Path, monkeypatch) -> None:
    user_cfg = tmp_path / "user" / "config" / "config.toml"
    user_cfg.parent.mkdir(parents=True)
    user_cfg.write_text('[llm]\nmodel = "from-user"\n', encoding="utf-8")

    repo_cfg = tmp_path / "repo" / "config" / "config.toml"
    repo_cfg.parent.mkdir(parents=True)
    repo_cfg.write_text('[llm]\nmodel = "from-repo"\n', encoding="utf-8")

    monkeypatch.setattr(
        "smartclaw.paths.get_config_search_paths",
        lambda: [tmp_path / "missing-opt", user_cfg, repo_cfg],
    )

    loader = ConfigLoader()
    assert loader._find_config_file() == user_cfg
    cfg = loader.load()
    assert cfg.llm.model_name == "from-user"


def test_tenant_llm_overrides_global_in_merge() -> None:
    raw = {
        "llm": {
            "provider": "openai",
            "model": "global-model",
            "base_url": "https://global.example/v1",
            "api_key": "global-key",
        },
        "tenants": {
            "dept_a": {
                "enabled": True,
                "llm": {
                    "model": "tenant-model",
                    "base_url": "https://tenant.example/v1",
                },
            },
        },
    }
    cfg = ConfigLoader()._parse_config(raw)
    assert "dept_a" in cfg.tenants
    assert cfg.tenants["dept_a"].llm.model_name == "tenant-model"

    merged_default = tenant_llm_config_as_merge_dict(cfg, "default")
    assert merged_default["model_name"] == "global-model"

    merged_tenant = tenant_llm_config_as_merge_dict(cfg, "dept_a")
    assert merged_tenant["model_name"] == "tenant-model"
    assert merged_tenant["base_url"] == "https://tenant.example/v1"
    assert merged_tenant["api_key"] == "global-key"

    agent_blob = normalize_agent_llm_dict(
        merge_agent_llm_with_global({"model_name": "agent-model"}, merged_tenant)
    )
    assert agent_blob["model_name"] == "agent-model"


def test_get_config_search_paths_includes_repo_and_user_entries() -> None:
    paths = get_config_search_paths()
    assert any(p.name == "config.toml" for p in paths)
    assert any(".smartclaw" in str(p) for p in paths)
