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


def _versions(tmp_path: Path, version: str = "0.0.7", *, lock: str | None = None) -> dict:
    """The three M1 points, as keyword arguments for the check."""
    return {
        "pyproject": _write(
            tmp_path / "pyproject.toml",
            f'[project]\nname = "fabulist"\nversion = "{version}"\n',
        ),
        "package_json": _write(
            tmp_path / "frontend" / "package.json",
            '{\n\t"name": "fabulist-frontend",\n\t"version": "%s"\n}\n' % version,
        ),
        "uv_lock": _write(tmp_path / "uv.lock", _uv_lock(lock or version)),
    }


def _changelog(tmp_path: Path, body: str) -> Path:
    return _write(tmp_path / "CHANGELOG.md", f"# Changelog\n\n{body}")


def _fake_git(*, tags: str, subjects: str):
    def run(*args: str) -> tuple[int, str]:
        return (0, tags) if args[0] == "tag" else (0, subjects)

    return run


# --- M1: three version points ---------------------------------------------------


def test_m1_passes_when_all_three_points_agree(capsys, tmp_path):
    assert gate.check_version_agreement("0.0.7", **_versions(tmp_path)) is True
    out = capsys.readouterr().out.strip()
    assert out.startswith("PASS M1")
    assert "pyproject.toml:3" in out and "frontend/package.json:3" in out and "uv.lock:9" in out


def test_m1_names_a_stale_lockfile(capsys, tmp_path):
    """The release the owner remembers: manifests bumped, lockfile left behind."""
    points = _versions(tmp_path, lock="0.0.6")
    assert gate.check_version_agreement("0.0.7", **points) is False
    out = capsys.readouterr().out.strip()
    assert out.startswith("FAIL M1")
    assert "uv.lock:9 = 0.0.6" in out and "pyproject.toml" not in out


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
