#!/bin/sh
# Fabulist sidecar entrypoint: check the preset, resolve the Vulkan device, warm
# every model, then serve.
#
# WHY THIS EXISTS. A Vulkan device index is assigned in Vulkan's enumeration
# order, which is the loader's order, not the order of the cards on the board
# and not the order of any other backend's ids (on the maintainer's box
# Vulkan0 is the RTX 5090 and Vulkan1 is the RX 7900 XTX; a CUDA build calls
# the same 5090 "CUDA0"). Hard-coding "Vulkan0" in models.ini is therefore a
# guess: right on one host, silently wrong on the next — and "wrong" here looks
# like a working server on the wrong GPU, or an OOM far from the cause.
#
# The rule: resolve, verify, and fail loudly. Never assume.
#
# Selection, in order:
#   FABULIST_SIDECAR_DEVICE=VulkanN|N   explicit id; a missing id is FATAL.
#   FABULIST_SIDECAR_DEVICE_MATCH=text  case-insensitive substring of the device
#                                       name; zero or several matches is FATAL.
#   neither set, exactly one Vulkan device   use it (nothing to guess).
#   neither set, several Vulkan devices      FATAL, listing them. Picking one
#                                            would be exactly the assumption
#                                            this script exists to avoid.
#   neither set, no Vulkan device            fall through to CPU. This is the
#                                            Vulkan build's own native fallback
#                                            (verified: with the ICD hidden the
#                                            same binary serves rerank on CPU),
#                                            so there is no CPU profile.
#
# WEIGHTS. The shipped preset names HuggingFace references (hf-repo/hf-file),
# so the weights DO NOT EXIST ON DISK at boot — llama.cpp downloads them when
# the router spawns that model's child. The boot check is shaped to the source:
#
#   hf-repo/hf-file   FORM only: org/repo[:tag] and one *.gguf path inside it.
#                     A malformed ref is one clear boot error (a fetch failure
#                     at request time is llama.cpp's own to report, and it
#                     names repo/file/gated state itself).
#   model/mmproj      still a local path (the offline override): the file must
#                     exist, as before.
#   LLAMA_CACHE       must be writable when any section fetches from HF, or the
#                     first request dies mid-download with a permission error
#                     far from the cause. Unset means the cache lands inside the
#                     container and is lost on recreate — warned, not fatal.
#
# The preset's `[*]` section is the router's GLOBAL preset (shared flags folded
# into every model section), not a model: it is skipped here.
#
# WARMUP (issue #64). Downloading on a model's first request puts a 2.7 GiB
# fetch in the middle of a turn: the router cannot answer until the weights
# land, so a new chat's first turn looks slow or broken. The download is moved
# to container start instead: after the router listens, this script asks it to
# LOAD every preset model (`POST /models/load`), which fetches that model's
# weights and leaves it resident, and waits until /v1/models reports every id
# `loaded`. Only then is the readiness marker written. The compose healthcheck
# requires that marker, and the engine's `depends_on: service_healthy` gates on
# the healthcheck — so the engine cannot start against a cold sidecar, and a
# warmup that fails leaves the container UNHEALTHY (logged, never silent, and
# never a crash-loop: the router keeps serving).
#
# Why a background server + probe, not a llama-server flag: the router's preset
# has no "preload at boot" option on the pinned image (b11804). The load route
# is used instead of one synthetic inference call per role because it is
# role-agnostic — it warms a chat model or a local-weights preset exactly as it
# warms the shipped reranker/embedder/decision trio, with no per-section guess
# at which route a model speaks. It is idempotent and cheap when the weights
# are already in the cache: a load from disk, no fetch.
#
# The warmup is a request per model, so it travels the router. python3 (present
# on the pinned image) speaks HTTP: the image has curl, but python3 is what
# parses the per-model status out of /v1/models.
#
# Env overrides (defaults match docker-compose.yml):
#   LLAMA_SERVER                 /app/llama-server
#   FABULIST_SIDECAR_PRESET      /etc/fabulist/models.ini
#   FABULIST_SIDECAR_PORT        8080
#   FABULIST_SIDECAR_MODELS_MAX  3
#   FABULIST_SIDECAR_SLEEP_IDLE_SECONDS  300
#   FABULIST_SIDECAR_WARMUP      on   (off = serve immediately; for hosts that
#                                      pre-seed the cache. The readiness marker
#                                      is written at once, so health is unchanged
#                                      and the first request pays the download.)
#   FABULIST_SIDECAR_WARMUP_TIMEOUT  1800  seconds the warmup may take; keep it
#                                      under compose's healthcheck start_period.
#   FABULIST_SIDECAR_READY_DIR   /tmp/fabulist-sidecar  holds warmup.ok (the
#                                      marker the healthcheck tests for).

set -eu

LLAMA_SERVER="${LLAMA_SERVER:-/app/llama-server}"
PRESET="${FABULIST_SIDECAR_PRESET:-/etc/fabulist/models.ini}"
PORT="${FABULIST_SIDECAR_PORT:-8080}"
MODELS_MAX="${FABULIST_SIDECAR_MODELS_MAX:-3}"
SLEEP_IDLE="${FABULIST_SIDECAR_SLEEP_IDLE_SECONDS:-300}"
WARMUP_TIMEOUT="${FABULIST_SIDECAR_WARMUP_TIMEOUT:-1800}"
READY_DIR="${FABULIST_SIDECAR_READY_DIR:-/tmp/fabulist-sidecar}"
READY_FILE="$READY_DIR/warmup.ok"

log() { echo "[sidecar] $*"; }
fatal() {
    echo "[sidecar] FATAL: $*" >&2
    exit 1
}

WARMUP=$(printf '%s' "${FABULIST_SIDECAR_WARMUP:-on}" | tr '[:upper:]' '[:lower:]')
case "$WARMUP" in
    on|1|true|yes) WARMUP=on ;;
    off|0|false|no) WARMUP=off ;;
    *) fatal "FABULIST_SIDECAR_WARMUP='$WARMUP' is not a mode (use on, or off to skip the warmup)." ;;
esac

case "$WARMUP_TIMEOUT" in
    ''|*[!0-9]*) fatal "FABULIST_SIDECAR_WARMUP_TIMEOUT='$WARMUP_TIMEOUT' is not a whole number of seconds." ;;
esac

SERVER_PID=""
WARM_PID=""

[ -x "$LLAMA_SERVER" ] || fatal "llama-server not found or not executable: $LLAMA_SERVER"
[ -r "$PRESET" ] || fatal "models preset not readable: $PRESET"

# --- 1. preset sources: HF refs by FORM, local paths by EXISTENCE -----------
# Why both: docker-compose.yml ships HF refs (the warmup fetches them at
# container start), while a deployment that keeps GGUFs on disk binds its own
# preset and names paths under /models. The same script has to fail usefully for
# both, and it must never report an hf ref as a missing file.
problems=""
hf_sections=""
local_sections=""
# The section names ARE the model ids the router serves; kept in order for the
# warmup (issue #64) so the preset alone decides what gets warmed.
model_ids=""

section=""
sec_hf=""
sec_hf_file=""
sec_local=""
sec_mmproj=""

add_problem() {
    problems="$problems
  $*"
}

# org/repo, optional :tag — one slash, no spaces, no leading slash.
valid_hf_repo() {
    printf '%s' "$1" \
        | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*(:[A-Za-z0-9._-]+)?$'
}

# one GGUF path inside the repo (subdirectories are legal).
valid_hf_file() {
    printf '%s' "$1" | grep -Eq '^[A-Za-z0-9_][A-Za-z0-9._/-]*\.gguf$'
}

flush_section() {
    [ -n "$section" ] || return 0
    # The global section ([default] or [*]) carries shared flags, not a model.
    case "$section" in
        default|'*') section="" ; sec_hf="" ; sec_hf_file="" ; sec_local="" ; sec_mmproj="" ; return 0 ;;
    esac
    if [ -n "$sec_hf" ] && [ -n "$sec_local" ]; then
        add_problem "[$section] names BOTH hf-repo ($sec_hf) and a local model path ($sec_local); pick one."
    fi
    if [ -z "$sec_hf" ] && [ -z "$sec_local" ]; then
        add_problem "[$section] names no model: set hf-repo + hf-file, or model = <path>."
    fi
    if [ -n "$sec_hf" ]; then
        hf_sections="$hf_sections
  [$section] $sec_hf ${sec_hf_file:-<quant/default pick>}"
    else
        local_sections="$local_sections
  [$section] $sec_local${sec_mmproj:+ + $sec_mmproj}"
    fi
    model_ids="$model_ids $section"
    section=""
    sec_hf=""
    sec_hf_file=""
    sec_local=""
    sec_mmproj=""
}

while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        ''|\#*) continue ;;
    esac
    # A section header starts a new model id.
    case "$line" in
        \[*)
            flush_section
            section=$(printf '%s' "$line" | sed -e 's/^[[:space:]]*\[//' -e 's/\][[:space:]]*$//')
            continue
            ;;
    esac

    key=$(printf '%s' "${line%%=*}" | tr -d ' \t')
    val=$(printf '%s' "${line#*=}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')

    case "$key" in
        hf-repo)
            sec_hf="$val"
            if ! valid_hf_repo "$val"; then
                add_problem "[$section] hf-repo = '$val' is not an HF repo ref (expected org/repo or org/repo:tag)."
            fi
            ;;
        hf-file)
            sec_hf_file="$val"
            if ! valid_hf_file "$val"; then
                add_problem "[$section] hf-file = '$val' is not a *.gguf file path inside the repo."
            fi
            ;;
        model|mmproj)
            if [ "$key" = "model" ]; then
                sec_local="$val"
            else
                sec_mmproj="$val"
            fi
            [ -f "$val" ] || add_problem "[$section] $key = $val does not exist."
            ;;
    esac
done < "$PRESET"
flush_section

if [ -n "$problems" ]; then
    fatal "models preset $PRESET has unusable source(s):$problems

  hf-repo/hf-file are HuggingFace references: they are downloaded when the
  model is loaded (the entrypoint warms them at container start), so nothing
  has to exist on disk for them.
  model/mmproj are local paths: they are read from the read-only mount at
  /models (\${FABULIST_MODELS_DIR:-./models} on the host) and must exist.
  See README.md > Docker > Models sidecar."
fi

if [ -n "$hf_sections" ]; then
    if [ "$WARMUP" = "on" ]; then
        log "downloaded at container start, then served warm (HuggingFace):$hf_sections"
    else
        log "downloads on first request (HuggingFace):$hf_sections"
    fi
fi
if [ -n "$local_sections" ]; then
    log "local weights:$local_sections"
fi

# --- 2. the HuggingFace cache must be writable -------------------------------
# llama.cpp resolves its cache as LLAMA_CACHE, then HF_HUB_CACHE /
# HUGGINGFACE_HUB_CACHE, then HF_HOME/hub, then $HOME/.cache/huggingface/hub.
# Only the first three can be a mounted volume here; HF_HOME is honoured too so
# a user who set it is not surprised.
CACHE_DIR=""
if [ -n "${LLAMA_CACHE:-}" ]; then
    CACHE_DIR="$LLAMA_CACHE"
elif [ -n "${HF_HUB_CACHE:-}" ]; then
    CACHE_DIR="$HF_HUB_CACHE"
elif [ -n "${HUGGINGFACE_HUB_CACHE:-}" ]; then
    CACHE_DIR="$HUGGINGFACE_HUB_CACHE"
elif [ -n "${HF_HOME:-}" ]; then
    CACHE_DIR="$HF_HOME/hub"
fi

if [ -n "$hf_sections" ]; then
    if [ -z "$CACHE_DIR" ]; then
        log "WARNING: neither LLAMA_CACHE nor HF_HOME/HF_HUB_CACHE is set."
        log "WARNING: llama.cpp will cache the ~2.7 GiB of weights under \$HOME/.cache/huggingface/hub"
        log "WARNING: INSIDE this container — they are re-downloaded after every recreate."
    else
        mkdir -p "$CACHE_DIR" 2>/dev/null \
            || fatal "the HuggingFace cache directory cannot be created: $CACHE_DIR

  Set LLAMA_CACHE (docker-compose.yml uses /cache, backed by a named volume)."
        probe="$CACHE_DIR/.fabulist-write-probe.$$"
        ( : > "$probe" ) 2>/dev/null \
            || fatal "the HuggingFace cache directory is not writable: $CACHE_DIR

  The warmup downloads into it; a read-only or root-owned mount fails there
  instead of here, so this is a boot error. docker-compose.yml backs
  LLAMA_CACHE=/cache with a named volume that Docker creates writable."
        rm -f "$probe"

        if command -v df >/dev/null 2>&1; then
            avail_mib=$(df -Pk "$CACHE_DIR" 2>/dev/null | awk 'NR==2 { print int($4 / 1024) }') || avail_mib=""
            case "$avail_mib" in
                ''|*[!0-9]*) : ;;
                *)
                    if [ "$avail_mib" -lt 4096 ]; then
                        log "WARNING: ${avail_mib} MiB free under $CACHE_DIR; the three models need ~2.8 GiB (plus room for a second copy during a download)."
                    fi
                    ;;
            esac
        fi
        log "weights cache: $CACHE_DIR"
    fi
fi

# --- 3. enumerate Vulkan devices ---------------------------------------------
# --list-devices exits 0 and prints a header plus one line per device. A
# Vulkan build with no usable ICD prints "(none)" (or nothing) under the
# header; that is the CPU-fallback case, not an error.
devices_out=$("$LLAMA_SERVER" --list-devices 2>&1) \
    || fatal "--list-devices failed:
$devices_out"

device_lines=$(printf '%s\n' "$devices_out" \
    | sed -n 's/^[[:space:]]*\(Vulkan[0-9][0-9]*\):[[:space:]]*\(.*\)$/\1|\2/p')

device_count=$(printf '%s\n' "$device_lines" | grep -c . || true)

show_devices() {
    if [ "$device_count" -eq 0 ]; then
        echo "  (no Vulkan device was enumerated)" >&2
    else
        printf '%s\n' "$device_lines" | sed -e 's/^/  /' -e 's/|/: /' >&2
    fi
}

WANT="${FABULIST_SIDECAR_DEVICE:-}"
MATCH="${FABULIST_SIDECAR_DEVICE_MATCH:-}"

DEV=""
DEV_LINE=""

if [ -n "$WANT" ]; then
    # Accept both "Vulkan0" and a bare "0"; "cpu"/"none" ask for NO offload
    # (llama-server's own --device none: the models stay on the CPU even when
    # a GPU is visible — the mode the sidecar was measured for, spike #10).
    case "$WANT" in
        [0-9]*) WANT="Vulkan$WANT" ;;
    esac
    case "$WANT" in
        Vulkan[0-9]*) : ;;
        cpu|CPU|none|NONE) DEV="none" ;;
        *) fatal "FABULIST_SIDECAR_DEVICE='$WANT' is not a Vulkan device id (examples: Vulkan0, 0) or 'cpu'" ;;
    esac
    if [ "$DEV" != "none" ]; then
        DEV_LINE=$(printf '%s\n' "$device_lines" | grep "^$WANT|" || true)
        if [ -z "$DEV_LINE" ]; then
            echo "[sidecar] FATAL: FABULIST_SIDECAR_DEVICE=$WANT was requested but no such Vulkan device exists." >&2
            echo "[sidecar] devices this host exposes:" >&2
            show_devices
            exit 1
        fi
        DEV="$WANT"
    fi
elif [ -n "$MATCH" ]; then
    matches=$(printf '%s\n' "$device_lines" | grep -i -F -- "$MATCH" || true)
    match_count=$(printf '%s\n' "$matches" | grep -c . || true)
    if [ "$match_count" -eq 0 ]; then
        echo "[sidecar] FATAL: FABULIST_SIDECAR_DEVICE_MATCH='$MATCH' matched no Vulkan device." >&2
        echo "[sidecar] devices this host exposes:" >&2
        show_devices
        exit 1
    fi
    if [ "$match_count" -gt 1 ]; then
        echo "[sidecar] FATAL: FABULIST_SIDECAR_DEVICE_MATCH='$MATCH' matched $match_count Vulkan devices; be more specific or use FABULIST_SIDECAR_DEVICE=VulkanN." >&2
        printf '%s\n' "$matches" | sed -e 's/^/[sidecar]   /' -e 's/|/: /' >&2
        exit 1
    fi
    DEV_LINE="$matches"
    DEV=$(printf '%s' "$DEV_LINE" | cut -d'|' -f1)
elif [ "$device_count" -eq 1 ]; then
    DEV_LINE="$device_lines"
    DEV=$(printf '%s' "$DEV_LINE" | cut -d'|' -f1)
elif [ "$device_count" -gt 1 ]; then
    echo "[sidecar] FATAL: $device_count Vulkan devices are visible and no choice was made." >&2
    echo "[sidecar] Vulkan enumerates in its own order; refusing to guess which one you meant." >&2
    echo "[sidecar] devices this host exposes:" >&2
    show_devices
    echo "[sidecar] set FABULIST_SIDECAR_DEVICE=VulkanN (or FABULIST_SIDECAR_DEVICE_MATCH=<name substring>) in .env." >&2
    exit 1
fi

if [ "$DEV" = "none" ]; then
    log "CPU only (--device none): models stay on the CPU by request."
elif [ -n "$DEV" ]; then
    log "using Vulkan device: $DEV_LINE"
    free_mib=$(printf '%s' "$DEV_LINE" | sed -n 's/.*, *\([0-9][0-9]*\) MiB free).*/\1/p')
    if [ -n "$free_mib" ] && [ "$free_mib" -lt 4096 ]; then
        log "WARNING: $DEV reports only ${free_mib} MiB free; the three models need ~4.5 GiB resident (6 GiB comfortable)."
        log "WARNING: the router will start, but a model load may fail with an out-of-memory error."
    fi
else
    log "WARNING: no Vulkan device was found — falling back to CPU (this build's native behaviour)."
    log "WARNING: decision calls take several seconds and embedding indexing is ~100x slower on CPU."
fi

# --- 4. readiness marker ----------------------------------------------------
# The compose healthcheck is `test -f <marker> && curl /v1/models`. The marker
# is written ONLY when every model is loaded (warmup on) or immediately (warmup
# off, where the operator owns the cache). A stale marker from a previous run
# (`docker restart` keeps /tmp) is removed first, so a restart re-earns health.
mkdir -p "$READY_DIR" 2>/dev/null \
    || fatal "the readiness directory cannot be created: $READY_DIR

  The compose healthcheck reads $READY_FILE from it; a healthcheck that can
  never pass would hold the engine back forever."
rm -f "$READY_FILE"

mark_ready() {
    printf '%s\n' "$*" > "$READY_FILE" \
        || fatal "the readiness marker cannot be written: $READY_FILE"
}

# --- 5. warm every model (issue #64) ---------------------------------------
# One request per model to the ROUTER: POST /models/load. That is the step that
# fetches a model's HuggingFace weights and leaves it resident, and it is what
# the first turn would otherwise do mid-conversation. It is role-agnostic (a
# chat model, a local-weights preset and the shipped three all work), and it is
# fast when the weights are already cached: load from disk, no fetch.
#
# Idempotent by construction: a model already `loaded`/`loading` is skipped, and
# llama.cpp answers a redundant load with 400 "model is already running", which
# is treated as success. The wait is bounded by FABULIST_SIDECAR_WARMUP_TIMEOUT
# and every transition is logged, so a slow 2.7 GiB fetch is visible while it
# happens instead of looking like a hang.
#
# Prints progress to stdout (the container log). Exit status: 0 = every model
# loaded, non-zero = warmup failed or timed out (the caller keeps the container
# unhealthy; the router keeps serving, so this is not a crash-loop).
warm_models() {
    if [ -z "$*" ]; then
        log "warmup: the preset names no model sections, so there is nothing to warm."
        return 0
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        log "WARMUP FAILED: python3 is not present in this image, so the models cannot be warmed."
        log "WARMUP FAILED: set FABULIST_SIDECAR_WARMUP=off to serve un-warmed (the first request then pays the download)."
        return 1
    fi

    # `exec` so the backgrounded warmup IS this pid: the trap can then stop it
    # with one kill instead of orphaning a python child.
    exec python3 - "http://127.0.0.1:$PORT" "$WARMUP_TIMEOUT" "$READY_FILE" "$@" <<'PY'
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE, TIMEOUT_ARG, READY_FILE = sys.argv[1], sys.argv[2], sys.argv[3]
MODELS = sys.argv[4:]
TIMEOUT = float(TIMEOUT_ARG)
DEADLINE = time.monotonic() + TIMEOUT
POLL_SECONDS = 0.5
HEARTBEAT_SECONDS = 30.0


def log(message):
    print("[sidecar] " + message, flush=True)


def request(method, path, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8") or "{}")


def model_statuses():
    payload = request("GET", "/v1/models")
    return {
        str(entry.get("id")): str((entry.get("status") or {}).get("value") or "unknown")
        for entry in (payload.get("data") or [])
    }


def fail(message):
    log("WARMUP FAILED: " + message)
    try:
        os.unlink(READY_FILE)
    except OSError:
        pass
    sys.exit(1)


# 1. The router has to listen before it can be asked to load anything. A fresh
#    container is typically listening in ~2 s; the poll also absorbs a slow
#    Vulkan initialisation (bounded by the warmup timeout).
while True:
    try:
        model_statuses()
        break
    except (OSError, ValueError) as exc:
        if time.monotonic() >= DEADLINE:
            fail("the router never answered /v1/models within %.0f s (%s)" % (TIMEOUT, exc))
        time.sleep(0.5)

# 2. One load request per model — this is what fetches the weights.
statuses = model_statuses()
started = time.monotonic()
requested_at = {model: started for model in MODELS}
for model in MODELS:
    state = statuses.get(model)
    if state in ("loaded", "loading"):
        log("warmup: %s is already %s" % (model, state))
        continue
    try:
        request("POST", "/models/load", {"model": model})
        log("warming %s ..." % model)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        if exc.code == 400 and "already" in detail:
            log("warmup: %s is already loaded" % model)
            continue
        fail(
            "%s: POST /models/load was refused (HTTP %d): %s"
            % (model, exc.code, detail)
        )
    except (OSError, ValueError) as exc:
        fail("%s: POST /models/load failed: %s" % (model, exc))

# 3. Wait until every model reports `loaded`. The router moves a model through
#    unloaded -> loading -> loaded. A load that FAILS does not always end at
#    `failed`: probed on the pinned image, a bad hf-repo/hf-file spawns the
#    child, the child exits 1, and the router puts the model back to
#    `unloaded` (the error text lives in the server log). So a model that
#    started loading and fell back to `unloaded` is a FAILURE, as is one that
#    never started loading while nothing else is loading. A model that is
#    later unloaded by --sleep-idle-seconds is NOT a failure (it was loaded).
reported = {}
last_status = {model: "unknown" for model in MODELS}
ever_loading = set()
ever_loaded = set()
last_heartbeat = time.monotonic()
START_GRACE_SECONDS = 120.0


def failures(now):
    broken = {}
    for model in MODELS:
        state = last_status[model]
        if state in ("failed", "removed", "error"):
            broken[model] = "status " + state
        elif state == "unloaded" and model in ever_loading and model not in ever_loaded:
            broken[model] = "fell back to unloaded after it started loading"
    # A model that never even started, while the router demonstrably CAN load
    # (another one finished) and nothing is loading now: the request was
    # refused before the child got going. The `ever_loaded or ever_loading`
    # guard keeps the first seconds of a multi-model warmup from looking like
    # a failure.
    if not broken and "loading" not in last_status.values() and (ever_loaded or ever_loading):
        for model in MODELS:
            waited = now - requested_at[model]
            if (
                last_status[model] == "unloaded"
                and model not in ever_loading
                and model not in ever_loaded
                and waited > START_GRACE_SECONDS
            ):
                broken[model] = "never started loading (%.0f s after the load request)" % waited
    return broken


while True:
    now = time.monotonic()
    try:
        statuses = model_statuses()
        last_status = {model: statuses.get(model, "absent from /v1/models") for model in MODELS}
    except (OSError, ValueError) as exc:
        last_status = {model: "router error: %s" % exc for model in MODELS}

    for model in MODELS:
        state = last_status[model]
        if state == "loading":
            ever_loading.add(model)
        elif state == "loaded":
            ever_loaded.add(model)
        if reported.get(model) != state:
            log("%s: %s" % (model, state))
            reported[model] = state

    broken = failures(now)
    if broken:
        for model, why in broken.items():
            log("WARMUP FAILED: %s could not be loaded (%s)." % (model, why))
        fail(
            "the reason is in the server log above (HuggingFace fetch, disk space, or out of memory); "
            "fix it and restart this container — the engine is NOT started against a cold sidecar."
        )

    if all(last_status[model] == "loaded" for model in MODELS):
        try:
            with open(READY_FILE, "w") as handle:
                json.dump(
                    {"warmed": MODELS, "seconds": round(time.monotonic() - started, 1)},
                    handle,
                )
                handle.write("\n")
        except OSError as exc:
            fail("every model is warm but the readiness marker cannot be written: %s" % exc)
        log(
            "warmup complete in %.1f s: %s — every model is resident; the first turn pays no download."
            % (time.monotonic() - started, ", ".join(MODELS))
        )
        sys.exit(0)

    if now >= DEADLINE:
        fail(
            "warmup timed out after %.0f s: %s. The download may simply be slow — raise "
            "FABULIST_SIDECAR_WARMUP_TIMEOUT (and compose's healthcheck start_period) if so."
            % (TIMEOUT, ", ".join("%s=%s" % (m, last_status[m]) for m in MODELS))
        )

    if now - last_heartbeat >= HEARTBEAT_SECONDS and DEADLINE - now > HEARTBEAT_SECONDS:
        log(
            "warmup still running (%.0f s): %s"
            % (now - started, ", ".join("%s=%s" % (m, last_status[m]) for m in MODELS))
        )
        last_heartbeat = now

    time.sleep(POLL_SECONDS)
PY
}

# --- 6. start llama-server, warm it, supervise it --------------------------
log "preset=$PRESET models-max=$MODELS_MAX sleep-idle=${SLEEP_IDLE}s port=$PORT"

start_server() {
    if [ -n "$DEV" ]; then
        "$LLAMA_SERVER" \
            --models-preset "$PRESET" \
            --models-max "$MODELS_MAX" \
            --sleep-idle-seconds "$SLEEP_IDLE" \
            --device "$DEV" \
            --host 0.0.0.0 \
            --port "$PORT" \
            --metrics &
    else
        "$LLAMA_SERVER" \
            --models-preset "$PRESET" \
            --models-max "$MODELS_MAX" \
            --sleep-idle-seconds "$SLEEP_IDLE" \
            --host 0.0.0.0 \
            --port "$PORT" \
            --metrics &
    fi
    SERVER_PID=$!
}

# Forward the container's stop signal to both children. Without this the sh
# would swallow SIGTERM and Docker would have to SIGKILL after the grace period.
shutdown() {
    if [ -n "$WARM_PID" ]; then
        kill "$WARM_PID" 2>/dev/null || true
    fi
    if [ -n "$SERVER_PID" ]; then
        kill -TERM "$SERVER_PID" 2>/dev/null || true
    fi
}
trap 'shutdown; exit 143' TERM INT
trap 'shutdown' EXIT

if [ "$WARMUP" = "off" ]; then
    # Opt-out (pre-seeded cache): health means "the router answers", exactly as
    # before issue #64, and the first request for a model pays the download.
    # The marker is written at once — the healthcheck's /v1/models probe is what
    # waits for the router.
    mark_ready '{"warmed": [], "skipped": "FABULIST_SIDECAR_WARMUP=off"}'
    log "warmup disabled (FABULIST_SIDECAR_WARMUP=off): serving now; a model's first request pays the download."
fi

start_server

if [ "$WARMUP" = "on" ]; then
    # Beside the server, so one stop signal reaches both; the foreground job is
    # the server itself, so the container's life is the server's life.
    warm_models $model_ids &
    WARM_PID=$!
fi

set +e
wait "$SERVER_PID"
status=$?
set -e
log "llama-server exited with status $status"
exit "$status"
