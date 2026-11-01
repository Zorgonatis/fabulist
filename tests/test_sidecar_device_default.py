"""The sidecar's shipped device default (issue #102): CPU, with the GPU as opt-in.

The entrypoint resolves the device before it starts llama-server, so the argv
the server records is the answer. These cases pin the default and the
no-guessing rule:

* nothing set means CPU — on a host with no GPU, with one GPU, and with two;
* nothing set never picks a visible GPU for the user (an automatic switch is one
  the user cannot see coming; the owner rejected that option on issue #102, so
  the free-memory check is a warning and nothing more);
* the explicit GPU forms still work: an id, a bare index, and a name match;
* the shipped compose file and `.env.example` say the same thing.

The fake llama-server answers ``--list-devices`` and records its argv, then
exits. The entrypoint exits with it, so each case is one short subprocess.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO_ROOT / "deploy" / "sidecar" / "entrypoint.sh"

ONE_GPU = """Available devices:
  Vulkan0: NVIDIA GeForce RTX 5090 (32607 MiB, 30000 MiB free)
"""

TWO_GPUS = """Available devices:
  Vulkan0: NVIDIA GeForce RTX 5090 (32607 MiB, 30000 MiB free)
  Vulkan1: AMD Radeon RX 7900 XTX (24560 MiB, 24000 MiB free)
"""

NO_GPU = """Available devices:
"""

SMALL_GPU = """Available devices:
  Vulkan0: NVIDIA GeForce RTX 3060 (6144 MiB, 3000 MiB free)
"""


def write_fake_server(tmp_path: Path, devices: str) -> tuple[Path, Path]:
    """A stand-in llama-server: answers --list-devices, records its argv."""
    binary = tmp_path / "llama-server"
    argv_out = tmp_path / "argv.txt"
    binary.write_text(
        "#!/bin/sh\n"
        f'out="{argv_out}"\n'
        'if [ "$1" = "--list-devices" ]; then\n'
        f"  printf '%s' '{devices}'\n"
        "  exit 0\n"
        "fi\n"
        'for a in "$@"; do printf "%s\\n" "$a" >> "$out"; done\n'
        "exit 0\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary, argv_out


def boot(tmp_path: Path, devices: str, **device_env: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Boot the entrypoint with the given device variables and return its argv."""
    server, argv_out = write_fake_server(tmp_path, devices)
    preset = tmp_path / "models.ini"
    preset.write_text(
        "version = 1\n\n"
        "[demo]\n"
        "hf-repo = ggml-org/models\n"
        "hf-file = tinyllamas/stories15M-q8_0.gguf\n"
        "ctx-size = 512\n",
        encoding="utf-8",
    )
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LLAMA_SERVER": str(server),
        "LLAMA_CACHE": str(tmp_path / "cache"),
        "FABULIST_SIDECAR_PRESET": str(preset),
        "FABULIST_SIDECAR_WARMUP": "off",
        "FABULIST_SIDECAR_READY_DIR": str(tmp_path / "ready"),
    }
    env.update(device_env)
    result = subprocess.run(
        ["sh", str(ENTRYPOINT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    argv = argv_out.read_text(encoding="utf-8").splitlines() if argv_out.exists() else []
    return result, argv


def device_arg(argv: list[str]) -> str | None:
    return argv[argv.index("--device") + 1] if "--device" in argv else None


# --- the default: CPU, on every host ---------------------------------------


@pytest.mark.parametrize(
    "label,devices",
    [("no GPU", NO_GPU), ("one GPU", ONE_GPU), ("two GPUs", TWO_GPUS)],
)
def test_nothing_set_starts_on_the_cpu(tmp_path, label, devices):
    """A fresh clone has no .env. That boot is CPU, whatever the host offers."""
    result, argv = boot(tmp_path, devices)

    assert result.returncode == 0, result.stderr
    assert device_arg(argv) == "none", f"{label}: the models must stay on the CPU"
    assert "the shipped default" in result.stdout
    assert "FATAL" not in result.stderr


def test_one_visible_gpu_is_not_picked_for_the_user(tmp_path):
    """Exactly one device is not an invitation: the GPU is an explicit opt-in."""
    result, argv = boot(tmp_path, ONE_GPU)

    assert result.returncode == 0, result.stderr
    assert device_arg(argv) == "none"
    assert "FABULIST_SIDECAR_DEVICE=VulkanN" in result.stdout, "the log names the way to opt in"


def test_two_visible_gpus_do_not_stop_the_boot(tmp_path):
    """Ambiguity used to be fatal when nothing was set. CPU ends the ambiguity."""
    result, argv = boot(tmp_path, TWO_GPUS)

    assert result.returncode == 0, result.stderr
    assert device_arg(argv) == "none"


# --- the opt-in: unchanged ------------------------------------------------


@pytest.mark.parametrize("value", ["Vulkan0", "0"])
def test_an_explicit_device_id_still_selects_that_gpu(tmp_path, value):
    result, argv = boot(tmp_path, ONE_GPU, FABULIST_SIDECAR_DEVICE=value)

    assert result.returncode == 0, result.stderr
    assert device_arg(argv) == "Vulkan0"
    assert "using Vulkan device" in result.stdout


def test_a_name_match_still_selects_that_gpu(tmp_path):
    result, argv = boot(tmp_path, TWO_GPUS, FABULIST_SIDECAR_DEVICE_MATCH="7900")

    assert result.returncode == 0, result.stderr
    assert device_arg(argv) == "Vulkan1"


@pytest.mark.parametrize("value", ["cpu", "none"])
def test_the_cpu_can_also_be_asked_for_by_name(tmp_path, value):
    result, argv = boot(tmp_path, ONE_GPU, FABULIST_SIDECAR_DEVICE=value)

    assert result.returncode == 0, result.stderr
    assert device_arg(argv) == "none"
    assert "by request" in result.stdout


def test_a_requested_device_that_does_not_exist_is_still_fatal(tmp_path):
    result, argv = boot(tmp_path, ONE_GPU, FABULIST_SIDECAR_DEVICE="Vulkan1")

    assert result.returncode != 0
    assert "no such Vulkan device exists" in result.stderr
    assert argv == [], "the server is never started"


# --- the memory check warns, and never chooses -----------------------------


def test_low_free_memory_warns_and_still_uses_the_requested_gpu(tmp_path):
    """Option 1 was rejected: no automatic fall back to CPU on a small device."""
    result, argv = boot(tmp_path, SMALL_GPU, FABULIST_SIDECAR_DEVICE="Vulkan0")

    assert result.returncode == 0, result.stderr
    assert device_arg(argv) == "Vulkan0", "the request wins; the check only warns"
    assert "reports only 3000 MiB free" in result.stdout
    assert "may fail with an out-of-memory error" in result.stdout


# --- the two files a user reads say the same default ----------------------


def test_the_shipped_compose_file_defaults_the_device_to_cpu():
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "FABULIST_SIDECAR_DEVICE: ${FABULIST_SIDECAR_DEVICE:-cpu}" in compose
    assert "FABULIST_SIDECAR_DEVICE_MATCH: ${FABULIST_SIDECAR_DEVICE_MATCH:-}" in compose


def test_the_env_example_offers_cpu_and_both_gpu_forms():
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "#FABULIST_SIDECAR_DEVICE=cpu" in example
    assert "#FABULIST_SIDECAR_DEVICE=Vulkan0" in example
    assert "#FABULIST_SIDECAR_DEVICE_MATCH=" in example
