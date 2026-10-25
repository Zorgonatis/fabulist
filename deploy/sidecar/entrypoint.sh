#!/bin/sh
# Fabulist sidecar entrypoint: check the preset, resolve the Vulkan device, then
# exec llama-server.
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
# the router spawns that model's child on its first request. The boot check is
# therefore shaped to the source:
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
# Env overrides (defaults match docker-compose.yml):
#   LLAMA_SERVER                 /app/llama-server
#   FABULIST_SIDECAR_PRESET      /etc/fabulist/models.ini
#   FABULIST_SIDECAR_PORT        8080
#   FABULIST_SIDECAR_MODELS_MAX  3
#   FABULIST_SIDECAR_SLEEP_IDLE_SECONDS  300

set -eu

LLAMA_SERVER="${LLAMA_SERVER:-/app/llama-server}"
PRESET="${FABULIST_SIDECAR_PRESET:-/etc/fabulist/models.ini}"
PORT="${FABULIST_SIDECAR_PORT:-8080}"
MODELS_MAX="${FABULIST_SIDECAR_MODELS_MAX:-3}"
SLEEP_IDLE="${FABULIST_SIDECAR_SLEEP_IDLE_SECONDS:-300}"

log() { echo "[sidecar] $*"; }
fatal() {
    echo "[sidecar] FATAL: $*" >&2
    exit 1
}

[ -x "$LLAMA_SERVER" ] || fatal "llama-server not found or not executable: $LLAMA_SERVER"
[ -r "$PRESET" ] || fatal "models preset not readable: $PRESET"

# --- 1. preset sources: HF refs by FORM, local paths by EXISTENCE -----------
# Why both: docker-compose.yml ships HF refs (weights arrive on first request),
# while a deployment that keeps GGUFs on disk binds its own preset and names
# paths under /models. The same script has to fail usefully for both, and it
# must never report an hf ref as a missing file.
problems=""
hf_sections=""
local_sections=""

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

  hf-repo/hf-file are HuggingFace references: they are downloaded on the
  model's FIRST REQUEST, so nothing has to exist on disk for them.
  model/mmproj are local paths: they are read from the read-only mount at
  /models (\${FABULIST_MODELS_DIR:-./models} on the host) and must exist.
  See README.md > Docker > Models sidecar."
fi

if [ -n "$hf_sections" ]; then
    log "downloads on first request (HuggingFace):$hf_sections"
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

  The first request downloads into it; a read-only or root-owned mount fails
  there instead of here, so this is a boot error. docker-compose.yml backs
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
    # Accept both "Vulkan0" and a bare "0"; reject anything else loudly.
    case "$WANT" in
        [0-9]*) WANT="Vulkan$WANT" ;;
    esac
    case "$WANT" in
        Vulkan[0-9]*) : ;;
        *) fatal "FABULIST_SIDECAR_DEVICE='$WANT' is not a Vulkan device id (examples: Vulkan0, 0)" ;;
    esac
    DEV_LINE=$(printf '%s\n' "$device_lines" | grep "^$WANT|" || true)
    if [ -z "$DEV_LINE" ]; then
        echo "[sidecar] FATAL: FABULIST_SIDECAR_DEVICE=$WANT was requested but no such Vulkan device exists." >&2
        echo "[sidecar] devices this host exposes:" >&2
        show_devices
        exit 1
    fi
    DEV="$WANT"
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

if [ -n "$DEV" ]; then
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

# --- 4. hand over to llama-server -------------------------------------------
log "preset=$PRESET models-max=$MODELS_MAX sleep-idle=${SLEEP_IDLE}s port=$PORT"

if [ -n "$DEV" ]; then
    exec "$LLAMA_SERVER" \
        --models-preset "$PRESET" \
        --models-max "$MODELS_MAX" \
        --sleep-idle-seconds "$SLEEP_IDLE" \
        --device "$DEV" \
        --host 0.0.0.0 \
        --port "$PORT" \
        --metrics
fi

exec "$LLAMA_SERVER" \
    --models-preset "$PRESET" \
    --models-max "$MODELS_MAX" \
    --sleep-idle-seconds "$SLEEP_IDLE" \
    --host 0.0.0.0 \
    --port "$PORT" \
    --metrics
