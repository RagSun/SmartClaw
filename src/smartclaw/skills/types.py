"""
Skills domain types.
"""

from dataclasses import dataclass, field


@dataclass
class SkillMetadata:
    """Metadata parsed from SKILL.md frontmatter."""

    skill_key: str | None = None
    version: str = "0.1.0"
    changelog: str = ""
    primary_env: str | None = None
    owner: str = ""
    reviewer: str = ""
    sla: str = ""
    risk_level: str = "info"
    test_status: str = "unknown"
    install_method: str = "none"
    install_spec: str = ""
    install_command: str = ""
    allowed_envs: list[str] = field(default_factory=list)
    requires_bins: list[str] = field(default_factory=list)
    requires_env: list[str] = field(default_factory=list)
    always: bool = False


@dataclass
class SkillEntry:
    """A discovered skill."""

    name: str
    description: str
    source: str
    skill_md_path: str
    base_dir: str
    metadata: SkillMetadata = field(default_factory=SkillMetadata)


@dataclass
class SkillStatus:
    """Skill runtime eligibility status."""

    skill_key: str
    name: str
    source: str
    description: str
    version: str
    owner: str
    reviewer: str
    risk_level: str
    test_status: str
    enabled: bool
    eligible: bool
    tenant_id: str = ""
    tenant_allowed: bool = True
    missing_bins: list[str] = field(default_factory=list)
    missing_env: list[str] = field(default_factory=list)
    security_findings: list[str] = field(default_factory=list)
    primary_env: str | None = None
    blocked_reason: str | None = None

