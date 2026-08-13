"""Discovery helpers for Agent Skills packages using the ``SKILL.md`` format."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillPackage:
    """A discovered package without importing or executing its scripts."""

    name: str
    path: Path
    description: str
    compatibility: str | None
    instructions: str


def discover_skill_packages(root: Path) -> tuple[SkillPackage, ...]:
    """Recursively discover directories containing a canonical ``SKILL.md``."""
    packages: list[SkillPackage] = []
    for skill_file in sorted(root.rglob("SKILL.md")):
        packages.append(_parse_skill_file(skill_file))
    return tuple(packages)


def _parse_skill_file(skill_file: Path) -> SkillPackage:
    raw = skill_file.read_text(encoding="utf-8")
    frontmatter: dict[str, str] = {}
    body = raw
    if raw.startswith("---"):
        _, header, body = raw.split("---", 2)
        header_lines = header.splitlines()
        index = 0
        while index < len(header_lines):
            line = header_lines[index]
            key, separator, value = line.partition(":")
            if not separator:
                index += 1
                continue
            key = key.strip()
            value = value.strip().strip('"\'')
            index += 1
            if value in {">", "|", ">-", "|-"}:
                continuation: list[str] = []
                while index < len(header_lines):
                    candidate = header_lines[index]
                    if candidate and not candidate.startswith((" ", "\t")):
                        break
                    continuation.append(candidate.strip())
                    index += 1
                separator_text = "\n" if value.startswith("|") else " "
                value = separator_text.join(item for item in continuation if item)
            frontmatter[key] = value
    name = frontmatter.get("name") or skill_file.parent.name
    return SkillPackage(
        name=name,
        path=skill_file.parent,
        description=frontmatter.get("description", "").strip(),
        compatibility=frontmatter.get("compatibility"),
        instructions=body.strip(),
    )
