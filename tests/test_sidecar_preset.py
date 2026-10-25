"""The sidecar's self-provisioning contract (HuggingFace refs, no download step).

The owner-approved shape, pinned here so a later edit cannot quietly undo it:

* every section of the shipped ``deploy/sidecar/models.ini`` names its weights as
  a HuggingFace reference (``hf-repo`` + an exact ``hf-file``), not as a path
  under ``/models`` — llama.cpp fetches the files on the model's FIRST REQUEST;
* the three references are the verified table (repo, exact file, unruly details
  included: the reranker's DOT separator, the embedder's SHORT repo name);
* ``mmproj-auto`` is OFF for the two models that must not drag a projector in,
  and left on for the one whose projector the deployment actually wants;
* ``docker-compose.yml`` keeps that download in a named volume (so it survives
  recreates), points ``LLAMA_CACHE`` at it, and passes ``HF_TOKEN`` through;
* ``deploy/sidecar/entrypoint.sh`` validates the FORM of an hf ref and the
  EXISTENCE of a local path, and refuses to boot on an unwritable cache.

The entrypoint cases run the real script with a fake ``llama-server`` (it only
needs ``--list-devices`` and an exit), so the boot checks are exercised, not
grepped.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from fabulist.sidecar import SIDECAR_ROLES

REPO_ROOT = Path(__file__).resolve().parents[1]
PRESET = REPO_ROOT / "deploy" / "sidecar" / "models.ini"
ENTRYPOINT = REPO_ROOT / "deploy" / "sidecar" / "entrypoint.sh"

#: The owner-pinned table. Weights are (repo, exact file); a projector only
#: exists where the deployment wants one.
#:
#: The reranker repo is NOT the popular mradermacher mirror: that conversion
#: drops the sequence-classification head (no qwen3.classifier.output_labels,
#: no cls.output.weight), so `pooling = rank` scores a generative model and the
#: true-match document came back at 0.0 in a live probe. Voodisss's Q4_K_M is
#: byte-identical (sha256 c04f5f56…) to the artifact the owner's deployment had
#: verified. A file's NAME cannot tell you that; its kv block can.
EXPECTED_REFS = {
    "d1-3b": ("LiquidAI/d1-3B-GGUF", "d1-3B-Q4_K_M.gguf"),
    "embeddinggemma-2": ("unsloth/embeddinggemma-2-GGUF", "embeddinggemma-2-Q8_0.gguf"),
    "qwen3-reranker": ("Voodisss/Qwen3-Reranker-0.6B-GGUF-llama_cpp", "Qwen3-Reranker-0.6B-Q4_K_M.gguf"),
}


def read_preset_sections() -> dict[str, dict[str, str]]:
    """``section -> {key: value}`` from the shipped router preset.

    llama.cpp's preset is not ``configparser``-shaped (top-level keys before the
    first section, ``*`` is legal), so the lines are read directly.
    """
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in PRESET.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        current[key.strip()] = value.strip()
    return sections


# --- the shipped preset ----------------------------------------------------


def test_every_served_model_names_its_weights_as_a_huggingface_reference():
    sections = read_preset_sections()
    served = {model for _slot, model in SIDECAR_ROLES.values()}

    assert served <= set(sections), "every pinned role's model id has a section"
    for model_id, (repo, hf_file) in EXPECTED_REFS.items():
        section = sections[model_id]
        assert section["hf-repo"] == repo, f"{model_id} fetches from the verified repo"
        assert section["hf-file"] == hf_file, f"{model_id} pins the exact file, not a quant tag"
        assert hf_file.endswith(".gguf")


def test_no_section_falls_back_to_a_models_directory_path():
    """The point of the change: nothing has to exist on disk before first boot."""
    for model_id, section in read_preset_sections().items():
        assert "model" not in section, f"{model_id} must not name a local model path"
        assert "mmproj" not in section, f"{model_id} must not name a local projector path"


def test_the_projector_autopick_is_off_only_where_a_projector_is_unwanted():
    sections = read_preset_sections()
    # d1's repo ships 543-797 MiB projectors the decision route never uses, and
    # the reranker's ships none (off keeps a future sibling from appearing).
    for model_id in ("d1-3b", "qwen3-reranker"):
        assert sections[model_id]["mmproj-auto"].lower() == "false"
    # The embedder WANTS its projector (input_modalities), quant-matched from
    # the same repo: mmproj-Q8_0.gguf for a Q8_0 model file.
    assert "mmproj-auto" not in sections["embeddinggemma-2"]


def test_the_serving_flags_survive_the_ref_rewrite():
    """ctx/pooling/normalization are the owner's wiring, not part of the ref move."""
    sections = read_preset_sections()
    assert sections["qwen3-reranker"]["ctx-size"] == "2048"
    assert sections["qwen3-reranker"]["pooling"] == "rank"
    assert sections["qwen3-reranker"]["cache-type-k"] == "q8_0"
    assert sections["embeddinggemma-2"]["ctx-size"] == "8192"
    assert sections["embeddinggemma-2"]["pooling"] == "mean"
    assert sections["embeddinggemma-2"]["embd-normalize"] == "2"
    assert sections["embeddinggemma-2"]["ubatch-size"] == "32768"
    assert sections["d1-3b"]["ctx-size"] == "8192"


# --- compose keeps what the download costs ---------------------------------


def read_compose() -> str:
    return (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def test_compose_backs_the_hf_cache_with_a_named_volume():
    compose = read_compose()
    assert "LLAMA_CACHE: /cache" in compose, "the cache location is pinned, not left to $HOME"
    assert "fabulist-model-cache:/cache" in compose, "the cache is a volume, so it outlives a recreate"
    assert "\nvolumes:\n  fabulist-model-cache:" in compose, (
        "the named volume must be DECLARED, else compose creates an anonymous one"
    )


def test_compose_passes_hf_token_through_and_documents_it_as_optional():
    compose = read_compose()
    assert "HF_TOKEN: ${HF_TOKEN:-}" in compose
    # The stale claim the rewrite removed: nothing shipped is gated.
    assert "is gated on Hugging Face" not in compose
    assert "not anonymously fetchable" not in compose


# --- the entrypoint's boot checks (run for real) ---------------------------


DEVICES = """Available devices:
  Vulkan0: NVIDIA GeForce RTX 5090 (32607 MiB, 30000 MiB free)
"""


@pytest.fixture()
def fake_server(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in llama-server: answers --list-devices, records its argv."""
    binary = tmp_path / "llama-server"
    argv_out = tmp_path / "argv.txt"
    binary.write_text(
        "#!/bin/sh\n"
        f'out="{argv_out}"\n'
        'if [ "$1" = "--list-devices" ]; then\n'
        f"  printf '%s' '{DEVICES}'\n"
        "  exit 0\n"
        "fi\n"
        'for a in "$@"; do printf "%s\\n" "$a" >> "$out"; done\n'
        "exit 0\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary, argv_out


def run_entrypoint(
    preset: Path,
    *,
    server: Path,
    cache: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LLAMA_SERVER": str(server),
        "FABULIST_SIDECAR_PRESET": str(preset),
        "FABULIST_SIDECAR_DEVICE": "Vulkan0",
    }
    if cache is not None:
        env["LLAMA_CACHE"] = str(cache)
    env.update(extra_env or {})
    return subprocess.run(
        ["sh", str(ENTRYPOINT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def write_preset(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.ini"
    path.write_text("version = 1\n\n" + body, encoding="utf-8")
    return path


def test_entrypoint_boots_on_hf_refs_without_any_file_on_disk(fake_server, tmp_path):
    server, argv_out = fake_server
    cache = tmp_path / "cache"
    preset = write_preset(
        tmp_path,
        "[demo]\nhf-repo = ggml-org/models\nhf-file = tinyllamas/stories15M-q8_0.gguf\nctx-size = 512\n",
    )

    result = run_entrypoint(preset, server=server, cache=cache)

    assert result.returncode == 0, result.stderr
    assert "downloads on first request" in result.stdout
    assert "weights cache" in result.stdout
    assert cache.is_dir(), "the cache directory is created, not assumed"
    argv = argv_out.read_text(encoding="utf-8").splitlines()
    assert argv[0] == "--models-preset"
    assert str(preset) in argv


def test_entrypoint_rejects_a_malformed_repo_ref(fake_server, tmp_path):
    server, _ = fake_server
    preset = write_preset(tmp_path, "[demo]\nhf-repo = not-a-repo\nhf-file = x.gguf\n")

    result = run_entrypoint(preset, server=server, cache=tmp_path / "cache")

    assert result.returncode != 0
    assert "hf-repo = 'not-a-repo'" in result.stderr
    assert "[demo]" in result.stderr


def test_entrypoint_rejects_a_non_gguf_hf_file(fake_server, tmp_path):
    server, _ = fake_server
    preset = write_preset(tmp_path, "[demo]\nhf-repo = org/repo\nhf-file = weights.bin\n")

    result = run_entrypoint(preset, server=server, cache=tmp_path / "cache")

    assert result.returncode != 0
    assert "hf-file = 'weights.bin'" in result.stderr


def test_entrypoint_still_requires_a_local_model_file_to_exist(fake_server, tmp_path):
    """The offline override keeps the old, file-naming failure."""
    server, _ = fake_server
    preset = write_preset(tmp_path, "[demo]\nmodel = /models/missing.gguf\n")

    result = run_entrypoint(preset, server=server, cache=None)

    assert result.returncode != 0
    assert "/models/missing.gguf does not exist" in result.stderr


def test_entrypoint_refuses_a_section_that_names_both_sources(fake_server, tmp_path):
    server, _ = fake_server
    local = tmp_path / "local.gguf"
    local.write_bytes(b"GGUF")
    preset = write_preset(
        tmp_path,
        f"[demo]\nmodel = {local}\nhf-repo = org/repo\nhf-file = x.gguf\n",
    )

    result = run_entrypoint(preset, server=server, cache=tmp_path / "cache")

    assert result.returncode != 0
    assert "BOTH hf-repo" in result.stderr


def test_entrypoint_fails_when_the_hf_cache_is_not_writable(fake_server, tmp_path):
    server, _ = fake_server
    cache = tmp_path / "ro-cache"
    cache.mkdir()
    cache.chmod(0o500)  # readable, not writable
    preset = write_preset(tmp_path, "[demo]\nhf-repo = org/repo\nhf-file = x.gguf\n")

    try:
        result = run_entrypoint(preset, server=server, cache=cache)
    finally:
        cache.chmod(0o700)

    if os.geteuid() == 0:
        pytest.skip("running as root: a 0500 directory is still writable, so the check cannot fire")
    assert result.returncode != 0
    assert "not writable" in result.stderr
    assert str(cache) in result.stderr


def test_entrypoint_warns_instead_of_failing_when_no_cache_is_configured(fake_server, tmp_path):
    """A local-weights deployment must not need a cache at all."""
    server, argv_out = fake_server
    local = tmp_path / "local.gguf"
    local.write_bytes(b"GGUF")
    preset = write_preset(tmp_path, f"[demo]\nmodel = {local}\n")

    result = run_entrypoint(preset, server=server, cache=None)

    assert result.returncode == 0, result.stderr
    assert "local weights" in result.stdout
    assert "weights cache" not in result.stdout
    assert argv_out.read_text(encoding="utf-8").strip()
