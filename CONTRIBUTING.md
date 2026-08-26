# Contributing to SproutJobs

Thanks for looking. This is a small, deliberately boring codebase: standard
library plus `requests`, no framework, no build step, no magic.

---

## Local setup

You need **Python 3.11 or newer** (for `tomllib` and `datetime.UTC`).

```bash
# Fork the repository on GitHub first, then clone your fork
# (replace your-account with your GitHub account name):
git clone https://github.com/your-account/SproutJobs.git
cd SproutJobs
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Or just run `./run.sh`, which does all of that and then runs the pipeline.

## Tests

```bash
python -m pytest              # the default suite: offline and deterministic
python -m pytest -m integration   # opt-in, needs network, contacts real boards
python -m jobfinder compliance    # the engineering safeguards audit
```

The default run is offline by design — `addopts` in `pyproject.toml` deselects
the `integration` marker. Tests must not need the network unless they are marked
`integration`, and they must not depend on a database left behind by a previous
run.

Both the test suite and `python -m jobfinder compliance` must pass before a pull
request is merged.

---

## Architecture

Data flows one way, and each stage is independently testable:

```
discovery/      →  candidate board slugs, from pluggable sources
sources/ashby   →  verify each slug against Ashby's public API; fetch boards
normalize       →  raw JSON → NormalizedJob
classify        →  is this a software role at all?
eligibility     →  experience, work authorization, graduation, level range
experience      →  the evidence bucket the dashboard filters on
geo             →  U.S. geography tags
scoring         →  fit score, blockers, category (deterministic)
dedup/canonical →  collapse the same role posted many times
db              →  SQLite: history, first_seen, active/inactive reconciliation
reporting       →  CSV + static HTML export
server/dashboard→  local HTTP dashboard over the full dataset
pipeline        →  owns the order of operations and the failure policy
```

Two rules in `pipeline.py` matter more than the rest, and breaking either is a
serious regression:

1. **A board is reconciled only after a complete, successful fetch.** Closing a
   posting because a request failed would silently lose real jobs.
2. **Nothing reaches the reports that did not come from a successful fetch.**

---

## Adding a discovery provider

A provider is one class with a `name` and a `discover()` method. It does not
touch ingestion, SQLite, scoring, eligibility, canonicalization or the
dashboard, and it cannot create a company — every slug it produces is an
unverified guess until Ashby's own API confirms it.

1. Write the class in `jobfinder/discovery/`:

   ```python
   class MyDiscoverer:
       name = "my_source"

       def discover(self, force: bool = False) -> DiscoveryResult:
           result = DiscoveryResult(source=self.name)
           # ... populate result.candidates with DiscoveryCandidate(...)
           return result
   ```

2. Register it in `jobfinder/discovery/registry.py`, gated on a config flag, and
   add a line to `SOURCE_DESCRIPTIONS`.
3. Add the flag to `DiscoveryConfig` in `jobfinder/config.py` and to
   `config.toml`, with a comment saying why it ships on or off.
4. **Add an entry to [SOURCES.md](SOURCES.md)** — including the provider id, the
   hostname, whether `robots.txt` applies and what it actually said on a stated
   review date, the license position, and the reason for the default. The
   compliance audit fails if an enabled provider is missing from that file.
5. Add tests: it yields candidates from a fixture, it survives a network
   failure without raising, and it honors a disallowed `robots.txt`.

**A provider must never take down a run.** Exceptions are caught per-provider in
`run_discovery`; failures are recorded and reported, and the other providers and
all previously verified boards carry on.

### Source requirements

A new source is only accepted if it is:

- **Free and keyless** — no paid plan, no account, no API key.
- **Public** — nothing behind a login, a paywall, or a CAPTCHA.
- **Permitted** — `robots.txt` honored, terms not contradicted, and a refusal
  accepted rather than routed around.
- **Documentable** — you can give it an honest SOURCES.md entry.

**Not acceptable, and not negotiable:** scraping LinkedIn or any site whose
terms forbid it; scraping Google, Bing or another general search engine to
recover coverage; CAPTCHA solving; `User-Agent`, TLS-fingerprint or address
rotation; headless browsers used to appear human; any use of logged-in or
private data. If a source says no, the answer is no and the coverage loss is
the correct outcome.

---

## Adding parser rules

The eligibility, experience, geography and classification rules are plain
regular expressions and Python — readable, editable, and reviewable. That is the
whole design: a verdict you disagree with is a rule you can find and change.

When adding one:

- **Put it with its neighbours.** Sponsorship phrasing goes in
  `eligibility.py`, geography in `geo.py`, title seniority in `scoring.py`.
- **Quote real evidence.** A rule should come from posting text you actually
  saw, and the test should use that text.
- **Prefer "unknown" over a guess.** `experience_unknown` exists because a
  posting that says nothing about experience is not the same as one that rules
  you out. Never collapse the two.
- **Do not double-count.** Scoring deliberately emits one experience verdict and
  one title-seniority penalty, so "Senior Staff Engineer" is not punished twice.
- **Keep blockers identifiable.** A hard blocker must stay filterable rather
  than silently deleting the posting.

### Regression tests are required

Every rule change needs a test, and the test needs both directions:

- the phrasing that **should** match, using real posting text; and
- the near-miss that **should not** — "no prior experience required" must not be
  read as "requires prior experience".

`tests/test_eligibility.py`, `test_experience.py`, `test_scoring.py`,
`test_geo.py` and `test_work_authorization.py` are full of examples. A pull
request that changes a rule without a test will be asked for one.

---

## Non-negotiables

These are enforced by `tests/test_free_and_open.py` and
`python -m jobfinder compliance`, not just by convention:

- **No paid or key-requiring dependency in the core.** No OpenAI, Anthropic, or
  any other model API. No hosted database. No SaaS. Nothing that reads a
  credential from the environment. Runtime dependencies stay at `requests`.
- **Deterministic scoring.** The same posting must always produce the same
  score. No sampling, no model call, no dependence on the wall clock inside a
  scoring rule. If you cannot explain a score by pointing at a rule, it does not
  belong.
- **No writes to anyone's server.** No POST, PUT, PATCH or DELETE. SproutJobs
  never applies to anything.
- **No live job data in the repository.** No database, no CSV, no populated HTML
  export, and no shipped company or slug list. Check `git status` before you
  commit.
- **The dashboard stays on loopback.**

A local, offline, optional model integration could be discussed one day. The
core stays free, keyless and deterministic regardless.

---

## Pull requests

- Keep the diff focused; a bug fix and a refactor are two pull requests.
- Match the surrounding style — including comment density. This codebase
  explains *why*, not *what*.
- Say what you tested. If you touched a rule, show the posting text that
  motivated it.
- Run `python -m pytest` and `python -m jobfinder compliance` first.

Bug reports are welcome too, especially "this posting was scored wrong" reports
with the posting URL and what you expected.
