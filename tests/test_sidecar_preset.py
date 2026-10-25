"""The sidecar's self-provisioning contract (HuggingFace refs, no download step).

The owner-approved shape, pinned here so a later edit cannot quietly undo it:

* every section of the shipped ``deploy/sidecar/models.ini`` names its weights as
  a HuggingFace reference (``hf-repo`` + an exact ``hf-file``), not as a path
  under ``/models`` — llama.cpp fetches the files when the model is loaded;
* the three references are the verified table (repo, exact file, unruly details
  included: the reranker's DOT separator, the embedder's SHORT repo name);
* ``mmproj-auto`` is OFF for the two models that must not drag a projector in,
  and left on for the one whose projector the deployment actually wants;
* ``docker-compose.yml`` keeps that download in a named volume (so it survives
  recreates), points ``LLAMA_CACHE`` at it, and passes ``HF_TOKEN`` through;
* ``deploy/sidecar/entrypoint.sh`` validates the FORM of an hf ref and the
  EXISTENCE of a local path, and refuses to boot on an unwritable cache;
* issue #64: the entrypoint WARMS every preset model at container start (one
  ``POST /models/load`` per model id, polled until ``/v1/models`` says
  ``loaded``) and only then writes the readiness marker the compose healthcheck
  requires — so the engine's ``depends_on: service_healthy`` can never start
  against a cold sidecar, and a failed warmup is logged and stays unhealthy.
  ``FABULIST_SIDECAR_WARMUP=off`` restores the old serve-immediately behaviour
  for hosts that pre-seed the cache.

The entrypoint cases run the real script with a fake ``llama-server``, so the
boot checks and the warmup are exercised, not grepped. The preset-validation
cases point at a fake that only answers ``--list-devices`` and records its argv
(warmup off — they are about boot checks); the warmup cases point at a small
in-test ROUTER (``/v1/models`` + ``/models/load``) and assert on the marker, the
logs and the load requests.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import time
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
        # These cases are about the BOOT checks; the warmup cases below turn it
        # on deliberately (they need a router to warm against).
        "FABULIST_SIDECAR_WARMUP": "off",
        "FABULIST_SIDECAR_READY_DIR": str(preset.parent / "ready"),
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


# --- the warmup at container start (issue #64) -----------------------------
#
# The first turn of a new chat must not pay the 2.7 GiB weight download. The
# entrypoint asks the router to load every preset model and waits for
# /v1/models to say `loaded` before it writes the readiness marker the compose
# healthcheck tests for. These cases run the REAL script against a small router.

ROUTER_SCRIPT = '''#!/usr/bin/env python3
"""A stand-in for llama-server's ROUTER mode (what the warmup talks to).

Answers --list-devices, serves GET /v1/models with a per-model status, and
accepts POST /models/load the way the pinned image (b11804) was probed to: 200
plus an ASYNCHRONOUS transition to loaded, and 400 "model is already running"
for a redundant load. FAKE_* env vars choose the outcome; every load request is
appended to FAKE_REQUEST_LOG.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEVICES = """Available devices:
  Vulkan0: NVIDIA GeForce RTX 5090 (32607 MiB, 30000 MiB free)
"""

ARGS = sys.argv[1:]
if "--list-devices" in ARGS:
    print(DEVICES, end="")
    raise SystemExit(0)


def arg(name, default=None):
    return ARGS[ARGS.index(name) + 1] if name in ARGS else default


PORT = int(arg("--port", "8080"))
PRESET = arg("--models-preset")
FAIL_MODEL = os.environ.get("FAKE_FAIL_MODEL", "")
STUCK_MODEL = os.environ.get("FAKE_STUCK_MODEL", "")
# A load that fails the way the pinned image was probed to: the child exits and
# the router puts the model back to `unloaded` (it does NOT report `failed`).
DROP_MODEL = os.environ.get("FAKE_DROP_MODEL", "")
REQUEST_LOG = os.environ.get("FAKE_REQUEST_LOG", "")

MODELS = []
with open(PRESET, encoding="utf-8") as handle:
    for raw in handle:
        line = raw.strip()
        if line.startswith("[") and line.endswith("]") and line[1:-1] not in ("*", "default"):
            MODELS.append(line[1:-1])

# FAKE_PRELOADED=all starts every model `loaded` (a cache the router is already
# serving), which is the "weights already local" case the warmup must not slow.
PRELOADED = os.environ.get("FAKE_PRELOADED", "")
STATE = {model: ("loaded" if PRELOADED == "all" else "unloaded") for model in MODELS}
LOCK = threading.Lock()


def record(message):
    if not REQUEST_LOG:
        return
    with open(REQUEST_LOG, "a", encoding="utf-8") as handle:
        handle.write(message + "\\n")


def finish(model):
    time.sleep(1.0 if model == DROP_MODEL else 0.2)
    with LOCK:
        STATE[model] = "unloaded" if model == DROP_MODEL else "loaded"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def reply(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            with LOCK:
                data = [
                    {"id": model, "object": "model", "status": {"value": STATE[model]}}
                    for model in MODELS
                ]
            self.reply(200, {"object": "list", "data": data})
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        model = payload.get("model")
        if self.path != "/models/load" or model not in STATE:
            self.reply(404, {"error": "not found"})
            return
        record("load " + str(model))
        with LOCK:
            if STATE[model] == "loaded":
                self.reply(400, {"error": {"code": 400, "message": "model is already running"}})
                return
            STATE[model] = "failed" if model == FAIL_MODEL else "loading"
        if model not in (FAIL_MODEL, STUCK_MODEL):
            threading.Thread(target=finish, args=(model,), daemon=True).start()
        self.reply(200, {"success": True})


print("[fake-router] listening on %d" % PORT, flush=True)
ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''

TWO_MODELS = (
    "[decision]\nhf-repo = org/decision\nhf-file = decision.gguf\n\n"
    "[embedder]\nhf-repo = org/embedder\nhf-file = embedder.gguf\n"
)


@pytest.fixture()
def fake_router(tmp_path: Path) -> Path:
    """An executable stand-in for the router: /v1/models + /models/load."""
    script = tmp_path / "llama-server"
    script.write_text(ROUTER_SCRIPT, encoding="utf-8")
    script.chmod(0o755)
    return script


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(predicate, *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@contextlib.contextmanager
def running_entrypoint(
    preset: Path,
    *,
    server: Path,
    ready_dir: Path,
    log_path: Path,
    extra_env: dict[str, str] | None = None,
):
    """Start the entrypoint in its own session and always reap it (and its kids)."""
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LLAMA_SERVER": str(server),
        "FABULIST_SIDECAR_PRESET": str(preset),
        "FABULIST_SIDECAR_DEVICE": "Vulkan0",
        "FABULIST_SIDECAR_PORT": str(free_port()),
        "FABULIST_SIDECAR_READY_DIR": str(ready_dir),
        "FABULIST_SIDECAR_WARMUP_TIMEOUT": "20",
    }
    env.update(extra_env or {})
    with log_path.open("wb") as handle:
        process = subprocess.Popen(
            ["sh", str(ENTRYPOINT)],
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)


def read_log(log_path: Path) -> str:
    return log_path.read_text(encoding="utf-8") if log_path.exists() else ""


def test_entrypoint_warms_every_preset_model_before_it_reports_ready(fake_router, tmp_path):
    """One load request per model, and the marker only once all are loaded."""
    preset = write_preset(tmp_path, TWO_MODELS)
    ready = tmp_path / "ready"
    log = tmp_path / "entrypoint.log"
    requests = tmp_path / "loads.log"
    marker = ready / "warmup.ok"

    with running_entrypoint(
        preset,
        server=fake_router,
        ready_dir=ready,
        log_path=log,
        extra_env={"FAKE_REQUEST_LOG": str(requests)},
    ) as process:
        assert wait_for(marker.exists), read_log(log)[-4000:]
        assert process.poll() is None, "the entrypoint must go on serving after the warmup"

    assert json.loads(marker.read_text(encoding="utf-8"))["warmed"] == ["decision", "embedder"]
    assert requests.read_text(encoding="utf-8").splitlines() == ["load decision", "load embedder"]
    output = read_log(log)
    assert "downloaded at container start" in output, "the boot log must not claim a first-request download"
    assert "warming decision ..." in output and "warming embedder ..." in output
    assert "warmup complete" in output


def test_a_failed_warmup_names_the_model_and_leaves_the_container_unhealthy(fake_router, tmp_path):
    """Non-silent and non-crashing: logged, no marker, the router keeps serving."""
    preset = write_preset(tmp_path, TWO_MODELS)
    ready = tmp_path / "ready"
    log = tmp_path / "entrypoint.log"

    with running_entrypoint(
        preset,
        server=fake_router,
        ready_dir=ready,
        log_path=log,
        extra_env={"FAKE_FAIL_MODEL": "embedder"},
    ) as process:
        assert wait_for(
            lambda: "WARMUP FAILED: embedder could not be loaded" in read_log(log)
        ), read_log(log)[-4000:]
        # The healthy marker is what gates the engine start: it must be absent.
        assert not (ready / "warmup.ok").exists()
        assert "engine is NOT started against a cold sidecar" in read_log(log)
        assert process.poll() is None, "a failed warmup must not crash-loop the container"


def test_a_warmup_that_never_settles_times_out_and_stays_unhealthy(fake_router, tmp_path):
    preset = write_preset(tmp_path, TWO_MODELS)
    ready = tmp_path / "ready"
    log = tmp_path / "entrypoint.log"

    with running_entrypoint(
        preset,
        server=fake_router,
        ready_dir=ready,
        log_path=log,
        extra_env={
            "FAKE_STUCK_MODEL": "decision",
            "FABULIST_SIDECAR_WARMUP_TIMEOUT": "3",
        },
    ) as process:
        assert wait_for(
            lambda: "WARMUP FAILED: warmup timed out" in read_log(log), timeout=30
        ), read_log(log)[-4000:]
        assert "decision=loading" in read_log(log)
        assert not (ready / "warmup.ok").exists()
        assert process.poll() is None


def test_a_load_that_falls_back_to_unloaded_is_a_logged_failure(fake_router, tmp_path):
    """The shape a bad hf-file actually takes on the pinned image.

    Probed live (b11804): the router spawns the child, the child exits 1 because
    the file is not in the repo, and the model goes back to `unloaded` — never
    `failed`. The warmup has to call that a failure on its own, promptly, or the
    container would sit there looking merely slow until the timeout.
    """
    preset = write_preset(tmp_path, TWO_MODELS)
    ready = tmp_path / "ready"
    log = tmp_path / "entrypoint.log"

    with running_entrypoint(
        preset,
        server=fake_router,
        ready_dir=ready,
        log_path=log,
        extra_env={"FAKE_DROP_MODEL": "embedder"},
    ) as process:
        assert wait_for(
            lambda: "WARMUP FAILED: embedder could not be loaded"
            in read_log(log),
            timeout=30,
        ), read_log(log)[-4000:]
        assert "fell back to unloaded" in read_log(log)
        assert not (ready / "warmup.ok").exists()
        assert process.poll() is None, "a failed warmup must not crash-loop the container"


def test_an_already_loaded_model_is_not_reloaded(fake_router, tmp_path):
    """Idempotent: with the weights local the warmup only reads statuses."""
    preset = write_preset(tmp_path, TWO_MODELS)
    ready = tmp_path / "ready"
    log = tmp_path / "entrypoint.log"
    requests = tmp_path / "loads.log"

    with running_entrypoint(
        preset,
        server=fake_router,
        ready_dir=ready,
        log_path=log,
        extra_env={"FAKE_PRELOADED": "all", "FAKE_REQUEST_LOG": str(requests)},
    ) as process:
        assert wait_for((ready / "warmup.ok").exists), read_log(log)[-4000:]
        assert process.poll() is None

    output = read_log(log)
    assert "warmup: decision is already loaded" in output
    assert "warming decision" not in output
    assert not requests.exists() or requests.read_text(encoding="utf-8") == ""


def test_warmup_can_be_switched_off_for_a_pre_seeded_cache(fake_server, tmp_path):
    """The opt-out restores serve-immediately AND still reports ready."""
    server, argv_out = fake_server
    preset = write_preset(tmp_path, TWO_MODELS)
    ready = tmp_path / "ready"

    result = run_entrypoint(preset, server=server, cache=tmp_path / "cache")

    assert result.returncode == 0, result.stderr
    assert "warmup disabled (FABULIST_SIDECAR_WARMUP=off)" in result.stdout
    assert "downloads on first request" in result.stdout
    assert (ready / "warmup.ok").exists(), "health must be reachable with the warmup off"
    assert argv_out.read_text(encoding="utf-8").strip(), "the server is started with the preset, as before"


def test_a_bogus_warmup_mode_is_a_boot_error(fake_server, tmp_path):
    server, _ = fake_server
    preset = write_preset(tmp_path, TWO_MODELS)

    result = run_entrypoint(preset, server=server, extra_env={"FABULIST_SIDECAR_WARMUP": "maybe"})

    assert result.returncode != 0
    assert "FABULIST_SIDECAR_WARMUP='maybe' is not a mode" in result.stderr


def test_compose_healthcheck_requires_the_warmup_marker():
    """The gate: healthy means the models are local, not merely that the router listens."""
    compose = read_compose()
    assert "/tmp/fabulist-sidecar/warmup.ok" in compose, (
        "the healthcheck must test the marker the entrypoint writes after the warmup"
    )
    assert "test -f /tmp/fabulist-sidecar/warmup.ok" in compose
    assert "FABULIST_SIDECAR_WARMUP" in compose, "the opt-out is a documented compose knob"
    assert "downloads on first request" not in compose, "the stale boot claim is gone"


def test_compose_waits_at_least_as_long_as_the_warmup_budget():
    """A cold 2.7 GiB fetch must fit inside the healthcheck's start_period."""
    budget = int(
        re.search(r"FABULIST_SIDECAR_WARMUP_TIMEOUT:-(\d+)", ENTRYPOINT.read_text(encoding="utf-8")).group(1)
    )
    start_period = int(re.search(r"start_period:\s*(\d+)s", read_compose()).group(1))
    assert start_period >= budget
