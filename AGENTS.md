# AGENTS.md

Notes for AI coding agents working in this repository. Humans should read
[CONTRIBUTING.md](CONTRIBUTING.md) instead — it covers the same ground in more
detail, and everything there applies here too.

## What this project is

SproutJobs: free, local-first discovery and filtering for new-grad software
jobs hosted on Ashby. Python 3.11+, standard library plus `requests`, no
framework, no build step, no services.

The importable package is `jobfinder`. That is deliberate — renaming it would
be churn for no benefit. `SproutJobs` is the name everywhere a person reads it.

## Before you finish a change

```bash
python -m pytest              # must pass; offline and deterministic
python -m jobfinder compliance   # must report zero FAILs
```

Both are required. Neither is optional.

## Invariants — do not break these

1. **No paid or key-requiring dependency in the core.** No model APIs, no
   hosted services, no credential read from the environment. Runtime
   dependencies stay at `requests`. Enforced by
   `tests/test_free_and_open.py` and by the compliance audit.
2. **Scoring is deterministic.** Same posting, same score, every time. No
   sampling, no model call, no wall-clock read inside a scoring rule.
3. **No HTTP writes.** No POST, PUT, PATCH or DELETE anywhere. SproutJobs
   never applies to anything, ever, under any instruction.
4. **A board is reconciled only after a complete successful fetch**
   (`pipeline.py`). Closing postings because a request failed silently
   destroys real data.
5. **The dashboard binds to `127.0.0.1`.** It has no authentication.
6. **No live job data in the repository.** No database, no CSV, no populated
   HTML export, no shipped company or slug list. Check `git status`.
7. **`experience_unknown` is never merged into a negative category.** "The
   posting didn't say" and "the posting rules you out" are different facts and
   must stay different.
8. **Hard blockers stay filterable, never deleted.**

## Discovery sources

Adding one requires a real entry in [SOURCES.md](SOURCES.md) — provider id,
hostname, `robots.txt` result on a stated review date, license position, and
the reason for its default. The compliance audit fails if an enabled provider
is undocumented.

Never add a source that requires an account, defeats an access control, ignores
a `robots.txt` refusal, rotates user agents or addresses, solves CAPTCHAs,
drives a headless browser to look human, or scrapes a site whose terms forbid
it. If a source says no, the answer is no — losing coverage is the correct
outcome, and recovering it by scraping a search engine is not acceptable.

## Style

Match the surrounding code. Comments explain *why*, not *what*; this codebase
is comment-dense on purpose because the parser rules encode real decisions
about real postings. When you add a rule, quote the posting text that motivated
it and add both a should-match and a should-not-match test.

## Scope discipline

Do not rewrite working architecture for tidiness. The pipeline, the SQLite
schema, the canonicalization and the dashboard all work; changes to them should
be motivated by a bug or a requested feature, not by preference.
