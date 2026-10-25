#!/usr/bin/env python3
"""Smoke test for the Fabulist models sidecar (issue #18).

Runs against a live llama.cpp router serving the three models in
``deploy/sidecar/models.ini`` and checks the contract the engine actually
relies on:

  0. the three weights are HuggingFace references, so the FIRST request for a
     model downloads it (~2.7 GiB in total on a fresh cache) and a SECOND run
     serves it from the cache without re-downloading. The per-request timeout
     is generous for that reason (``--timeout``);
  1. the sidecar answers, and ``/v1/models`` lists all three model ids
     (``qwen3-reranker``, ``embeddinggemma-2``, ``d1-3b``);
  2. one ``/v1/rerank`` call returns sorted, differentiated scores for 10
     documents, AND the server log for that call contains ``truncated = 0`` on
     every sequence and no ``truncated = 1``. That second half is the point:
     the shipped reranker runs at ``ctx-size = 2048``, which is sufficient only
     because the engine clips each candidate to 600 chars (one ~222-token
     query+document pair per sequence). A future change to the clip length must
     fail this test instead of silently truncating documents;
  3. one ``/v1/embeddings`` call returns unit-norm vectors of one dimension
     (pins ``pooling = mean`` + ``embd-normalize = 2``, the owner's flags);
  4. one ``/v1/systemone`` call against ``d1-3b`` returns a ``noul``
     probability — the native decision route, not a chat completion;
  5. the decision model ADVERTISES that route (``output_modalities:
     ["decisions"]``). This check runs LAST, after the models have been
     loaded: the router resolves a preset's capabilities from its GGUF, offline,
     so on a FRESH cache — where the weights do not exist yet — ``/v1/models``
     reports the default ``["text"]`` until the first request has pulled and
     loaded the file. Checking before the downloads would fail for a reason
     that is not a regression.

Usage (compose, from the repo root — the default compose file ships the
sidecar and publishes it on 127.0.0.1:8445):

    uv run python scripts/sidecar_smoke.py

Usage (something else serving the preset, host mapping or a local binary):

    uv run python scripts/sidecar_smoke.py \\
        --base-url http://127.0.0.1:4455 --log-file /tmp/sidecar.log

The truncated assertion reads the server's stdout, because llama.cpp does not
carry a per-document ``truncated`` field in the rerank response. Supply it three
ways, in order of precedence:

  --log-file PATH   read a captured file (``... > /tmp/sidecar.log``);
  --log-cmd CMD     run a command whose stdout is the log, e.g.
                    ``--log-cmd "docker compose logs --no-color fabulist-models"``
                    (use this for multi-file compose runs or a remote host);
  --compose-file F  sugar for ``--log-cmd "docker compose -f F logs --no-color
                    fabulist-models"``; defaults to docker-compose.yml when that
                    file is in the current directory.

There is deliberately no "skip the log check" flag.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

EXPECTED_MODELS = ("qwen3-reranker", "embeddinggemma-2", "d1-3b")

# The recall-shaped candidates and query from the liveness probe: role-tagged
# exactly as build_memory_block() renders them ("- [<layer>] <text>"), the true
# match at index 2, a lexical distractor at index 0.
DOCS = [
    "- [user] I told Tessa the lantern oil was running low and she laughed, saying we would buy more in the morning market before the stalls closed.",
    "- [narrative] The rain hammered the tin roof of the Wayfarer's Rest while Marta counted coins behind the bar, her knife never far from her hand.",
    "- [Tessa] I remember the morning he gave me the brass compass. I still keep it in the left pocket of my coat, and I have never once opened it.",
    "- [Wanderer] I crossed the salt flats alone with two skins of water and a broken ankle; the sun there does not forgive hesitation.",
    "- [narrative] Captain Reyes drew the harbour chart across the table, tracing the reef line with one finger and saying nothing about the missing boat.",
    "- [user] I asked the smith to reforge the broken sabre and he quoted three silver, then spat and said two if I helped at the bellows.",
    "- [narrative] Snow gathered on the sill of the tower window; below, the garrison drilled in silence, their breath rising like a slow tide.",
    "- [Marta] The cellar key stays on the nail behind the bar. Nobody but me and the boy has touched it since the fire, and the boy is dead.",
    "- [narrative] The market at Duncairn smelled of fish and tar; a one-eyed woman sold charms that she swore would keep a sailor dry.",
    "- [user] I promised the child I would come back for the wooden horse, and I have not forgotten it, even now.",
]

RERANK_QUERY = "Where did I leave the brass compass Tessa gave me, and did I ever open it?"
RERANK_TRUE_INDEX = 2

FAILURES: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    """Record one assertion; print it as it happens."""
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def post_json(base_url: str, path: str, payload: dict, timeout: float) -> tuple[int, dict | str, float]:
    """POST a JSON body; return (status, parsed body or raw error text, ms)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, f"{type(exc).__name__}: {exc}", (time.perf_counter() - started) * 1000
    elapsed = (time.perf_counter() - started) * 1000
    try:
        return status, json.loads(raw), elapsed
    except json.JSONDecodeError:
        return status, raw.decode("utf-8", "replace"), elapsed


def get_json(base_url: str, path: str, timeout: float) -> tuple[int, dict | str]:
    req = urllib.request.Request(base_url.rstrip("/") + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def read_sidecar_log(args: argparse.Namespace) -> str:
    """Return the sidecar's stdout as text, or raise SystemExit with the fix."""
    if args.log_file:
        path = Path(args.log_file)
        if not path.is_file():
            raise SystemExit(f"error: --log-file does not exist: {path}")
        return path.read_text(encoding="utf-8", errors="replace")
    if args.log_cmd:
        cmd = shlex.split(args.log_cmd)
    elif args.compose_file:
        cmd = [
            "docker",
            "compose",
            "-f",
            args.compose_file,
            "logs",
            "--no-color",
            args.service,
        ]
    else:
        raise SystemExit(
            "error: the rerank `truncated == 0` assertion needs the sidecar's log.\n"
            "  compose:      --compose-file docker-compose.yml (the default if that file is here)\n"
            "  multi-file:   --log-cmd \"docker compose -f docker-compose.yml -f override.yml logs fabulist-models\"\n"
            "  local server: redirect its stdout to a file and pass --log-file PATH"
        )
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"error: cannot read sidecar logs ({' '.join(shlex.quote(c) for c in cmd)}): {exc}")
    if proc.returncode != 0:
        raise SystemExit(
            "error: the log command failed (is the stack up?):\n"
            f"{proc.stderr.strip()[:800]}"
        )
    return proc.stdout


def smoke_models(base_url: str, timeout: float) -> None:
    status, body = get_json(base_url, "/v1/models", timeout)
    if status != 200 or not isinstance(body, dict):
        check(False, "GET /v1/models", f"HTTP {status}: {str(body)[:300]}")
        return
    entries = {
        m.get("id"): m
        for m in body.get("data", [])
        if isinstance(m, dict) and m.get("id")
    }
    missing = [m for m in EXPECTED_MODELS if m not in entries]
    check(not missing, "three model ids listed", f"{sorted(entries)}" + (f" | missing {missing}" if missing else ""))
    for model_id in EXPECTED_MODELS:
        entry = entries.get(model_id, {})
        state = entry.get("status", {}).get("value", "?")
        arch = entry.get("architecture") or {}
        out = arch.get("output_modalities")
        print(f"       {model_id}: status={state} output_modalities={out}")


def smoke_decision_modality(base_url: str, timeout: float) -> None:
    """After a load, the decision model advertises its native route.

    Read from the GGUF, offline: a cold cache cannot know it before the file
    exists, which is why this runs after the three requests instead of before.
    """
    status, body = get_json(base_url, "/v1/models", timeout)
    if status != 200 or not isinstance(body, dict):
        check(False, "GET /v1/models (after load)", f"HTTP {status}: {str(body)[:300]}")
        return
    decision = next(
        (
            m.get("architecture") or {}
            for m in body.get("data", [])
            if isinstance(m, dict) and m.get("id") == "d1-3b"
        ),
        {},
    )
    check(
        "decisions" in (decision.get("output_modalities") or []),
        "d1-3b advertises the decisions modality",
        str(decision.get("output_modalities")),
    )


def smoke_rerank(base_url: str, timeout: float, args: argparse.Namespace) -> None:
    # Read the log before the call, so the truncated assertion covers exactly
    # this request (a warm model and a cold load both emit the slot lines).
    before = read_sidecar_log(args)
    status, body, elapsed = post_json(
        base_url,
        "/v1/rerank",
        {"model": "qwen3-reranker", "query": RERANK_QUERY, "documents": DOCS},
        timeout,
    )
    if status != 200 or not isinstance(body, dict):
        check(False, "POST /v1/rerank", f"HTTP {status}: {str(body)[:400]}")
        return
    results = body.get("results") or []
    check(len(results) == len(DOCS), "rerank returned one row per document", f"{len(results)} rows, {elapsed:.0f} ms")
    scores = [r.get("relevance_score", r.get("score")) for r in results]
    numeric = all(isinstance(s, (int, float)) for s in scores)
    check(numeric, "rerank scores are numeric", f"{len(set(scores))} distinct")
    if not numeric or len(scores) != len(DOCS):
        return
    check(scores == sorted(scores, reverse=True), "rerank scores sorted descending")
    check(scores[0] > 0.5, "top score is a confident match", f"top={scores[0]:.6f}")
    check(
        results[0].get("index") == RERANK_TRUE_INDEX,
        "the true-match document ranks first",
        f"top index={results[0].get('index')} (expected {RERANK_TRUE_INDEX})",
    )

    # The ctx-2048 safety margin: one sequence per candidate, none truncated.
    after = read_sidecar_log(args)
    delta = after[len(before) :] if after.startswith(before) else after
    trunc1 = [line for line in delta.splitlines() if "truncated = 1" in line]
    trunc0 = [line for line in delta.splitlines() if "truncated = 0" in line]
    check(not trunc1, "no rerank sequence was truncated", f"{len(trunc1)} truncated=1 line(s)")
    check(
        len(trunc0) >= len(DOCS),
        "every rerank sequence reports truncated = 0",
        f"{len(trunc0)} truncated=0 line(s) for {len(DOCS)} documents",
    )
    if not delta.strip():
        check(False, "sidecar log captured for the rerank call", "no new log lines — wrong --log-file or --service?")


def smoke_embeddings(base_url: str, timeout: float) -> None:
    status, body, elapsed = post_json(
        base_url,
        "/v1/embeddings",
        {"model": "embeddinggemma-2", "input": ["The rain hammered the tin roof.", "Marta counted coins."]},
        timeout,
    )
    if status != 200 or not isinstance(body, dict):
        check(False, "POST /v1/embeddings", f"HTTP {status}: {str(body)[:400]}")
        return
    rows = body.get("data") or []
    check(len(rows) == 2, "embeddings returned two vectors", f"{len(rows)} rows, {elapsed:.0f} ms")
    vectors = [r.get("embedding") for r in rows if isinstance(r, dict)]
    if len(vectors) != 2 or not all(isinstance(v, list) for v in vectors):
        check(False, "embeddings carry vectors")
        return
    dims = {len(v) for v in vectors}
    check(len(dims) == 1 and 0 < next(iter(dims)), "vectors share one non-zero dimension", f"dims={sorted(dims)}")
    norms = [sum(float(x) * float(x) for x in v) ** 0.5 for v in vectors]
    check(
        all(0.98 <= n <= 1.02 for n in norms),
        "vectors are unit-normalized (embd-normalize = 2)",
        f"norms={[round(n, 4) for n in norms]}",
    )


def smoke_decision(base_url: str, timeout: float) -> None:
    status, body, elapsed = post_json(
        base_url,
        "/v1/systemone",
        {
            "model": "d1-3b",
            "state": "Marta keeps the cellar key on a nail behind the bar. The boy who used to touch it is dead.",
            "questions": {
                "protective_arrangement": {
                    "type": "noul",
                    "instructions": "Does the state describe a protective arrangement?",
                }
            },
        },
        timeout,
    )
    if status != 200 or not isinstance(body, dict):
        check(False, "POST /v1/systemone (d1-3b)", f"HTTP {status}: {str(body)[:400]}")
        return
    answers = body.get("answers") or {}
    answer = answers.get("protective_arrangement") or {}
    value = answer.get("noul")
    check(
        isinstance(value, (int, float)) and 0.0 <= float(value) <= 1.0,
        "d1-3b answered with a noul probability",
        f"noul={value} ({elapsed:.0f} ms)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-test the Fabulist models sidecar (three models, one router).",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8445",
        help="router root (default: %(default)s — the compose host mapping)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="sidecar stdout captured to a file (required unless --compose-file reads it)",
    )
    parser.add_argument(
        "--log-cmd",
        default=None,
        help="command whose stdout is the sidecar log, e.g. "
        "'docker compose logs --no-color fabulist-models' "
        "(precedence: --log-file, --log-cmd, --compose-file)",
    )
    parser.add_argument(
        "--compose-file",
        default=None,
        help="compose file whose sidecar service logs are read for the truncated assertion "
        "(default: docker-compose.yml when it exists in the current directory)",
    )
    parser.add_argument(
        "--service",
        default="fabulist-models",
        help="compose service name for --compose-file logs (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="per-request timeout in seconds; a cold model load on Vulkan is seconds, not ms (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    if not args.log_file and not args.log_cmd and not args.compose_file and Path("docker-compose.yml").is_file():
        args.compose_file = "docker-compose.yml"
    if not args.log_file and not args.log_cmd and not args.compose_file:
        parser.error(
            "no log source: pass --log-file PATH, --log-cmd CMD or --compose-file FILE "
            "(the truncated assertion is mandatory)"
        )

    print(f"sidecar smoke test -> {args.base_url}")
    if args.log_file:
        log_source = args.log_file
    elif args.log_cmd:
        log_source = args.log_cmd
    else:
        log_source = f"compose:{args.compose_file} service:{args.service}"
    print(f"log source: {log_source}")
    smoke_models(args.base_url, args.timeout)
    smoke_rerank(args.base_url, args.timeout, args)
    smoke_embeddings(args.base_url, args.timeout)
    smoke_decision(args.base_url, args.timeout)
    smoke_decision_modality(args.base_url, args.timeout)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("OK — sidecar reachable, three models served, rerank/embedding/decision contracts hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
