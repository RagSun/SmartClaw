"""Lightweight static checks for skill directories."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from smartclaw.skills.types import SkillEntry


SUSPICIOUS_PATTERNS = {
    "curl_pipe_shell": "curl/wget pipe to shell",
    "rm_rf": "destructive rm -rf",
    "powershell_encoded": "PowerShell encoded command",
    "secret_literal": "possible hard-coded secret",
    "external_download": "external download command",
}


def _pattern_findings(text: str) -> list[str]:
    lower = text.lower()
    findings: list[str] = []
    if ("curl " in lower or "wget " in lower) and ("| sh" in lower or "| bash" in lower):
        findings.append(SUSPICIOUS_PATTERNS["curl_pipe_shell"])
    if "rm -rf /" in lower or "rd /s /q c:" in lower:
        findings.append(SUSPICIOUS_PATTERNS["rm_rf"])
    if "-encodedcommand" in lower or "frombase64string" in lower:
        findings.append(SUSPICIOUS_PATTERNS["powershell_encoded"])
    if "api_key=" in lower or "secret_key=" in lower or "access_key_secret" in lower:
        findings.append(SUSPICIOUS_PATTERNS["secret_literal"])
    if "curl " in lower or "wget " in lower or "invoke-webrequest" in lower:
        findings.append(SUSPICIOUS_PATTERNS["external_download"])
    return sorted(set(findings))


def scan_skill_entry(skill: SkillEntry, *, max_file_bytes: int = 256_000) -> list[str]:
    """Return human-readable security findings for a skill."""
    findings: list[str] = []
    base = Path(skill.base_dir).resolve()
    skill_md = Path(skill.skill_md_path).resolve()
    try:
        skill_md.relative_to(base)
    except ValueError:
        findings.append("SKILL.md escapes skill directory")

    if skill_md.is_symlink():
        findings.append("SKILL.md is symlink")

    files_to_scan = [skill_md]
    for suffix in ("*.py", "*.js", "*.ts", "*.sh", "*.ps1", "*.bat"):
        files_to_scan.extend(base.glob(suffix))

    for path in sorted(set(files_to_scan), key=lambda p: str(p).lower()):
        try:
            resolved = path.resolve()
            resolved.relative_to(base)
        except Exception:
            findings.append(f"{path.name}: file escapes skill directory")
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                findings.append(f"{path.name}: file too large to scan")
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            findings.append(f"{path.name}: read failed: {e}")
            continue
        findings.extend(f"{path.name}: {item}" for item in _pattern_findings(text))
    return sorted(set(findings))


def scan_skill(entry: SkillEntry, config: Any = None) -> dict[str, Any]:
    """Backward-compatible lifecycle scanner result."""
    limits = getattr(getattr(getattr(config, "skills", None), "limits", None), "max_skill_file_bytes", 256000)
    findings = scan_skill_entry(entry, max_file_bytes=int(limits))
    allowlist = set(getattr(getattr(config, "skills", None), "security_allowlist_skill_keys", []) or [])
    skill_key = entry.metadata.skill_key or entry.name
    blocked = bool(findings) and skill_key not in allowlist
    return {
        "skill_key": skill_key,
        "blocked": blocked,
        "findings": [{"severity": "warn", "reason": item} for item in findings],
    }


__all__ = ["scan_skill", "scan_skill_entry"]
