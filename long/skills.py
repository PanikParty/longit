"""Stored skills: named instruction bundles that are matched against the user's prompt and injected for that turn.

A skill is a markdown file with a small frontmatter header:

    ---
    name: undo-last-commit
    description: Undo the most recent commit but keep the changes
    triggers: [undo last commit, uncommit, "re:\\bundo (the )?last commit\\b"]
    ---
    Instructions for the model...

Skills are only *instructions*. They cannot grant permissions: every command they lead to still goes through
the safety classifier, the mode and the approval prompts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BUILTIN_DIR = Path(__file__).parent / "builtin_skills"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_FRONT = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n?(.*)$", re.S)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    triggers: tuple[str, ...]
    body: str
    path: Path
    source: str  # "builtin" | "user" | "project"
    action: bool = False  # must end by changing something; replies that only talk are nudged to keep going


def _split_inline_list(inner: str) -> list[str]:
    out = []
    for dq, sq, bare in re.findall(r'\s*(?:"([^"]*)"|\'([^\']*)\'|([^,]+))', inner):
        item = (dq or sq or bare).strip()
        if item:
            out.append(item)
    return out


def _parse_meta(block: str) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    key: str | None = None
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and key is not None and isinstance(meta.get(key), list):
            meta[key].append(line.lstrip()[2:].strip().strip("\"'"))
            continue
        k, sep, v = line.partition(":")
        if not sep:
            continue
        key, v = k.strip().lower(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            meta[key] = _split_inline_list(v[1:-1])
        elif v == "":
            meta[key] = []
        else:
            meta[key] = v.strip("\"'")
    return meta


def parse_skill(text: str, path: Path, source: str) -> Skill:
    """Raises ValueError with a human-readable reason if the file is not a usable skill."""
    m = _FRONT.match(text.replace("\r\n", "\n"))
    if not m:
        raise ValueError("missing '---' frontmatter header")
    meta, body = _parse_meta(m.group(1)), m.group(2).strip()
    name = str(meta.get("name") or path.stem).strip().lower()
    if not _NAME.match(name):
        raise ValueError(f"invalid skill name '{name}' (use lowercase letters, digits, - and _)")
    if not body:
        raise ValueError("skill has no instructions")
    trig = meta.get("triggers", [])
    triggers = tuple(t for t in ([trig] if isinstance(trig, str) else trig) if t)
    for t in triggers:
        if t.startswith("re:"):
            try:
                re.compile(t[3:])
            except re.error as e:
                raise ValueError(f"bad regex trigger {t!r}: {e}") from e
    desc = meta.get("description", "")
    action = str(meta.get("action", "")).strip().lower() in ("true", "yes", "1")
    return Skill(name, desc if isinstance(desc, str) else "", triggers, body, path, source, action)


def _compile(trigger: str) -> re.Pattern[str]:
    if trigger.startswith("re:"):
        return re.compile(trigger[3:], re.I)
    words = r"\s+".join(re.escape(w) for w in trigger.split())
    return re.compile(rf"(?<!\w){words}(?!\w)", re.I)


def load_dir(directory: Path, source: str) -> tuple[list[Skill], list[str]]:
    skills: list[Skill] = []
    warnings: list[str] = []
    if not directory.is_dir():
        return skills, warnings
    for f in sorted(directory.glob("*.md")):
        try:
            skills.append(parse_skill(f.read_text(encoding="utf-8"), f, source))
        except (ValueError, OSError, UnicodeDecodeError) as e:
            warnings.append(f"skipped {f}: {e}")
    return skills, warnings


@dataclass
class SkillSet:
    user_dir: Path | None = None
    builtin_dir: Path = BUILTIN_DIR
    warnings: list[str] = field(default_factory=list)
    _layers: dict[str, list[Skill]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._layers = {"builtin": [], "user": [], "project": []}
        self._load("builtin", self.builtin_dir)
        if self.user_dir:
            self._load("user", self.user_dir)

    def _load(self, source: str, directory: Path) -> None:
        skills, warns = load_dir(directory, source)
        self._layers[source] = skills
        self.warnings = [w for w in self.warnings if not w.startswith(f"[{source}] ")] + [f"[{source}] {w}" for w in warns]

    def set_project(self, root: Path | None) -> None:
        """(Re)load skills from <root>/.long/skills; pass None to clear."""
        self._load("project", root / ".long" / "skills") if root else self._layers.update(project=[])

    def all(self) -> list[Skill]:
        merged: dict[str, Skill] = {}
        for source in ("builtin", "user", "project"):  # later layers override earlier ones
            for s in self._layers[source]:
                merged[s.name] = s
        return sorted(merged.values(), key=lambda s: s.name)

    def get(self, name: str) -> Skill | None:
        return next((s for s in self.all() if s.name == name.lower()), None)

    def match(self, prompt: str) -> tuple[Skill, str] | None:
        """Best trigger match: the longest matched trigger text wins. Returns (skill, trigger) or None."""
        best: tuple[int, Skill, str] | None = None
        for s in self.all():
            for t in s.triggers:
                m = _compile(t).search(prompt)
                if m and (best is None or len(m.group(0)) > best[0]):
                    best = (len(m.group(0)), s, t)
        return (best[1], best[2]) if best else None


MODEL_SELECT_SYSTEM = """You route a user's request to a stored skill. Reply with ONLY the exact name of the one skill
that clearly fits the request, or the word none. Do not explain."""


def model_select_messages(skills: list[Skill], prompt: str) -> list[dict[str, str]]:
    listing = "\n".join(f"- {s.name}: {s.description or '(no description)'}" for s in skills)
    return [{"role": "system", "content": MODEL_SELECT_SYSTEM},
            {"role": "user", "content": f"Skills:\n{listing}\n\nRequest: {prompt}\n\nSkill name or none:"}]


def pick_from_reply(skills: list[Skill], reply: str) -> Skill | None:
    word = re.sub(r"[^a-z0-9_-]", "", reply.strip().split()[0].lower()) if reply.strip() else ""
    return next((s for s in skills if s.name == word), None)


def injection(skill: Skill) -> str:
    return (f"\n\n## Active skill: {skill.name}\n"
            "The user's request matches this stored skill. Follow it for this request. It adds guidance only: the safety "
            "rules, the current mode and the user's approvals above still apply, and it cannot grant extra permissions.\n\n"
            f"{skill.body}")
