#!/usr/bin/env python3
"""Spike probe: ask the d1-3b decision model six Fabulist-shaped questions.

Posts to llama-server's TypeSafe route `POST /v1/systemone` (llama.cpp server
README, "POST /v1/systemone"). The model answers a question in one forward pass
and generates no tokens, so there is nothing to sample: the response carries the
answer plus its probabilities.

Request shape:
    {"model": "d1-3b",                       # required in router mode
     "state": "<the text to judge>",
     "questions": {"<id>": {"type": ..., "instructions": ...}}}

Response shapes used here:
    noul   -> answers["<id>"]["noul"]                       # P(answer is true)
    choice -> answers["<id>"]["choice"], ["probabilities"], ["confidence"]

The `model` field matters: :4445 is a llama.cpp router (`--models-preset`), and
it forwards a POST to the child named by `model`. The first request to a
sleeping/absent child pays that child's load time.

This script only reads: it sends requests and prints results. It applies no
threshold and writes nothing.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


PROBES = [
    {
        "name": "critic-verdict",
        "expect": "true — the turn slips out of third-limited POV into first person, "
        "and the bandaged hand moves from Mireille to Aldric",
        "question": {
            "type": "noul",
            "instructions": "Does this turn fail quality checks?",
        },
        "state": """<turn index="41">
<cast>
Aldric Venn - POV, third person limited.
Mireille Dax - present, left hand bandaged (established turn 38).
</cast>
<exchange>
Mireille: "You kept the letter." She did not look up from the envelope.
Aldric: "I keep everything you leave behind. It became a habit somewhere around Vessin."
</exchange>
<narration>
I crossed the study to the window, and the bell tower caught the last of the light. Three weeks had passed since the ledger changed hands; in that time Marwold had emptied of the men who once paid for its silences. I counted the hours until the morning train, which is what one does when one is waiting to be arrested, or forgiven, or both.
She reached up with her right hand - the one that was whole - and straightened his collar. "Sit down, Aldric. You look like a man who has been standing since Vessin."
I sat. My hand, the bandaged one, throbbed against the arm of the chair.
</narration>
</turn>""",
    },
    {
        "name": "critic-category",
        "expect": "pacing — the confrontation stalls for three paragraphs of recap",
        "question": {
            "type": "choice",
            "instructions": "Which single category best describes the main problem with this turn?",
            "criteria": {
                "continuity": "It contradicts established facts, timeline or cast.",
                "style": "The prose itself is weak, repetitive or out of voice.",
                "pacing": "The scene does not move; it restates what the reader already knows.",
                "none": "There is no problem worth fixing in this turn.",
            },
        },
        "state": """<turn index="42">
<exchange>
Mireille: "Answer the question."
</exchange>
<narration>
Aldric did not answer at once, and in the silence the whole of the last three years arranged itself for him in the order he preferred not to remember it. There had been the winter in Vessin, when the ledger was still in Marek's strongroom and the guild still pretended to audit itself; the spring when the first surveyors came down the valley with their brass instruments and their reasonable questions; the summer of the fire at the east warehouse, which the guilds called an accident and the insurers called a loss and Marek called a lesson; the autumn when Mireille arrived with a warrant she never served.
Then there had been the second winter, which was mostly waiting, and the spring after it, which was mostly the sound of the river. The ledger moved twice that year, and each move cost him a friend. He had explained all of this before, to Mireille and to himself, in the study and on the road and once, badly, in a letter he burned. The surveyors had their instruments. The warehouse had its ashes. Marek had his lesson. None of it changed the envelope on the desk between them, which was still sealed.
He could still remember the first surveyor's coat, and the exact grey of it.
</narration>
</turn>""",
    },
    {
        "name": "spotlight",
        "expect": "line_3 (line_2 is defensible)",
        "question": {
            "type": "choice",
            "instructions": "Which candidate line is the most quotable highlight of this turn?",
            "criteria": {
                "line_1": '"You kept the letter." She did not look up from the envelope.',
                "line_2": '"I keep everything you leave behind. It became a habit somewhere around Vessin."',
                "line_3": "He had spent three weeks learning to say her name without it sounding like a confession.",
                "line_4": "Outside, the bell tower caught the last of the afternoon light.",
            },
        },
        "state": """<turn index="41">
<exchange>
Mireille: "You kept the letter." She did not look up from the envelope.
Aldric: "I keep everything you leave behind. It became a habit somewhere around Vessin."
</exchange>
<narration>
He had spent three weeks learning to say her name without it sounding like a confession, and he had not managed it yet. Outside, the bell tower caught the last of the afternoon light. He counted the hours until the morning train, which is what a man does when he is waiting to be forgiven.
</narration>
</turn>""",
    },
    {
        "name": "genre",
        "expect": "fantasy — secondary world with a ritual order and a lit resonance lamp",
        "question": {
            "type": "choice",
            "instructions": "Which genre is this scene?",
            "criteria": {
                "fantasy": "A secondary world with its own rules, magic or ritual.",
                "sci-fi": "Technology or science drives the premise.",
                "horror": "The scene's purpose is dread or the uncanny.",
                "slice-of-life": "Ordinary life, no speculative premise.",
                "other": "None of the above fits.",
            },
        },
        "state": """<turn index="7">
<scene>
The tide-readers came up the shale path at dusk, nine of them, cowled against the salt, and stopped at the lamp. Sera had lit it an hour before the order allowed, which was the third time this month, and the keeper's ledger had a column for that. The lamp burned coal from the deep seam, and the coal sang when the water was listening - a low note, more felt than heard, that told the readers where the drowned congregation had gone.
The eldest reader put two fingers on the glass, withdrew them, and wrote nothing. "It is answering someone else tonight," she said. Sera had known that for an hour. She had also known that the answering was her mother's name, and that saying so aloud would end her apprenticeship before the season turned.
</scene>
</turn>""",
    },
    {
        "name": "compaction",
        "expect": "true — 34 turns / ~11.8k tokens against an 8k context budget",
        "question": {
            "type": "noul",
            "instructions": "Is this history too long to keep whole?",
        },
        "state": """<history turns="34" approx_tokens="11800" context_budget_tokens="8192">
t7  Sera lights the lamp early; the tide-reader detects the drowned congregation. [resolved]
t8  Aldric accepts the ledger from Marek's strongroom clerk. [resolved]
t9  Mireille arrives in Vessin with an unserved warrant. [resolved]
t10 The east warehouse burns; the guilds call it an accident. [resolved]
t11 Sera's apprenticeship terms are read aloud in the chapter house. [resolved]
t12 Aldric burns a letter he never sends. [resolved]
t13 The surveyors take measurements of the valley floor. [resolved]
t14 Mireille refuses to serve the warrant and explains why. [resolved]
t15 Marek learns the ledger is gone. [resolved]
t16-20 The road to Marwold; three roadside scenes, one theft, one confession. [resolved]
t21 Sera is forbidden from lighting the lamp before the order allows. [resolved]
t22-24 The ledger changes hands twice; each move costs Aldric a friend. [resolved]
t25 The bell tower at Marwold is repaired and rehung. [resolved]
t26 Mireille's left hand is injured in Vessin. [resolved]
t27 Aldric rents the study in Marwold. [resolved]
t28-31 The surveyors' findings are published; two guilds dissolve. [resolved]
t32 Sera's mother's name is found in the drowned register. [resolved]
t33 The keeper's ledger records Sera's third violation. [resolved]
t34 Mireille arrives in Marwold with the letter. [open]
</history>""",
    },
    {
        "name": "memory-worthiness",
        "expect": "true — a permanent injury and a standing arrangement",
        "question": {
            "type": "noul",
            "instructions": "Does this turn contain a durable fact worth remembering?",
        },
        "state": """<turn index="41">
<exchange>
Mireille: "The surgeon in Vessin took two fingers. I do not use the left hand anymore, and I would rather you stopped asking about it."
Aldric: "Then the ledger stays here. Not the strongroom, not the bank - the house at Marwold, under the third floorboard, where you found it the first time."
Mireille: "That is a promise, then."
Aldric: "That is a promise."
</exchange>
</turn>""",
    },
]


class ProbeError(Exception):
    pass


def build_body(probe):
    """The /v1/systemone body for one probe (without the router's `model` field)."""
    return {
        "state": probe["state"],
        "questions": {probe["name"]: probe["question"]},
    }


def post(base_url, model, body, timeout):
    url = base_url.rstrip("/") + "/v1/systemone"
    payload = json.dumps({"model": model, **body}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        raise ProbeError("HTTP %s from %s: %s" % (exc.code, url, detail)) from exc
    except urllib.error.URLError as exc:
        raise ProbeError("cannot reach %s: %s" % (url, exc.reason)) from exc
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return json.loads(raw.decode("utf-8")), elapsed_ms


def question_id(body):
    (name,) = body["questions"].keys()
    return name


def format_noul(answer):
    probability = answer.get("noul")
    if probability is None:
        return "no `noul` field in the answer"
    verdict = "true" if probability >= 0.5 else "false"
    line = "noul P(true) = %.4f -> %s" % (probability, verdict)
    if "confidence" in answer:
        line += "  (confidence %.4f)" % answer["confidence"]
    return line


#: Rubric v2 (2026-10-13): replacement instructions/criteria for the three
#: choice probes, written after the v1 misses. Levers (diagnosed in the
#: llm-expert lane before its provider died): make the escape option ("none"
#: / "other") an ACTIVE claim instead of a safe default; give every criterion
#: a lexical anchor (literal signal words); use concrete sign-lists with an
#: explicit "one sign is enough"; add a tie-break for the spotlight ladder.
#: v1 stays the default so the recorded first-round numbers stay reproducible;
#: run with --rubric v2.
RUBRIC_V2 = {
    "critic-category": {
        "type": "choice",
        "instructions": (
            "Name the single biggest flaw in this turn. Answer 'none' only if "
            "no flaw can be named."
        ),
        "criteria": {
            "continuity": "Contradicts established facts, timeline, cast or injuries.",
            "style": "Weak or wrong prose: repetition, banned phrases, wrong point of view.",
            "pacing": "The scene stalls or recaps: it restates known history, it does not advance.",
            "none": "No flaw can be named. Choose this only when every other option fails.",
        },
    },
    "spotlight": {
        "type": "choice",
        "instructions": (
            "Pick the line that earns its place: it reveals character or turns the "
            "scene. Prefer the later line when tied. Ignore mood-only or "
            "scenery-only lines."
        ),
        # The candidate lines ARE the criteria; they stay verbatim so the probe
        # still measures selection, not rewriting.
        "criteria": {
            "line_1": '"You kept the letter." She did not look up from the envelope.',
            "line_2": '"I keep everything you leave behind. It became a habit somewhere around Vessin."',
            "line_3": "He had spent three weeks learning to say her name without it sounding like a confession.",
            "line_4": "Outside, the bell tower caught the last of the afternoon light.",
        },
    },
    "genre": {
        "type": "choice",
        "instructions": (
            "Name the dominant genre. One clear sign is enough. Answer 'other' "
            "only when no sign below appears."
        ),
        "criteria": {
            "fantasy": "Secondary world, magic, ritual orders, guilds, pre-industrial life, artefacts that answer.",
            "sci-fi": "Futuristic technology, space, machines, laboratories, ships, AI.",
            "horror": "Dread, fear, gore, monsters, the uncanny as threat.",
            "slice-of-life": "Ordinary everyday life, no magic or technology premise.",
            "other": "No sign above appears. Use only as a last resort.",
        },
    },
}



#: Rubric v3 (2026-10-13, after the v2 run + a correction): the production
#: critic does NOT use a {continuity,style,pacing} taxonomy — it returns a
#: tri-state verdict ok/diffs/fail (prompts/critic.py). v3 therefore (a) feeds
#: the critic probes a PRODUCTION-SHAPED state (user msg, numbered prose,
#: validator flags, prior-turn tail, state line — the exact input the critic
#: LLM receives), and (b) asks the engine's REAL taxonomy for the tri-state.
#: It also measures BOTH directions of a cheap gate: a broken turn must read
#: true, a clean turn must read false (a gate that never fires is useless).
PROBES_V3 = [
    {
        "name": "critic-repair-broken",
        "expect": "true — POV slips from third-limited to first person and the "
        "bandaged hand moves from Mireille to Aldric",
        "question": {
            "type": "noul",
            "instructions": (
                "Does this turn need repair? Answer true if the prose contains "
                "a repair-worthy defect."
            ),
        },
        "state": """<user_message>Mireille: "You kept the letter."</user_message>
<prose>
1 Mireille: "You kept the letter." She did not look up from the envelope.
2 Aldric: "I keep everything you leave behind."
3 I crossed the study to the window.
4 She reached up with her right hand - the one that was whole - and straightened his collar.
5 I sat. My hand, the bandaged one, throbbed against the arm of the chair.
6 Mireille flexed her bandaged left hand and said nothing.
</prose>
<validator_flags>[{"rule_id": "expression_blank", "check": "slop", "severity": "warn"}]</validator_flags>
<prior_turns_tail>Aldric poured the tea. The letter stayed sealed on the desk.</prior_turns_tail>
<state_line>time: 18:40 | loc: study | tone: quiet</state_line>""",
    },
    {
        "name": "critic-repair-clean",
        "expect": "false — the turn is clean: consistent POV, no continuity error",
        "question": {
            "type": "noul",
            "instructions": (
                "Does this turn need repair? Answer true if the prose contains "
                "a repair-worthy defect."
            ),
        },
        "state": """<user_message>I ask Mireille whether she ever read the letter.</user_message>
<prose>
1 Mireille: "You kept the letter." She did not look up from the envelope.
2 Aldric said nothing. The tea had gone cold an hour ago.
3 Outside, the bell tower caught the last of the afternoon light.
4 She turned the envelope over once, then set it down unopened.
</prose>
<validator_flags>[]</validator_flags>
<prior_turns_tail>Aldric poured the tea. The letter stayed sealed on the desk.</prior_turns_tail>
<state_line>time: 18:40 | loc: study | tone: quiet</state_line>""",
    },
    {
        "name": "critic-verdict-tri",
        "expect": "fail — the turn restates three years of prior history and never advances",
        "question": {
            "type": "choice",
            "instructions": (
                "Return the verdict the engine should apply to this turn."
            ),
            "criteria": {
                "ok": "The turn advances the scene with fresh detail and no repair-worthy defect.",
                "diffs": "A local defect (a repeated image, one weak line) that find/replace edits can repair.",
                "fail": "The whole turn restates the prior scene and never advances; it needs regeneration.",
            },
        },
        "state": """<user_message>Mireille: "Answer the question."</user_message>
<prose>
1 Aldric did not answer at once, and in the silence the whole of the last three years arranged itself for him in the order he preferred not to remember it.
2 There had been the winter in Vessin, when the ledger was still in Marek's strongroom and the guild still pretended to audit itself; the spring when the first surveyors came down the valley; the summer of the fire at the east warehouse; the autumn when Mireille arrived with a warrant she never served.
3 Then there had been the second winter, which was mostly waiting, and the spring after it, which was mostly the sound of the river. The ledger moved twice that year, and each move cost him a friend. He had explained all of this before, to Mireille and to himself.
4 The surveyors had their instruments. The warehouse had its ashes. Marek had his lesson. None of it changed the envelope on the desk between them, which was still sealed.
</prose>
<validator_flags>[]</validator_flags>
<prior_turns_tail>The same history had been recited in the study two days before.</prior_turns_tail>
<state_line>time: 09:10 | loc: study | tone: tense</state_line>""",
    },
    {
        "name": "spotlight",
        "expect": "line_2 OR line_3 — both defensible (line_2 reveals backstory in speech; line_3 is interior), v1/v2 both chose line_2",
        "question": RUBRIC_V2["spotlight"],
        "state": PROBES[2]["state"],
    },
    {
        "name": "genre",
        "expect": "fantasy — secondary world with a ritual order and a lit resonance lamp",
        "question": RUBRIC_V2["genre"],
        "state": PROBES[3]["state"],
    },
    {
        "name": "compaction",
        "expect": "true — 34 turns / ~11.8k tokens against an 8k context budget",
        "question": PROBES[4]["question"],
        "state": PROBES[4]["state"],
    },
    {
        "name": "memory-worthiness",
        "expect": "true — a permanent injury and a standing arrangement",
        "question": PROBES[5]["question"],
        "state": PROBES[5]["state"],
    },
]


def select_probes(rubric):
    """PROBES with the v2 question text swapped in for the covered probes."""
    if rubric == "v3":
        return list(PROBES_V3)
    if rubric != "v2":
        return list(PROBES)
    selected = []
    for probe in PROBES:
        override = RUBRIC_V2.get(probe["name"])
        selected.append(dict(probe, question=override) if override else probe)
    return selected


def format_choice(answer):
    choice = answer.get("choice", "<missing>")
    line = "%s" % choice
    if "confidence" in answer:
        line += "  (confidence %.4f)" % answer["confidence"]
    return line


def print_probabilities(answer):
    probabilities = answer.get("probabilities")
    if not probabilities:
        return
    for key, value in sorted(probabilities.items(), key=lambda kv: -kv[1]):
        print("      %-24s %.4f" % (key, value))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:4445",
        help="llama-server base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--model",
        default="d1-3b",
        help="routed model id / preset section name (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="per-request timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--rubric",
        choices=("v1", "v2", "v3"),
        default="v1",
        help="question wording: v1 = first-round baseline, v2 = revised criteria, v3 = production-shaped critic states + engine taxonomy (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    probes = select_probes(args.rubric)
    print("d1-3b /v1/systemone spike — %d probes, one request each (rubric %s)" % (len(probes), args.rubric))
    print("base_url = %s   model = %s" % (args.base_url, args.model))
    print("-" * 72)

    rows = []
    failures = 0
    for index, probe in enumerate(probes, start=1):
        body = build_body(probe)
        header = "[%d/%d] %s  (%s)" % (
            index,
            len(PROBES),
            probe["name"],
            probe["question"]["type"],
        )
        print(header)
        print("  question : %s" % probe["question"]["instructions"])
        if "criteria" in probe["question"]:
            print("  options  : %s" % ", ".join(probe["question"]["criteria"].keys()))
        print("  state    : %d chars" % len(probe["state"]))
        try:
            response, elapsed_ms = post(args.base_url, args.model, body, args.timeout)
        except ProbeError as exc:
            failures += 1
            print("  ERROR    : %s" % exc)
            if "501" in str(exc):
                print(
                    "            501 = the routed model is not a decision model; check that "
                    "GET /models reports output_modalities [\"decisions\"] for %r" % args.model
                )
            print("  expect   : %s" % probe["expect"])
            print()
            rows.append((probe["name"], probe["question"]["type"], "ERROR", None, None))
            continue

        answer = response.get("answers", {}).get(question_id(body))
        if answer is None:
            failures += 1
            print("  ERROR    : response has no answer for %r" % probe["name"])
            print("  raw      : %s" % json.dumps(response)[:400])
            print()
            rows.append((probe["name"], probe["question"]["type"], "ERROR", None, None))
            continue

        if probe["question"]["type"] == "noul":
            print("  answer   : %s" % format_noul(answer))
            top = answer.get("noul")
        else:
            print("  answer   : %s" % format_choice(answer))
            print_probabilities(answer)
            top = max(answer.get("probabilities", {}).values(), default=None)

        print("  expect   : %s" % probe["expect"])
        usage = response.get("usage", {})
        print(
            "  latency  : %.0f ms   input_tokens=%s output_tokens=%s"
            % (elapsed_ms, usage.get("input_tokens", "?"), usage.get("output_tokens", "?"))
        )
        if "model" in response:
            print("  served by: %s" % response["model"])
        print()
        rows.append((probe["name"], probe["question"]["type"], answer, top, elapsed_ms))

    print("-" * 72)
    print("%-22s %-7s %-14s %8s %9s" % ("probe", "type", "answer", "top P", "ms"))
    for name, qtype, answer, top, elapsed_ms in rows:
        if isinstance(answer, str):
            shown = answer
        elif qtype == "noul":
            shown = "%.4f" % answer.get("noul", float("nan"))
        else:
            shown = str(answer.get("choice", "?"))
        print(
            "%-22s %-7s %-14s %8s %9s"
            % (
                name,
                qtype,
                shown,
                "-" if top is None else "%.4f" % top,
                "-" if elapsed_ms is None else "%.0f" % elapsed_ms,
            )
        )
    if failures:
        print("\n%d/%d probes failed" % (failures, len(PROBES)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
