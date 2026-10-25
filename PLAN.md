# Plan

The design record of this repository is `docs/decisions/` — a public digest of the
load-bearing rulings, one entry per decision, each stating the ruling and the consequence it
protects.

```
docs/decisions/README.md      the digest (start here)
AGENTS.md                     working rules for contributors and AI agents
docs/                         live specs (persona, UI language, prompt surfaces, components)
private/                      superseded wave records (unshipped)
```

The full working log — the founding plan, every decision in order with its build history,
wave records, and the current-state notes — is maintained privately and is not part of this
repository. Module docstrings throughout `src/fabulist/` cite the historical log ids
(`D9`, `D38`, …) for traceability; `docs/decisions/README.md` carries the same ids so a
citation can be resolved to the ruling it names.
