#!/usr/bin/env python3
"""Re-spike: hardened typed-question shapes on the d1-3b decision model.

Owner directive under test: *"can we harden the typed questions? (does the
content include any of the phrases from this list …)"* plus the named LATER
quality gate *"does this turn sufficiently drive narrative forward?"*. This
script measures whether hardened question shapes separate good from bad input
RELIABLY. It wires NOTHING.

It reuses the wire client and the ``noul``/``choice`` renderers from the sibling
spike ``scripts/spike/d1_systemone.py`` (loaded by path; that module is not a
package member). Only the fixture bank, the batched question map and this
report's bookkeeping are new.

Three probe families:

1. **Slop-phrase membership.** Three formulations — (a) a BATCHED MAP, one
   ``noul`` question per banned phrase; (b) a SINGLE ``noul`` with the whole
   list in ``instructions`` + criteria; (c) a CHOICE over slop categories with
   sign-lists and a gated ``none`` escape — against three fixtures: clean prose,
   slop-laden prose (5 phrases), and borderline prose (1 mild phrase).
   The banned phrases are re-used, not curated: see :data:`SLOP_SOURCE`.

2. **Narrative movement** (the later quality-gate shape). One ``noul`` question
   built from the critic's own "Lack of movement" definition
   (``src/fabulist/prompts/critic.py``), run 3x per fixture on a stalled recap,
   an advancing turn, and a mood-only turn — the spread matters as much as the
   separation (the critic-verdict kill was about spread).

3. **Nested-criteria sanity.** Three cheap probes: a ``noul`` with a criteria
   dict, a two-question map, and an empty questions map (expect a clean 400).

Every probe prints its ``expect`` line so a surprise is visible. No threshold is
applied and nothing is written; the live answers are the result.

Run:
    uv run python scripts/spike/d1_respike.py
    uv run python scripts/spike/d1_respike.py --family movement --runs 3
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from typing import NamedTuple

# ---------------------------------------------------------------------------
# reuse the sibling spike's client + renderers (loaded by path, not by import)
# ---------------------------------------------------------------------------


def _load_sibling(module_name: str):
    """Load ``scripts/spike/<module_name>.py`` by path (the d1 spike pattern)."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, module_name + ".py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_d1 = _load_sibling("d1_systemone")

#: The shared wire client + error type from the first spike.
post = _d1.post
ProbeError = _d1.ProbeError

#: The shared answer renderers from the first spike.
format_noul = _d1.format_noul
format_choice = _d1.format_choice
print_probabilities = _d1.print_probabilities


def build_body(state: str, questions: dict) -> dict:
    """The ``/v1/systemone`` body (without the router's ``model`` field).

    Unlike the sibling spike's ``build_body`` this takes the whole question map,
    so one request can carry a batched map of many questions.
    """
    return {"state": state, "questions": questions}


def slugify(phrase: str) -> str:
    """A stable question id for a phrase: lowercase, non-alphanumerics -> ``_``."""
    return re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")


# ---------------------------------------------------------------------------
# the banned-phrase bank (RE-USED, not curated)
# ---------------------------------------------------------------------------

#: The list is re-used from the engine, not invented here. The engine's compiled
#: list is authoritative for the ``slop`` validator check and cites its own
#: provenance: ``src/fabulist/graph/validator.py``
#: ``DEFAULT_SLOP_PATTERNS`` (``SlopPattern``, distilled from the preset's
#: ``Writing Guidelines (Anti-Slop)``). The preset source of record is
#: ``~/Projects/Stabs-EDH/Stabs-GLM5.1-Directives-v4.0.0.json`` ``prompts/15``
#: ("Writing Guidelines (Anti-Slop)" == ``validator.PRESET_ANTISLOP_LINE`` 207).
SLOP_SOURCE = (
    "validator.DEFAULT_SLOP_PATTERNS (src/fabulist/graph/validator.py) <- "
    "Stabs-GLM5.1-Directives-v4.0.0.json prompts/15 (Anti-Slop, line 207)"
)


class SlopPhrase(NamedTuple):
    """One banned phrase with its engine rule id and fixture-match keywords."""

    rule_id: str
    phrase: str
    keywords: tuple[str, ...]  # lowercase substrings that count as the phrase
    section: str


#: 18 canonical phrases spanning the engine sections, each traceable to one
#: ``DEFAULT_SLOP_PATTERNS`` rule (same ``rule_id``). 18 is deliberate: it is the
#: batch size the batching-cost check is asked about.
SLOP_PHRASES: tuple[SlopPhrase, ...] = (
    SlopPhrase("shivers_down", "shivers down her spine", ("shiver",), "Emotion"),
    SlopPhrase("air_charged", "the air was thick with", ("air was thick", "air grew thick", "air was heavy", "air charging"), "Emotion"),
    SlopPhrase("breath_catching", "breath catching", ("breath catching", "breath caught in"), "Emotion"),
    SlopPhrase("torn_between", "torn between", ("torn between",), "Emotion"),
    SlopPhrase("world_narrowing", "the world narrowed", ("world narrowed", "world narrowing"), "Emotion"),
    SlopPhrase("predatory_grin", "predatory grin", ("predatory grin", "predatory smirk"), "Physical"),
    SlopPhrase("analytical_gaze", "analytical gaze", ("analytical gaze",), "Physical"),
    SlopPhrase("barely_whisper", "barely a whisper", ("barely a whisper", "barely whispered"), "Voice"),
    SlopPhrase("pregnant_pause", "pregnant pause", ("pregnant pause",), "Movement"),
    SlopPhrase("silence_stretched", "the silence stretched", ("silence stretched", "silence stretching"), "Movement"),
    SlopPhrase("long_moment", "for a long moment", ("for a long moment",), "Movement"),
    SlopPhrase("did_not_look_back", "didn't look back", ("didn't look back", "did not look back"), "Movement"),
    SlopPhrase("economical_movements", "economical movements", ("economical movements", "economical movement"), "Movement"),
    SlopPhrase("stark_contrast", "stark contrast", ("stark contrast",), "Misc"),
    SlopPhrase("night_is_young", "the night is young", ("night is young",), "Misc"),
    SlopPhrase("playful_challenge", "playful challenge", ("playful challenge",), "Misc"),
    SlopPhrase("everything_else_faded", "everything else faded", ("everything else faded", "everything else ceased"), "Patterns"),
    SlopPhrase("soft_blur", "a soft blur", ("soft blur",), "Patterns"),
)

#: Ground-truth phrase ids each fixture is built to contain.
SLOP_FIXTURE_TRUTH = {
    "clean": (),
    "slop": ("shivers_down", "air_charged", "long_moment", "pregnant_pause", "predatory_grin"),
    "borderline": ("soft_blur",),
}


def phrase_hits(text: str) -> list[tuple[str, str]]:
    """``[(rule_id, matched_keyword), …]`` for phrases present in ``text``.

    Pure helper, model-free: the fixtures' ground truth.
    """
    low = text.lower()
    hits: list[tuple[str, str]] = []
    for phrase in SLOP_PHRASES:
        for keyword in phrase.keywords:
            if keyword in low:
                hits.append((phrase.rule_id, keyword))
                break
    return hits


#: slug(phrase) -> engine rule id, so map answers map back to ground truth.
SLUG_TO_RULE: dict[str, str] = {slugify(p.phrase): p.rule_id for p in SLOP_PHRASES}


# ---------------------------------------------------------------------------
# family 1 fixtures — competent RP prose in the spike's Marwold universe
# ---------------------------------------------------------------------------

CLEAN_PROSE = """Mireille set the envelope on the desk and did not open it. Rain moved across the window in slow sheets, warping the lamp light. Aldric watched her hands instead of the paper: the right one steady, the left held close to her coat. "You rode through the whole of it," he said. "The valley road floods this time of year." She nodded once. "The horse knows the way." He poured two fingers of rye and pushed one glass across the wood. She took it, drank, and set it down with a small click. For a while neither of them spoke. Then she said, "Marek is dead." The words landed in the room and stayed there, taking up the space between the glasses. Aldric did not move. Outside, a cart wheel turned in the mud, and the sound went on without them."""

SLOP_PROSE = """Mireille did not answer at once. A shiver ran down her spine that had nothing to do with the cold, and she set her glass down before her hand could betray her. The air was thick with everything neither of them had said. For a long moment Aldric only watched her, turning the envelope over in his fingers. A pregnant pause opened between them, wide enough to fall into. Then he smiled, a slow predatory grin, and leaned back in his chair as if he had already won. "Say it," he said. "Or I will." The lamp guttered. Outside, the rain kept on without either of them, and the clock in the hall counted past the hour."""

BORDERLINE_PROSE = """Aldric read the note twice and set it down. The kitchen smelled of bread and wet wool; the fire had burned low in the grate. "She wants the ledger back," he said. Mireille did not look up from the stove. "Of course she does." Outside, the yard was quiet, the puddles holding a pale copy of the sky. For a moment the only sound was the kettle working toward a boil. He watched the room dissolve into a soft blur at the edges and blinked it back. "I won't give it to her," he said. Mireille turned then, and the spoon in her hand went still. "Then we should decide what we tell Marek." """

SLOP_FIXTURES: tuple[tuple[str, str, str], ...] = (
    ("clean", CLEAN_PROSE, "none of the 18 phrases -> every check false / none"),
    ("slop", SLOP_PROSE, "the 5 listed phrases -> every check true / a slop class"),
    ("borderline", BORDERLINE_PROSE, "one mild phrase (a soft blur) -> record it"),
)


# ---------------------------------------------------------------------------
# family 1 formulations
# ---------------------------------------------------------------------------


def build_slop_map_questions() -> dict:
    """(a) BATCHED MAP: one ``noul`` question per phrase; id = slug(phrase)."""
    questions = {}
    for phrase in SLOP_PHRASES:
        questions[slugify(phrase.phrase)] = {
            "type": "noul",
            "instructions": (
                'Does the text contain the exact phrase "%s" or a trivial variant '
                "of it?" % phrase.phrase
            ),
        }
    return questions


def build_slop_single_question() -> dict:
    """(b) SINGLE ``noul`` with the whole list in ``instructions`` + criteria."""
    listed = "; ".join('"%s"' % p.phrase for p in SLOP_PHRASES)
    return {
        "slop_any": {
            "type": "noul",
            "instructions": (
                "Does the text contain any of these banned phrases, verbatim or "
                "near-verbatim: %s?" % listed
            ),
            "criteria": {
                "true": "One or more listed phrases appears verbatim or near-verbatim.",
                "false": "No listed phrase appears.",
            },
        }
    }


#: (c) CHOICE categories: sign-lists per section, with the escape gated last.
SLOP_CHOICE_CRITERIA = {
    "emotion": (
        "Stock inner-state filler: shivers down the spine, the air thick with "
        "feeling, torn between, a breath catching, the world narrowing."
    ),
    "physical": (
        "Stock body-language filler: a predatory grin, an analytical gaze, pupils "
        "blown, a face that is a mask of something."
    ),
    "voice": (
        "Stock voice filler: a voice like silk or gravel or velvet, a purr, a "
        "growl, a rumble, barely a whisper."
    ),
    "movement": (
        "Stock staging filler: a pregnant pause, a silence stretching, for a long "
        "moment, didn't look back, economical movements."
    ),
    "none": "No stock phrase above appears. Use only as a last resort.",
}


def build_slop_choice_question() -> dict:
    """(c) CHOICE over slop categories with a last-resort ``none`` escape."""
    return {
        "slop_class": {
            "type": "choice",
            "instructions": (
                "Name the class of stock prose filler this text uses. One clear "
                "sign is enough. Answer 'none' only when no sign below appears."
            ),
            "criteria": dict(SLOP_CHOICE_CRITERIA),
        }
    }


# ---------------------------------------------------------------------------
# family 2 — narrative movement (the critic's own definition)
# ---------------------------------------------------------------------------

MOVEMENT_QUESTION = {
    "type": "noul",
    "instructions": "Does this turn drive the narrative forward?",
    "criteria": {
        "true": (
            "Something changes: new information surfaces, a consequence lands, a "
            "decision point opens, or the situation materially shifts."
        ),
        "false": (
            "The turn only restates what is known, replays an emotion, or holds "
            "the same standoff — nothing changes."
        ),
    },
}

STALLED_TURN = """Aldric did not answer at once. The study was as it had been for three weeks: the same cold tea, the same sealed envelope, the same rain working at the window. He remembered Vessin in the winter, when the ledger still sat in Marek's strongroom and the guild still pretended to audit itself, and the spring after, when the surveyors came down the valley and asked their reasonable questions. He remembered the fire at the east warehouse, which the guilds called an accident. He turned the envelope over. It was still sealed. Outside, the rain kept on. Mireille watched him from the door and said nothing, and the silence held exactly as it had held the day before."""

ADVANCING_TURN = """Mireille crossed the study and broke the seal before Aldric could stop her. Inside was not the ledger but a single sheet in Marek's hand, dated the morning of the warehouse fire, naming the two men who had paid for it. Aldric read it twice. "He kept a copy," he said. "Of course he kept a copy." She folded the sheet and put it in her coat. "Then he knows we have it, which means he knows we can use it." Down in the street, a cab stopped and did not move on. Neither of them went to the window. "We have until morning," Mireille said, "before he decides we are a problem he can solve." Aldric reached for his coat. "Then we don't wait for morning." """

MOOD_TURN = """Rain came off the river in long grey lines, and the house at Marwold settled into itself, board by board. The lamp in the study threw the same yellow circle it threw every evening; the fire had gone to embers an hour ago and no one had fed it. Somewhere above, a shutter worked loose in the wind and tapped against the frame, patient as a clock. The smell of wet stone came in under the door. It was the kind of evening that made the valley feel older than the town in it, and the town older than the men who argued over it. Nothing in the room moved except the shadows, and they moved the way shadows do when the wick is low."""

MOVEMENT_FIXTURES: tuple[tuple[str, str, str], ...] = (
    ("stalled", STALLED_TURN, "false — nothing changes, the scene is restated"),
    ("advancing", ADVANCING_TURN, "true — a reveal lands and a decision opens"),
    ("mood-only", MOOD_TURN, "borderline — atmosphere only, no movement; record it"),
)


# ---------------------------------------------------------------------------
# family 3 — nested-criteria sanity (cheap)
# ---------------------------------------------------------------------------

SANITY_NOUL_QUESTION = {
    "repair": {
        "type": "noul",
        "instructions": "Does this turn need repair?",
        "criteria": {
            "true": "The prose contains a repair-worthy defect.",
            "false": "The prose contains no repair-worthy defect.",
        },
    }
}

SANITY_TWO_QUESTION_MAP = {
    "needs_repair": {
        "type": "noul",
        "instructions": "Does this turn need repair?",
    },
    "advances": {
        "type": "noul",
        "instructions": "Does this turn drive the narrative forward?",
    },
}

SANITY_STATE = """<turn index="41">
<prose>
1 Mireille set the envelope on the desk and did not open it.
2 "Marek is dead," she said. The words landed in the room and stayed there.
</prose>
<state_line>time: 18:40 | loc: study | tone: quiet</state_line>
</turn>"""


# ---------------------------------------------------------------------------
# runners — each prints, and returns rows for the report
# ---------------------------------------------------------------------------


def _warm(base_url: str, model: str, timeout: float) -> float:
    """Pay the model-load wake so later latencies mean something."""
    body = build_body("Warm-up.", {"ready": {"type": "noul", "instructions": "Is this request served?"}})
    started = time.perf_counter()
    try:
        response, _ = post(base_url, model, body, timeout)
    except ProbeError as exc:
        print("  warm-up ERROR: %s" % exc)
        print(
            "  hint: a 500 'failed to load' is a CUDA OOM — check `nvidia-smi` and "
            "free the GPU holder before re-running."
        )
        return -1.0
    elapsed = (time.perf_counter() - started) * 1000.0
    answer = response.get("answers", {}).get("ready", {})
    print("  warm-up: %s   P(true)=%s   %.0f ms" % (answer.get("noul"), answer.get("noul"), elapsed))
    return elapsed


def run_batch_cost(base_url: str, model: str, timeout: float, state: str) -> dict:
    """Batching cost: does an 18-question map stay cheap vs 1 question?

    Posts the same state with 1, 6 and 18 questions drawn from the phrase bank
    and reports latency + ``input_tokens`` for each, so the report can say
    whether a batched map is one forward pass or scales with question count.
    """
    print("-" * 78)
    print("BATCHING COST (same state, N noul questions in one request)")
    rows = {}
    for n in (1, 6, 18):
        questions = {}
        for phrase in SLOP_PHRASES[:n]:
            questions[slugify(phrase.phrase)] = {
                "type": "noul",
                "instructions": (
                    'Does the text contain the exact phrase "%s" or a trivial '
                    "variant of it?" % phrase.phrase
                ),
            }
        try:
            response, ms = post(base_url, model, build_body(state, questions), timeout)
        except ProbeError as exc:
            print("  %2d questions -> ERROR: %s" % (n, exc))
            rows[n] = {"error": str(exc)}
            continue
        usage = response.get("usage", {})
        answered = len(response.get("answers", {}))
        print(
            "  %2d questions -> %4.0f ms   input_tokens=%s   answered=%d"
            % (n, ms, usage.get("input_tokens"), answered)
        )
        rows[n] = {"ms": ms, "input_tokens": usage.get("input_tokens"), "answered": answered}
    return rows


def run_slop(base_url: str, model: str, timeout: float) -> dict:
    """Family 1: three formulations x three fixtures."""
    print("=" * 78)
    print("FAMILY 1 — SLOP-PHRASE MEMBERSHIP")
    print("  phrases  : %d re-used from %s" % (len(SLOP_PHRASES), SLOP_SOURCE))
    print("  map size : %d questions (batching-cost check)" % len(SLOP_PHRASES))
    print("=" * 78)

    results: dict = {"fixtures": {}, "map_input_tokens": None, "single_input_tokens": None}

    for name, text, expect in SLOP_FIXTURES:
        truth = SLOP_FIXTURE_TRUTH[name]
        print("-" * 78)
        print("fixture: %-10s  (%d chars)  truth=%s" % (name, len(text), ",".join(truth) or "none"))
        print("  expect : %s" % expect)
        fixture: dict = {"truth": list(truth), "hits": phrase_hits(text)}

        # (a) BATCHED MAP ----------------------------------------------------
        map_q = build_slop_map_questions()
        body = build_body(text, map_q)
        print("  (a) BATCHED MAP  %d questions" % len(map_q))
        try:
            response, ms = post(base_url, model, body, timeout)
            answers = response.get("answers", {})
            fired = []
            table = []
            for phrase in SLOP_PHRASES:
                qid = slugify(phrase.phrase)
                ans = answers.get(qid, {})
                p = ans.get("noul")
                table.append((qid, p))
                if isinstance(p, (int, float)) and p >= 0.5:
                    fired.append(qid)
            usage = response.get("usage", {})
            tokens = usage.get("input_tokens")
            if results["map_input_tokens"] is None:
                results["map_input_tokens"] = tokens
            print("      latency %.0f ms   input_tokens=%s   output_tokens=%s" % (ms, tokens, usage.get("output_tokens")))
            print("      fired (P>=0.5): %s" % (", ".join(fired) if fired else "none"))
            for qid, p in table:
                print("        %-26s %s" % (qid, "%.4f" % p if isinstance(p, (int, float)) else p))
            fixture["map"] = {"fired": fired, "table": table, "ms": ms, "input_tokens": tokens}
        except ProbeError as exc:
            print("      ERROR: %s" % exc)
            fixture["map"] = {"error": str(exc)}

        # (b) SINGLE noul + criteria ----------------------------------------
        print("  (b) SINGLE noul (whole list in instructions, with criteria)")
        try:
            response, ms = post(base_url, model, build_body(text, build_slop_single_question()), timeout)
            answer = response.get("answers", {}).get("slop_any", {})
            usage = response.get("usage", {})
            if results["single_input_tokens"] is None:
                results["single_input_tokens"] = usage.get("input_tokens")
            print("      %s" % format_noul(answer))
            print("      latency %.0f ms   input_tokens=%s" % (ms, usage.get("input_tokens")))
            fixture["single"] = {"p": answer.get("noul"), "ms": ms, "input_tokens": usage.get("input_tokens")}
        except ProbeError as exc:
            print("      ERROR: %s" % exc)
            fixture["single"] = {"error": str(exc)}

        # (c) CHOICE over slop classes --------------------------------------
        print("  (c) CHOICE over slop classes (none gated last resort)")
        try:
            response, ms = post(base_url, model, build_body(text, build_slop_choice_question()), timeout)
            answer = response.get("answers", {}).get("slop_class", {})
            usage = response.get("usage", {})
            print("      %s" % format_choice(answer))
            print_probabilities(answer)
            print("      latency %.0f ms   input_tokens=%s" % (ms, usage.get("input_tokens")))
            fixture["choice"] = {
                "choice": answer.get("choice"),
                "probabilities": answer.get("probabilities"),
                "ms": ms,
                "input_tokens": usage.get("input_tokens"),
            }
        except ProbeError as exc:
            print("      ERROR: %s" % exc)
            fixture["choice"] = {"error": str(exc)}

        results["fixtures"][name] = fixture

    # --- score card --------------------------------------------------------
    print("-" * 78)
    print("FAMILY 1 SCORE CARD")
    clean = results["fixtures"]["clean"]
    slop = results["fixtures"]["slop"]
    border = results["fixtures"]["borderline"]

    def _fired_rules(f):
        """Map slugs fired in the (a) map back to engine rule ids."""
        m = f.get("map", {})
        fired = m.get("fired")
        if fired is None:
            return None
        return {SLUG_TO_RULE.get(slug, slug) for slug in fired}

    n = len(SLOP_PHRASES)
    clean_fired = _fired_rules(clean)
    slop_fired = _fired_rules(slop)
    border_fired = _fired_rules(border)
    slop_truth = set(slop.get("truth", ()))
    print(
        "  (a) map: clean FP %s/%d   slop recall %s/%d   borderline fired=%s (truth=%s)"
        % (
            len(clean_fired) if clean_fired is not None else "?",
            n,
            len(slop_fired & slop_truth) if slop_fired is not None else "?",
            len(slop_truth),
            len(border_fired) if border_fired is not None else "?",
            ",".join(sorted(border_fired)) if border_fired is not None else "?",
        )
    )
    for label, f in (("clean", clean), ("slop", slop), ("borderline", border)):
        single = f.get("single", {})
        p = single.get("p")
        print("  (b) single: %-10s P(true)=%s" % (label, "%.4f" % p if isinstance(p, (int, float)) else p))
    for label, f in (("clean", clean), ("slop", slop), ("borderline", border)):
        ch = f.get("choice", {})
        probs = ch.get("probabilities") or {}
        top = max(probs.values()) if probs else None
        print("  (c) choice: %-10s %s  (top P=%s)"
              % (label, ch.get("choice"), "%.4f" % top if top is not None else "?"))
    results["batch_cost"] = run_batch_cost(base_url, model, timeout, SLOP_PROSE)
    return results


def run_movement(base_url: str, model: str, timeout: float, runs: int) -> dict:
    """Family 2: the movement gate, ``runs`` times per fixture (spread matters)."""
    print("=" * 78)
    print("FAMILY 2 — NARRATIVE MOVEMENT  (%d runs per fixture)" % runs)
    print("  question : %s" % MOVEMENT_QUESTION["instructions"])
    print("=" * 78)

    results: dict = {}
    for name, text, expect in MOVEMENT_FIXTURES:
        print("-" * 78)
        print("fixture: %-9s  (%d chars)" % (name, len(text)))
        print("  expect : %s" % expect)
        probs: list[float | None] = []
        for run in range(1, runs + 1):
            try:
                response, ms = post(base_url, model, build_body(text, {"movement": MOVEMENT_QUESTION}), timeout)
            except ProbeError as exc:
                print("  run %d ERROR: %s" % (run, exc))
                probs.append(None)
                continue
            answer = response.get("answers", {}).get("movement", {})
            p = answer.get("noul")
            probs.append(p if isinstance(p, (int, float)) else None)
            usage = response.get("usage", {})
            print("  run %d: %-28s latency %.0f ms  input_tokens=%s"
                  % (run, format_noul(answer), ms, usage.get("input_tokens")))
        valid = [p for p in probs if p is not None]
        spread = (max(valid) - min(valid)) if valid else None
        print("  P(true) runs : %s" % ", ".join("%.4f" % p for p in valid))
        print("  spread       : %s" % ("%.4f" % spread if spread is not None else "n/a"))
        results[name] = {"probs": valid, "spread": spread, "expect": expect}
    return results


def run_sanity(base_url: str, model: str, timeout: float) -> dict:
    """Family 3: criteria dict, 2-question map, empty questions map."""
    print("=" * 78)
    print("FAMILY 3 — NESTED-CRITERIA SANITY")
    print("=" * 78)
    results: dict = {}

    print("-" * 78)
    print("(1) noul WITH a criteria dict — expect: accepted, no error")
    try:
        response, ms = post(base_url, model, build_body(SANITY_STATE, SANITY_NOUL_QUESTION), timeout)
        answer = response.get("answers", {}).get("repair", {})
        print("  raw answer : %s" % json.dumps(answer))
        print("  latency    : %.0f ms" % ms)
        results["noul_criteria"] = {"ok": "noul" in answer, "answer": answer}
    except ProbeError as exc:
        print("  ERROR      : %s" % exc)
        results["noul_criteria"] = {"ok": False, "error": str(exc)}

    print("-" * 78)
    print("(2) TWO-question map — expect: both ids answered")
    try:
        response, ms = post(base_url, model, build_body(SANITY_STATE, SANITY_TWO_QUESTION_MAP), timeout)
        answers = response.get("answers", {})
        print("  answered ids: %s" % ", ".join(sorted(answers)))
        for qid in SANITY_TWO_QUESTION_MAP:
            print("    %-14s %s" % (qid, format_noul(answers.get(qid, {}))))
        print("  latency    : %.0f ms" % ms)
        results["two_question"] = {"ids": sorted(answers), "answers": answers}
    except ProbeError as exc:
        print("  ERROR      : %s" % exc)
        results["two_question"] = {"error": str(exc)}

    print("-" * 78)
    print("(3) EMPTY questions map — expect: clean 400")
    try:
        response, ms = post(base_url, model, build_body(SANITY_STATE, {}), timeout)
        print("  UNEXPECTED 200: %s" % json.dumps(response)[:300])
        results["empty_map"] = {"status": 200, "body": response}
    except ProbeError as exc:
        match = re.search(r"HTTP (\d+) from", str(exc))
        status = int(match.group(1)) if match else None
        print("  status     : %s" % status)
        print("  body       : %s" % str(exc).split(": ", 1)[-1][:300])
        results["empty_map"] = {"status": status, "error": str(exc)}
    return results


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:4445", help="llama-server base URL (default: %(default)s)")
    parser.add_argument("--model", default="d1-3b", help="routed model id / preset section (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=120.0, help="per-request timeout seconds (default: %(default)s)")
    parser.add_argument("--runs", type=int, default=3, help="movement repeats per fixture (default: %(default)s)")
    parser.add_argument(
        "--family",
        choices=("all", "slop", "movement", "sanity"),
        default="all",
        help="which probe family to run (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    print("d1-3b /v1/systemone RE-SPIKE — hardened typed questions")
    print("base_url = %s   model = %s" % (args.base_url, args.model))
    print("warm-up first (first request pays the model load)")
    if _warm(args.base_url, args.model, args.timeout) < 0:
        return 1
    print()

    if args.family in ("all", "slop"):
        run_slop(args.base_url, args.model, args.timeout)
        print()
    if args.family in ("all", "movement"):
        run_movement(args.base_url, args.model, args.timeout, args.runs)
        print()
    if args.family in ("all", "sanity"):
        run_sanity(args.base_url, args.model, args.timeout)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
