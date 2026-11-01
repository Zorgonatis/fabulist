"""The release documentation gate (``scripts/release_docs_check.py``).

Every test builds its own tree under ``tmp_path`` — a fake changelog, compose
file, ``.env.example`` and docs tree — so no test reads the real repo state,
needs a git tag or touches the network. The git history behind M3 is a fake.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # scripts/ is a namespace package, not installed
    sys.path.insert(0, str(_ROOT))

from scripts import release_docs_check as gate  # noqa: E402


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _uv_lock(version: str) -> str:
    """A lockfile whose ROOT package (not the one before it) carries ``version``."""
    return (
        "version = 1\n\n"
        "[[package]]\n"
        'name = "anyio"\n'
        'version = "4.15.1"\n\n'
        "[[package]]\n"
        'name = "fabulist"\n'
        f'version = "{version}"\n'
        'source = { editable = "." }\n'
    )


def _package_lock(version: str, *, top: str | None = None, installed: str | None = None) -> str:
    """npm's lockfile: the version twice — top-level, then ``packages[""]``.

    The line numbers match the real file (3 and 9), and the installed packages
    below carry ``version`` keys of their own that a scan must not confuse with
    the root's.
    """
    top = version if top is None else top
    installed = version if installed is None else installed
    return (
        "{\n"
        '  "name": "fabulist-frontend",\n'
        f'  "version": "{top}",\n'
        '  "lockfileVersion": 3,\n'
        '  "requires": true,\n'
        '  "packages": {\n'
        '    "": {\n'
        '      "name": "fabulist-frontend",\n'
        f'      "version": "{installed}",\n'
        '      "dependencies": {\n'
        '        "dompurify": "^3.4.17"\n'
        "      }\n"
        "    },\n"
        '    "node_modules/dompurify": {\n'
        '      "version": "3.4.17",\n'
        '      "resolved": "https://registry.npmjs.org/dompurify/-/dompurify-3.4.17.tgz"\n'
        "    }\n"
        "  }\n"
        "}\n"
    )


def _versions(
    tmp_path: Path,
    version: str = "0.0.7",
    *,
    uv_lock: str | None = None,
    package_lock: dict[str, str] | None = None,
) -> dict:
    """The M1 points, as keyword arguments for the check."""
    return {
        "pyproject": _write(
            tmp_path / "pyproject.toml",
            f'[project]\nname = "fabulist"\nversion = "{version}"\n',
        ),
        "package_json": _write(
            tmp_path / "frontend" / "package.json",
            '{\n\t"name": "fabulist-frontend",\n\t"version": "%s"\n}\n' % version,
        ),
        "uv_lock": _write(tmp_path / "uv.lock", _uv_lock(uv_lock or version)),
        "package_lock": _write(
            tmp_path / "frontend" / "package-lock.json",
            _package_lock(version, **(package_lock or {})),
        ),
    }


def _changelog(tmp_path: Path, body: str) -> Path:
    return _write(tmp_path / "CHANGELOG.md", f"# Changelog\n\n{body}")


def _fake_git(*, tags: str, subjects: str):
    def run(*args: str) -> tuple[int, str]:
        return (0, tags) if args[0] == "tag" else (0, subjects)

    return run


# --- M1: four version points ----------------------------------------------------


def test_m1_passes_when_all_four_points_agree(capsys, tmp_path):
    assert gate.check_version_agreement("0.0.7", **_versions(tmp_path)) is True
    out = capsys.readouterr().out.strip()
    assert out.startswith("PASS M1")
    for point in (
        "pyproject.toml:3",
        "frontend/package.json:3",
        "uv.lock:9",
        "frontend/package-lock.json:3",
        "frontend/package-lock.json:9",
    ):
        assert point in out, point


def test_m1_names_a_stale_lockfile(capsys, tmp_path):
    """The release the owner remembers: manifests bumped, lockfile left behind."""
    points = _versions(tmp_path, uv_lock="0.0.6")
    assert gate.check_version_agreement("0.0.7", **points) is False
    out = capsys.readouterr().out.strip()
    assert out.startswith("FAIL M1")
    assert "uv.lock:9 = 0.0.6" in out and "pyproject.toml" not in out


def test_m1_names_a_stale_package_lock(capsys, tmp_path):
    """The v0.0.5/uv.lock failure, one third point over: the whole lock left behind."""
    points = _versions(tmp_path, package_lock={"top": "0.0.6", "installed": "0.0.6"})
    assert gate.check_version_agreement("0.0.7", **points) is False
    out = capsys.readouterr().out.strip()
    assert out.startswith("FAIL M1")
    assert "frontend/package-lock.json:3 (top-level version) = 0.0.6" in out
    assert "frontend/package-lock.json:9 (packages[\"\"].version) = 0.0.6" in out
    assert "disagrees with itself" not in out


def test_m1_says_when_the_package_lock_disagrees_with_itself(capsys, tmp_path):
    """One field bumped, the other missed: a contradiction, not a stale lock."""
    points = _versions(tmp_path, package_lock={"top": "0.0.6"})
    assert gate.check_version_agreement("0.0.7", **points) is False
    out = capsys.readouterr().out.strip()
    assert out.startswith("FAIL M1")
    assert "frontend/package-lock.json disagrees with itself" in out
    assert "top-level version = 0.0.6 at frontend/package-lock.json:3" in out
    assert 'packages[""].version = 0.0.7 at frontend/package-lock.json:9' in out
    # pyproject and ``uv.lock`` agree; the contradiction is the lock's own.
    assert "pyproject.toml" not in out and "uv.lock" not in out


# --- M2: changelog shape --------------------------------------------------------


def test_m2_accepts_a_dated_section_with_issue_references(capsys, tmp_path):
    changelog = _changelog(
        tmp_path,
        "## [0.0.7] - 2026-11-01\n\n### Fixed\n\n- A thing works again (#99)\n- Another (#100)\n",
    )
    assert gate.check_changelog_shape("0.0.7", changelog=changelog) is True
    assert capsys.readouterr().out.startswith("PASS M2 CHANGELOG.md:3")


def test_m2_rejects_a_bullet_without_an_issue_reference(capsys, tmp_path):
    changelog = _changelog(
        tmp_path, "## [0.0.7] - 2026-11-01\n\n- A thing works again (#99)\n- An unindexed change\n"
    )
    assert gate.check_changelog_shape("0.0.7", changelog=changelog) is False
    out = capsys.readouterr().out
    assert out.startswith("FAIL M2 CHANGELOG.md:6") and "(#NN)" in out
    undated = _changelog(tmp_path, "## [Unreleased]\n\n- A thing (#99)\n")
    assert gate.check_changelog_shape("0.0.7", changelog=undated) is False
    assert "no section '## [0.0.7] - YYYY-MM-DD'" in capsys.readouterr().out


# --- M3: issue coverage ---------------------------------------------------------


def test_m3_fails_on_an_unindexed_issue_and_skips_without_a_tag(capsys, tmp_path):
    changelog = _changelog(tmp_path, "## [0.0.7] - 2026-11-01\n\n- One change (#99)\n")
    git = _fake_git(tags="v0.0.6\nv0.0.5\n", subjects="fix things (#99)\nfeat: more (#101)\n")
    assert gate.check_issue_coverage("0.0.7", changelog=changelog, git=git) is False
    out = capsys.readouterr().out
    assert out.startswith("FAIL M3") and "#101" in out and "#99" not in out
    untagged = _fake_git(tags="v0.0.8\n", subjects="fix things (#99)\n")
    assert gate.check_issue_coverage("0.0.7", changelog=changelog, git=untagged) is False
    out = capsys.readouterr().out
    assert out.startswith("SKIP M3") and "no v* tag below 0.0.7" in out


def test_m3_reads_the_version_section_when_it_exists(capsys, tmp_path):
    """A cut that already wrote its own section reconciles against that one."""
    changelog = _changelog(
        tmp_path,
        "## [0.0.7] - 2026-11-01\n\n- One change (#99)\n\n"
        "## [Unreleased]\n\n- Work indexed only under Unreleased (#101)\n",
    )
    git = _fake_git(tags="v0.0.6\n", subjects="fix things (#99)\nfeat: more (#101)\n")
    assert gate.check_issue_coverage("0.0.7", changelog=changelog, git=git) is False
    out = capsys.readouterr().out.strip()
    assert out.startswith("FAIL M3 (reading [0.0.7]):")
    assert "missing #101" in out and "[Unreleased]" not in out


def test_m3_reads_unreleased_and_says_so_when_the_version_section_is_absent(capsys, tmp_path):
    """Pre-cut the entries live under [Unreleased]; the line names the section it read."""
    changelog = _changelog(
        tmp_path, "## [Unreleased]\n\n- One change (#99)\n- Another (#100)\n"
    )
    git = _fake_git(tags="v0.0.6\nv0.0.5\n", subjects="fix things (#99)\nfeat: more (#100)\n")
    assert gate.check_issue_coverage("0.0.7", changelog=changelog, git=git) is True
    out = capsys.readouterr().out.strip()
    assert out.startswith("PASS M3 (reading [Unreleased]):")
    assert "v0.0.6..HEAD is accounted for" in out
    # The same changelog fails M2: the checks do not depend on each other.
    assert gate.check_changelog_shape("0.0.7", changelog=changelog) is False
    assert capsys.readouterr().out.startswith("FAIL M2")
    unindexed = _fake_git(tags="v0.0.6\n", subjects="fix things (#99)\nnew work (#104)\n")
    assert gate.check_issue_coverage("0.0.7", changelog=changelog, git=unindexed) is False
    out = capsys.readouterr().out.strip()
    assert out.startswith("FAIL M3 (reading [Unreleased]):") and "missing #104" in out


def test_m3_fails_without_a_section_to_attribute_instead_of_listing_issues(capsys, tmp_path):
    changelog = _changelog(tmp_path, "## [0.0.6] - 2026-10-28\n\n- Already shipped (#95)\n")
    git = _fake_git(tags="v0.0.6\n", subjects="fix things (#99)\n")
    assert gate.check_issue_coverage("0.0.7", changelog=changelog, git=git) is False
    out = capsys.readouterr().out.strip()
    assert out.startswith("FAIL M3")
    assert "no changelog section to attribute" in out
    assert "#99" not in out and "missing" not in out


# --- M4: environment parity -----------------------------------------------------


def test_m4_flags_an_undocumented_compose_variable(capsys, tmp_path):
    compose = _write(
        tmp_path / "docker-compose.yml",
        "services:\n  engine:\n    environment:\n"
        "      FABULIST_HOST_ALIAS: host.docker.internal\n"
        "  models:\n    environment:\n"
        "      # a comment is prose, not configuration\n"
        "      FABULIST_SIDECAR_WARMUP: ${FABULIST_SIDECAR_WARMUP:-on}\n",
    )
    env = _write(tmp_path / ".env.example", "#FABULIST_SIDECAR_DEVICE=cpu\n")
    assert gate.check_environment_parity(compose_files=(compose,), env_example=env) is False
    out = capsys.readouterr().out
    assert out.startswith("FAIL M4")
    assert "FABULIST_SIDECAR_WARMUP" in out and "FABULIST_HOST_ALIAS" not in out


def test_m4_accepts_documented_and_plumbing_names(capsys, tmp_path):
    compose = _write(
        tmp_path / "docker-compose.yml",
        "services:\n  engine:\n    environment:\n"
        "      FABULIST_HOST_ALIAS: host.docker.internal\n"
        "      FABULIST_SIDECAR_URL: http://models:8080/v1\n"
        "  models:\n    environment:\n"
        "      FABULIST_SIDECAR_DEVICE: ${FABULIST_SIDECAR_DEVICE:-cpu}\n",
    )
    env = _write(tmp_path / ".env.example", "#FABULIST_SIDECAR_DEVICE=cpu\n")
    assert gate.check_environment_parity(compose_files=(compose,), env_example=env) is True
    assert capsys.readouterr().out.startswith("PASS M4")


# --- M5: retired words ----------------------------------------------------------


def test_m5_flags_the_retired_word_as_a_label_but_not_as_an_identifier(capsys, tmp_path):
    pane = tmp_path / "frontend" / "src" / "studio" / "DecisionQueriesPane.svelte"
    _write(pane, "const q = await getDecisionQueries();\nconst kind = { kind: 'queries' };\n")
    assert gate.check_retired_words(root=tmp_path) is True
    assert "absent from 1 file(s) scanned" in capsys.readouterr().out
    kinds = tmp_path / "frontend" / "src" / "lib" / "studio" / "kinds.ts"
    _write(kinds, "tab: 'Queries'\n")
    assert gate.check_retired_words(root=tmp_path) is False
    out = capsys.readouterr().out
    assert out.startswith("FAIL M5") and "kinds.ts:1" in out and "Repair triggers" in out


# --- M7: links and anchors ------------------------------------------------------


def test_m7_resolves_github_slug_anchors_and_flags_breaks(capsys, tmp_path):
    _write(tmp_path / "docs" / "log.md", "## 1. Log and state\n")
    _write(tmp_path / "README.md", "[log](docs/log.md#1-log-and-state)\n")
    _write(tmp_path / "AGENTS.md", "# Agents\n")
    assert gate.check_links_and_anchors(root=tmp_path) is True
    assert capsys.readouterr().out.startswith("PASS M7")
    _write(tmp_path / "README.md", "[log](docs/nope.md)\n")
    assert gate.check_links_and_anchors(root=tmp_path) is False
    assert "docs/nope.md (no such file)" in capsys.readouterr().out
    _write(tmp_path / "README.md", "[log](docs/log.md#1-log-and-state)\n")
    _write(tmp_path / "docs" / "log.md", "## 1. Log and state, renamed\n")
    assert gate.check_links_and_anchors(root=tmp_path) is False
    assert "(no such heading)" in capsys.readouterr().out


# --- CLI ------------------------------------------------------------------------


def test_a_missing_or_prefixed_version_is_a_usage_error(capsys):
    assert gate.main([]) == 2
    assert gate.main(["v0.0.7"]) == 2
    assert "usage:" in capsys.readouterr().err
