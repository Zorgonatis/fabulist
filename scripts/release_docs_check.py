#!/usr/bin/env python
"""The release gate's documentation half: one line per check, any FAIL exits 1.

A release cut today produces a correct changelog and a wrong documentation set:
the changelog indexes, it does not reconcile. This script is the mechanical
reconciliation, and nothing more — one function per check, each printing exactly
one ``PASS`` / ``FAIL`` / ``SKIP`` line naming the file and line it looked at.
No colours, no JSON, no config file, no new surface.

Usage::

    uv run python scripts/release_docs_check.py 0.0.7

The argument is the version being cut, WITHOUT a leading ``v`` (a missing or
malformed argument is a usage error, exit 2). Checks run in order M1..M7 and the
exit status is 1 if any of them failed; ``SKIP`` is not a failure.

Checks:

* **M1** — ``pyproject.toml``, ``frontend/package.json``, the root package in
  ``uv.lock`` and both ``version`` fields of ``frontend/package-lock.json`` (the
  top-level one and ``packages[""].version``) all name the requested version.
  All of them: at tag ``v0.0.5`` the manifests said 0.0.5 while ``uv.lock`` still
  pinned 0.0.4, and nothing noticed. The four points move together — a release
  commit runs ``npm version`` (which rewrites both ``package-lock.json`` fields)
  and ``uv lock`` — so a gate that skipped one would ship that miss again.
* **M2** — ``CHANGELOG.md`` has ``## [<version>] - <YYYY-MM-DD>`` and every
  bullet in it carries the issue reference ``(#NN)`` (AGENTS.md, Releases).
* **M3** — every ``(#NN)`` in the release's commit subjects (``git log
  <previous-tag>..HEAD``) is referenced from the changelog section it reads: the
  ``[<version>]`` section when it exists, otherwise ``[Unreleased]`` — the cut
  writes the version section, so pre-cut the index is still ``[Unreleased]``.
  The line names which section it read; with neither present it fails, rather
  than listing issues as missing from a section that does not exist.
* **M4** — every ``FABULIST_*`` name the compose files pass is either in
  ``.env.example`` or justified plumbing (:data:`COMPOSE_PLUMBING`).
* **M5** — the copy register's retired words do not appear as user-visible text
  (:data:`RETIRED_WORDS`); identifiers keep their names on purpose.
* **M6** — the two existing generated-artifact checks report no drift. This one
  invokes them; it never re-implements them.
* **M7** — every relative link in the docs resolves, and every ``#anchor``
  matches a real heading in its target (GitHub slug rules).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
PACKAGE_JSON = REPO_ROOT / "frontend" / "package.json"
PACKAGE_LOCK = REPO_ROOT / "frontend" / "package-lock.json"
UV_LOCK = REPO_ROOT / "uv.lock"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
COMPOSE_FILES = tuple(sorted(REPO_ROOT.glob("docker-compose*.yml")))
AGENT_MAP_SCRIPT = REPO_ROOT / "scripts" / "generate_agent_map.py"
LIBRARY_SCRIPT = REPO_ROOT / "frontend" / "scripts" / "generate-library-spec.mjs"

#: Compose variables that ARE internal plumbing: the stack sets them, a user
#: never does. The allow-list is deliberately two entries — every name here must
#: be justifiable in one line.
#:
#: ``FABULIST_ENV_FILE`` is NOT here on purpose: compose never passes it (only the
#: Dockerfile and ``scripts/serve.py`` set it), so it is out of this check's
#: scope. If a compose file ever interpolates it, the check catches it as
#: undocumented — which is the point.
COMPOSE_PLUMBING: dict[str, str] = {
    "FABULIST_HOST_ALIAS": "compose sets it, so a host-local model URL is dialled on the host",
    "FABULIST_SIDECAR_URL": "compose sets it to a service name; no user names a container URL",
}

#: Copy-register retirements (docs/UI-COPY.md). The CODE keeps the identifiers —
#: kind id ``queries``, ``DecisionQueriesPane.svelte``, ``decisionQueries.ts``,
#: ``/api/decision-queries``, ``src/fabulist/decision_queries.py``, the log anchor
#: ``__decision_queries__`` — so only a standalone, case-sensitive occurrence of
#: the retired word is a hit: camelCase and lowercase identifiers never match.
@dataclass(frozen=True)
class RetiredWord:
    word: str
    replacement: str
    issue: str
    paths: tuple[str, ...]


RETIRED_WORDS: tuple[RetiredWord, ...] = (
    RetiredWord("Queries", "Repair triggers", "99", ("frontend/src",)),
)

#: Historical records quote both words by design and are never scanned.
HISTORICAL_PATHS = ("CHANGELOG.md", "docs/UI-COPY.md", "docs/decisions", "docs/archive")

_PYPROJECT_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"')
_PACKAGE_VERSION = re.compile(r'^\s*"version"\s*:\s*"([^"]+)"')
_JSON_KEY = re.compile(r'^(\s*)"((?:[^"\\]|\\.)*)"\s*:')
_ROOT_PACKAGE_NAME = 'name = "fabulist"'
_SECTION_HEADER = re.compile(r"^## \[([^\]]+)\] - (\d{4}-\d{2}-\d{2})\s*$")
_UNRELEASED_HEADER = re.compile(r"^## \[Unreleased\]\s*$")
#: The section M3 falls back to when the version's own section does not exist yet.
UNRELEASED = "Unreleased"
_ISSUE_REF = re.compile(r"\(#(\d+)\)")
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_INTERPOLATION = re.compile(r"\$\{(FABULIST_[A-Z0-9_]+)")
_ENVIRONMENT_BLOCK = re.compile(r"^(\s*)environment:\s*$")
_ENVIRONMENT_KEY = re.compile(r"^\s+(FABULIST_[A-Z0-9_]+):")
_ENV_EXAMPLE_NAME = re.compile(r"^\s*#*\s*(FABULIST_[A-Z0-9_]+)\s*=")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")

GitRunner = Callable[..., tuple[int, str]]


def _emit(status: str, check: str, detail: str) -> bool:
    """Print one result line; callers return this, so PASS is the only truthy one."""
    print(f"{status} {check} {detail}")
    return status == "PASS"


class _Skip(Exception):
    """A check cannot run on this machine; the reason becomes its SKIP line."""


def _git(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# M1 versions
# ---------------------------------------------------------------------------


def _first_value(path: Path, pattern: re.Pattern[str]) -> tuple[str | None, int | None]:
    """The first captured value in ``path`` and its 1-based line number."""
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = pattern.search(line)
        if match:
            return match.group(1), number
    return None, None


def _lock_version(path: Path) -> tuple[str | None, int | None]:
    """The root package's version, read from its own ``[[package]]`` block."""
    in_root = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line == _ROOT_PACKAGE_NAME:
            in_root = True
        elif line == "[[package]]":
            in_root = False
        elif in_root and line.startswith("version = "):
            return line.split('"')[1], number
    return None, None


def _json_member_lines(path: Path) -> dict[tuple[str, ...], int]:
    """The 1-based line of each object member in ``path``, keyed by its key path.

    ``json`` reports values, not positions, so the positions are scanned: the
    indentation of a key line is its nesting depth, which is all that is needed
    to tell the root ``"version"`` from ``packages[""].version`` from the
    ``"version"`` of every installed package.
    """
    found: dict[tuple[str, ...], int] = {}
    opened: list[tuple[int, str]] = []  # (indent, key) of every object still open
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        while opened and opened[-1][0] >= indent:
            opened.pop()
        match = _JSON_KEY.match(line)
        if match is None:
            continue
        key = match.group(2)
        found.setdefault(tuple(k for _, k in opened) + (key,), number)
        if stripped.endswith(("{", "[")):
            opened.append((indent, key))
    return found


#: npm writes the version twice per lockfile, so a FAIL names the field: a lock
#: that is simply stale is a different fault from one that contradicts itself.
LOCK_TOP_FIELD = "top-level version"
LOCK_PACKAGES_FIELD = 'packages[""].version'


def _package_lock_fields(path: Path) -> dict[str, tuple[str | None, int | None]]:
    """Both ``version`` fields of an npm lockfile: ``{field: (value, line)}``."""
    document = json.loads(path.read_text(encoding="utf-8"))
    lines = _json_member_lines(path)
    root = (document.get("packages") or {}).get("") or {}
    return {
        LOCK_TOP_FIELD: (document.get("version"), lines.get(("version",))),
        LOCK_PACKAGES_FIELD: (
            root.get("version"),
            lines.get(("packages", "", "version")),
        ),
    }


def check_version_agreement(
    version: str,
    *,
    pyproject: Path = PYPROJECT,
    package_json: Path = PACKAGE_JSON,
    uv_lock: Path = UV_LOCK,
    package_lock: Path = PACKAGE_LOCK,
) -> bool:
    """M1: the four version points a release cuts must all say ``version``."""
    lock_name = "frontend/package-lock.json"
    sites = (
        ("pyproject.toml", _first_value(pyproject, _PYPROJECT_VERSION)),
        ("frontend/package.json", _first_value(package_json, _PACKAGE_VERSION)),
        ("uv.lock", _lock_version(uv_lock)),
    )
    lock = _package_lock_fields(package_lock)
    top_value, top_line = lock[LOCK_TOP_FIELD]
    installed_value, installed_line = lock[LOCK_PACKAGES_FIELD]
    stale = [
        f"{name}:{line} = {found}"
        for name, (found, line) in sites
        if found != version
    ]
    if top_value != installed_value:
        return _emit(
            "FAIL",
            "M1",
            f"want {version}; {lock_name} disagrees with itself: "
            f"{LOCK_TOP_FIELD} = {top_value} at {lock_name}:{top_line}, "
            f"{LOCK_PACKAGES_FIELD} = {installed_value} at {lock_name}:{installed_line}"
            + (f"; also stale: {', '.join(stale)}" if stale else ""),
        )
    stale += [
        f"{lock_name}:{line} ({field}) = {value}"
        for field, (value, line) in lock.items()
        if value != version
    ]
    if stale:
        return _emit("FAIL", "M1", f"want {version}; stale: " + ", ".join(stale))
    lines = [f"{name}:{line}" for name, (_, line) in sites]
    lines += [f"{lock_name}:{line}" for _, line in lock.values()]
    return _emit("PASS", "M1", f"{version} at " + ", ".join(lines))


# ---------------------------------------------------------------------------
# M2 changelog shape / M3 issue coverage
# ---------------------------------------------------------------------------


def _section_body(lines: list[str], start: int) -> list[tuple[int, str]]:
    """The numbered body lines below the header on line ``start`` (1-based)."""
    body: list[tuple[int, str]] = []
    for number in range(start + 1, len(lines) + 1):
        line = lines[number - 1]
        if line.startswith("## "):
            break
        body.append((number, line))
    return body


def _changelog_section(text: str, version: str) -> tuple[int, list[tuple[int, str]]]:
    """``(header line, body lines)`` of the ``## [version]`` section; ``0`` if absent."""
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        match = _SECTION_HEADER.match(line)
        if match and match.group(1) == version:
            return number, _section_body(lines, number)
    return 0, []


def _unreleased_section(text: str) -> tuple[int, list[tuple[int, str]]]:
    """The same for ``## [Unreleased]``, whose header carries no date part."""
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        if _UNRELEASED_HEADER.match(line):
            return number, _section_body(lines, number)
    return 0, []


def _coverage_section(
    changelog: Path, version: str
) -> tuple[str, list[tuple[int, str]]] | None:
    """The section M3 reconciles against: ``[<version>]``, else ``[Unreleased]``.

    Returns the section name and its body, or ``None`` when the changelog holds
    neither. Resolving the section is independent of M2's verdict.
    """
    text = changelog.read_text(encoding="utf-8")
    start, body = _changelog_section(text, version)
    if start:
        return version, body
    start, body = _unreleased_section(text)
    if start:
        return UNRELEASED, body
    return None


def check_changelog_shape(version: str, *, changelog: Path = CHANGELOG) -> bool:
    """M2: a dated section exists and every bullet in it cites a closed issue."""
    start, body = _changelog_section(changelog.read_text(encoding="utf-8"), version)
    if start == 0:
        return _emit(
            "FAIL",
            "M2",
            f"{changelog.name} has no section '## [{version}] - YYYY-MM-DD'",
        )
    bullets = [(number, line) for number, line in body if line.lstrip().startswith("- ")]
    unreferenced = [(number, line) for number, line in bullets if not _ISSUE_REF.search(line)]
    if unreferenced:
        number, line = unreferenced[0]
        return _emit(
            "FAIL",
            "M2",
            f"{changelog.name}:{number} bullet carries no (#NN): {line.strip()}",
        )
    return _emit(
        "PASS",
        "M2",
        f"{changelog.name}:{start} section ({len(bullets)} bullets, every one cites an issue)",
    )


def _previous_tag(version: str, git: GitRunner) -> str:
    code, out = git("tag", "--list", "v*", "--sort=-v:refname")
    if code != 0:
        raise _Skip(f"git tag failed: {out.strip().splitlines()[0] if out.strip() else code}")
    wanted = tuple(int(part) for part in version.split("."))
    for line in out.splitlines():
        match = _TAG.match(line.strip())
        if match and tuple(int(part) for part in match.groups()) < wanted:
            return line.strip()
    raise _Skip(f"no v* tag below {version}")


def check_issue_coverage(
    version: str, *, changelog: Path = CHANGELOG, git: GitRunner = _git
) -> bool:
    """M3: the section it reads indexes every issue the release's commits cite.

    Pre-cut there is no ``[<version>]`` section to read — the cut writes it — so
    the entries live under ``[Unreleased]`` and the line says which it read. The
    check never reports issues as missing from a section that does not exist.
    """
    section = _coverage_section(changelog, version)
    if section is None:
        return _emit(
            "FAIL",
            "M3",
            f"no changelog section to attribute: {changelog.name} has neither "
            f"'## [{version}]' nor '## [{UNRELEASED}]'",
        )
    name, body = section
    check = f"M3 (reading [{name}]):"
    try:
        tag = _previous_tag(version, git)
        code, out = git("log", f"{tag}..HEAD", "--pretty=format:%s")
        if code != 0:
            raise _Skip(f"git log {tag}..HEAD failed: {out.strip().splitlines()[0]}")
    except _Skip as reason:
        return _emit("SKIP", check, f"{reason}; issue coverage unverified")
    committed = {int(number) for number in _ISSUE_REF.findall(out)}
    referenced = {int(number) for number in _ISSUE_REF.findall("\n".join(line for _, line in body))}
    missing = sorted(committed - referenced)
    if missing:
        return _emit(
            "FAIL",
            check,
            "missing "
            + ", ".join(f"#{number}" for number in missing)
            + f" — cited by {tag}..HEAD, absent from the [{name}] section of {changelog.name}",
        )
    return _emit(
        "PASS",
        check,
        f"every issue in {tag}..HEAD is accounted for "
        f"({len(committed)} cited, all in the [{name}] section of {changelog.name})",
    )


# ---------------------------------------------------------------------------
# M4 environment parity
# ---------------------------------------------------------------------------


def _compose_names(path: Path) -> dict[str, tuple[str, int]]:
    """Every ``FABULIST_*`` name the file passes: interpolations + ``environment:`` keys."""
    names: dict[str, tuple[str, int]] = {}
    block_indent: int | None = None
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for match in _INTERPOLATION.finditer(line):
            names.setdefault(match.group(1), (path.name, number))
        if block_indent is None:
            block = _ENVIRONMENT_BLOCK.match(line)
            if block:
                block_indent = len(block.group(1))
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= block_indent:
            block_indent = None
            continue
        key = _ENVIRONMENT_KEY.match(line)
        if key:
            names.setdefault(key.group(1), (path.name, number))
    return names


def check_environment_parity(
    *,
    compose_files: tuple[Path, ...] = COMPOSE_FILES,
    env_example: Path = ENV_EXAMPLE,
    plumbing: dict[str, str] = COMPOSE_PLUMBING,
) -> bool:
    """M4: a compose-passed ``FABULIST_*`` knob is documented or justified plumbing."""
    names: dict[str, tuple[str, int]] = {}
    for compose in compose_files:
        for name, origin in _compose_names(compose).items():
            names.setdefault(name, origin)
    documented = {
        match.group(1)
        for line in env_example.read_text(encoding="utf-8").splitlines()
        if (match := _ENV_EXAMPLE_NAME.match(line))
    }
    missing = [name for name in sorted(names) if name not in documented and name not in plumbing]
    if missing:
        detail = ", ".join(f"{names[name][0]}:{names[name][1]} {name}" for name in missing)
        return _emit(
            "FAIL",
            "M4",
            f"{len(names)} compose name(s); not in {env_example.name} nor plumbing: {detail}",
        )
    return _emit(
        "PASS",
        "M4",
        f"{len(names)} compose name(s) documented or justified plumbing",
    )


# ---------------------------------------------------------------------------
# M5 retired words
# ---------------------------------------------------------------------------


def _is_historical(relative: Path) -> bool:
    return any(
        str(relative) == exempt or str(relative).startswith(exempt + "/")
        for exempt in HISTORICAL_PATHS
    )


def check_retired_words(*, root: Path = REPO_ROOT) -> bool:
    """M5: a retired word appears only as an identifier, never as user-visible text."""
    scanned = 0
    findings: list[str] = []
    for retired in RETIRED_WORDS:
        pattern = re.compile(rf"\b{re.escape(retired.word)}\b")
        for entry in retired.paths:
            for path in sorted((root / entry).rglob("*")):
                relative = path.relative_to(root)
                if not path.is_file() or _is_historical(relative):
                    continue
                scanned += 1
                for number, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    if pattern.search(line):
                        findings.append(
                            f"{relative}:{number} says '{retired.word}' "
                            f"(#{retired.issue}: say '{retired.replacement}')"
                        )
    if findings:
        return _emit("FAIL", "M5", "; ".join(findings))
    summary = "; ".join(
        f"'{r.word}' -> '{r.replacement}' (#{r.issue})" for r in RETIRED_WORDS
    )
    return _emit("PASS", "M5", f"{summary}: absent from {scanned} file(s) scanned")


# ---------------------------------------------------------------------------
# M6 generated artifacts (delegated, never re-implemented)
# ---------------------------------------------------------------------------


def check_generated_artifacts(*, root: Path = REPO_ROOT) -> bool:
    """M6: propagate the exit status of the two existing generator ``--check`` runs."""
    agent_map = root / "scripts" / "generate_agent_map.py"
    library = root / "frontend" / "scripts" / "generate-library-spec.mjs"
    if "--check" not in agent_map.read_text(encoding="utf-8"):
        return _emit("SKIP", "M6", f"{agent_map.name} has no --check flag; nothing to propagate")
    skipped: list[str] = []
    steps: list[tuple[str, list[str], Path]] = []
    # The preconditions the repo's own artifact test states: no node, or no
    # lang-core install, means the check cannot run here — not that it drifted.
    if shutil.which("node") is None:
        skipped.append("library spec (node is not installed)")
    elif not (root / "frontend" / "node_modules" / "@openuidev" / "lang-core").is_dir():
        skipped.append("library spec (frontend/node_modules/@openuidev/lang-core is not installed)")
    else:
        steps.append(("library spec", ["node", str(library), "--check"], root / "frontend"))
    if shutil.which("uv") is None:
        skipped.append("agent map (uv is not installed)")
    else:
        steps.append(
            ("agent map", ["uv", "run", "python", str(agent_map), "--check"], root)
        )
    problems: list[str] = []
    for label, command, cwd in steps:
        proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            output = (proc.stdout + proc.stderr).splitlines()
            first = next((line.strip() for line in output if line.strip()), "")
            problems.append(f"{label} exited {proc.returncode}: {first}")
    if problems:
        return _emit("FAIL", "M6", "; ".join(problems))
    if skipped:
        ran = [label for label, _, _ in steps]
        detail = "; ".join(skipped)
        if ran:
            detail += f"; {' and '.join(ran)}: no drift"
        return _emit("SKIP", "M6", detail)
    return _emit("PASS", "M6", "library spec and agent map both report no drift")


# ---------------------------------------------------------------------------
# M7 links and anchors
# ---------------------------------------------------------------------------


def _slug(heading: str) -> str:
    """GitHub's heading slug: spaces to ``-``, other punctuation dropped, lowercased.

    Underscores and hyphens survive; ``## 1. Log and state`` becomes
    ``1-log-and-state``, which is the anchor the README links to.
    """
    text = re.sub(r"[^\w\- ]", "", heading.strip())
    return text.lower().replace(" ", "-")


def _prose_lines(path: Path) -> Iterator[tuple[int, str]]:
    """Lines outside fenced code blocks — a fence holds neither headings nor links."""
    fenced = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            yield number, line


def _anchors(path: Path) -> set[str]:
    """Every anchor GitHub would generate, including the ``-1`` duplicate suffixes."""
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    for _, line in _prose_lines(path):
        match = _HEADING.match(line)
        if not match:
            continue
        base = _slug(match.group(2))
        count = seen.get(base, 0)
        seen[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


def check_links_and_anchors(*, root: Path = REPO_ROOT) -> bool:
    """M7: every relative doc link resolves and every ``#anchor`` names a heading."""
    docs = [root / "README.md", root / "AGENTS.md"]
    docs.extend(sorted((root / "docs").rglob("*.md")))
    docs = [doc for doc in docs if doc.is_file()]
    checked = 0
    problems: list[str] = []
    for doc in docs:
        for number, line in _prose_lines(doc):
            for match in _LINK.finditer(line):
                target = match.group(1)
                if target.startswith(("http://", "https://", "mailto:")) or target == "#":
                    continue
                relative, _, anchor = target.partition("#")
                where = f"{doc.relative_to(root)}:{number}"
                if relative.startswith("/"):
                    resolved = root / relative.lstrip("/")
                elif relative:
                    resolved = doc.parent / relative
                else:
                    resolved = doc
                if not resolved.exists():
                    problems.append(f"{where} -> {target} (no such file)")
                    continue
                checked += 1
                if anchor and resolved.suffix == ".md" and anchor not in _anchors(resolved):
                    problems.append(f"{where} -> {target} (no such heading)")
    if problems:
        return _emit("FAIL", "M7", "; ".join(problems))
    return _emit(
        "PASS", "M7", f"{checked} link(s) across {len(docs)} doc(s) resolve, anchors included"
    )


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

USAGE = (
    "usage: uv run python scripts/release_docs_check.py <version>   "
    "e.g. 0.0.7 (no leading v)"
)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1 or not re.fullmatch(r"\d+\.\d+\.\d+", arguments[0]):
        print(USAGE, file=sys.stderr)
        return 2
    version = arguments[0]
    results = [
        check_version_agreement(version),
        check_changelog_shape(version),
        check_issue_coverage(version),
        check_environment_parity(),
        check_retired_words(),
        check_generated_artifacts(),
        check_links_and_anchors(),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
