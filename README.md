# SproutJobs

**Free, local-first discovery and filtering for new-grad software jobs hosted on Ashby.**

SproutJobs finds public [Ashby](https://jobs.ashbyhq.com) job boards, asks Ashby's own
public API what is currently posted on them, works out which roles a new graduate could
actually get, and serves it all as a searchable dashboard on your own machine.

No account. No API key. No subscription. No telemetry. Job data and your dashboard searches
stay on your computer; fetching public sources does send network requests to them.

**It never applies for you.** No forms, no messages, no accounts created on your behalf.
The last step is always yours: click **Apply** and fill in the employer's own application.

---

## The problem it solves

Searching for an entry-level software job means opening the same job board every day and
re-reading the same hundred postings to work out which handful are new, which are actually
entry-level, and which quietly rule you out on page three.

Job aggregators solve some of that and introduce their own problems: stale listings, ghost
postings, an account you have to make, and a ranking you cannot inspect.

SproutJobs does something narrower and more honest:

- **It reads the source.** Postings come from Ashby's public posting API — the same data the
  employer's own careers page renders — so a role that closed is gone, not stale.
- **It remembers.** A local SQLite file keeps every posting it has ever seen, so it can tell
  you what is genuinely new since your last run.
- **It shows its reasoning.** Every score is a list of plain rules that fired, and you can
  read them, disagree with them, and edit them.
- **It refuses to guess for you.** "The posting never mentioned experience" and "the posting
  requires five years" are different facts, and they are never collapsed into one bad score.

---

## Completely free, and local-first

Running SproutJobs costs nothing and requires no account anywhere:

- **No paid APIs and no API keys.** Nothing reads a credential from your environment.
- **No OpenAI, Anthropic, or any other model dependency.** Every verdict comes from readable
  rules in the source tree.
- **No cloud account, no hosted database, no SaaS.** One SQLite file in `data/`.
- **Two dependencies:** `requests` and `pytest`, both open source.
- **Deterministic.** The same posting always produces the same score.

This is enforced, not just promised. `tests/test_free_and_open.py` fails if a paid client is
imported, if a credential is read, or if an outbound request goes to a host outside the
documented list — and `python -m jobfinder compliance` audits the whole checkout.

**Currently supported ATS: Ashby only.** The adapter boundary is small enough that Greenhouse
or Lever could be added without touching scoring or reporting, but they are not there today.

---

## Requirements

- **Python 3.11 or newer** (for `tomllib` and `datetime.UTC`)
- macOS or Linux for `run.sh`; on Windows use the manual commands below
- An internet connection when you run it
- About **500 MB of disk** for a default run — it keeps the full text of every
  posting it has ever seen (measured: 448 MB after one run over 584 boards)

That is the whole list.

---

## Quick start

```bash
git clone https://github.com/yashtamhane/SproutJobs.git
cd SproutJobs
./run.sh
```

That is it. `run.sh` finds a suitable Python, creates `.venv`, installs the two
dependencies, creates an empty database, discovers Ashby boards, fetches what is posted on
them, scores everything, and opens the local dashboard automatically in your browser.

**The first run takes about two minutes.** It contacts roughly 600 candidate boards once
each, at a deliberately polite pace. Later runs only refresh the boards that worked, and are
faster.

For scale, one measured first run: 614 candidate boards discovered, 584 verified, 17,655
active postings stored, 3,834 of them software roles, 2,925 canonical opportunities after
duplicate collapsing. Your numbers will differ — postings change daily.

Press **Ctrl+C** to stop the dashboard.

```bash
./run.sh --no-open     # run and serve, but do not launch a browser
./run.sh --no-serve    # collect and export only; do not start the dashboard
./run.sh --deep        # also sweep the Internet Archive index — slow and wide, see below
```

If `python3` on your system is older than 3.11, `run.sh` looks for `python3.12`,
`python3.13` and similar automatically. To choose one yourself:

```bash
PYTHON_BIN=/opt/homebrew/bin/python3.12 ./run.sh
```

Prefer to do it by hand — or on Windows?

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
python -m jobfinder run
python -m jobfinder serve --open
```

---

## Everyday use

One command, whenever you want fresh results:

```bash
./run.sh
```

The dashboard is always at **http://127.0.0.1:8765**.

Already have data and just want to look at it, with no network at all?

```bash
python -m jobfinder open
```

### Useful commands

```bash
python -m jobfinder run                     # the everyday run
python -m jobfinder run --skip-discovery    # refresh known boards only, no discovery
python -m jobfinder run --refresh-discovery # re-read sources even if the cache is fresh
python -m jobfinder run --max-companies 20  # try a small run first
python -m jobfinder run --deep              # add the wide Internet Archive sweep

python -m jobfinder serve --open            # dashboard over the complete dataset
python -m jobfinder report                  # rebuild CSV + exports offline
python -m jobfinder rescore                 # re-judge stored jobs with current rules

python -m jobfinder add-company notion      # track a board yourself (still verified)
python -m jobfinder add-url https://jobs.ashbyhq.com/notion
python -m jobfinder companies               # the company registry
python -m jobfinder discovery-report        # where every candidate slug came from
python -m jobfinder stats                   # counts by geography and experience evidence
python -m jobfinder compliance              # audit the engineering safeguards
python -m jobfinder smoke-test notion       # check live boards, touching nothing
```

---

## The dashboard

The dashboard is a small local HTTP server bound to `127.0.0.1`, reading your SQLite file
directly. Every filter, count and sort runs in SQL over the **complete** dataset and the
browser is handed one page of results at a time — so a job that exists in your database can
never be invisible on the page.

### Filters

Two filters are **multi-select checkbox groups**, because the questions are not either/or.
In both, ticking nothing means *no filtering on that axis*.

**Geography** — tick any combination:

| Filter | Matches |
| --- | --- |
| **NYC** | New York City and its boroughs |
| **New Jersey** | Jersey City, Hoboken, Newark, anywhere else in NJ |
| **Remote US** | remote roles open to people in the United States |
| **Other US** | every other U.S. location — Austin, Seattle, Denver, all of it |

A posting can carry several tags and shows up under each. A role open in New York *and*
Austin is both **NYC** and **Other US**. Ticking **NYC** and **Remote US** gives you the
**union**, not the intersection.

**Experience evidence** — tick any combination:

| Filter | Matches |
| --- | --- |
| **Explicit new grad** | names new grads, entry level, or recent graduates |
| **0 years / no experience** | "no experience required", or a stated zero-year requirement |
| **0–1 years** | a stated band starting at zero |
| **Experience unknown** | the posting **never said** — not a rejection |
| **1+ years** | asks for one year, or a 1–2 band |
| **2+ years** | asks for two or more, including 3+ |
| **Hard blocker** | something disqualifies you outright |

The rest are single-select: free text, title keyword, company, match category, active/closed,
role type, location text, workplace, evidence confidence, authorization warnings, freshness
(today through 30 days, by publish date or discovery date), and minimum score. Sort by best
match, newest published, newest discovered, company, or compensation.

Collection is **U.S.-wide**. Nothing is dropped for being outside a particular city — the
geography tags are a preference you apply, not a filter baked into collection. Raw location
text is preserved exactly as the employer wrote it.

---

## How scoring works

Scoring is deterministic and explainable: plain rules, no model calls. Every score keeps the
list of signals that produced it, and those signals are what the dashboard shows under
**"Why it matched"**.

A role first has to look like actual software engineering. Keywords alone are not enough —
finance, data and sales postings routinely mention Python and SQL — so a software verdict
needs support from the title or a clearly engineering department.

**Being a software job earns no points.** Classification is a gate: it decides whether a
posting is considered at all. The score answers a different question — *is there evidence a
new graduate qualifies?* — and every posting starts at zero and earns its way up.

Geography contributes **nothing** to the score. An Austin role is not worth fewer points than
a Manhattan one; you narrow by region with the filter instead.

### `entry_level_score` versus eligibility and blockers

A low score can mean two completely different things, and collapsing them buries good jobs.
So four independent facts are recorded about every posting:

| Field | Question it answers |
| --- | --- |
| `entry_level_score` | How well does this suit a new graduate? (0–100, evidence only) |
| `evidence_confidence` | How much did the posting actually tell us? (high / medium / low) |
| `experience_category` | What did it say about experience? |
| `blocker_status` | Is anything disqualifying, regardless of fit? (clear / review / blocked) |

They really are independent, and that is the point. A role can be a **perfect entry-level
fit** *and* blocked by a citizenship requirement: `entry_level_score` stays high while the
ranked score is capped, so the card can tell you it would otherwise have been a strong match
rather than silently burying it.

Hard blockers are **retained and filterable**, never deleted. Use the **Blocker status**
filter to see exactly what was ruled out and why.

All four fields are in the CSV.

### `experience_unknown`: "didn't say" is not "no"

This is the distinction that matters most in a new-grad search.

> **Score:** low · **Confidence:** low · **Experience:** not stated
> *No explicit experience requirement was found in this posting.*

is not the same fact as

> **Score:** low · **Confidence:** high · **Experience:** 3+ years required
> *Experience requirement exceeds the new-grad target.*

The first has told you **nothing** and may well be a fine application. The second has told
you plainly that you do not meet the bar. Treating "no requirement found" as "too much
experience required" is how a tool quietly hides the roles you should have applied to.

So **unknown-experience postings are kept**, get their own filter, and are never described as
having failed anything. In practice they are a large share of all postings.

Every posting lands in exactly one category:

| Category | Meaning | On target? |
| --- | --- | --- |
| `explicit_new_grad` | Title names new grads / early career | ✅ |
| `explicit_entry_level` | Junior, Associate, Engineer I, "entry level" | ✅ |
| `recent_graduate` | Body welcomes recent graduates | ✅ |
| `explicit_zero_experience` | "No experience required", or 0 years | ✅ |
| `zero_to_one_year` | Stated 0–1 years | ✅ |
| `zero_to_two_years` | Stated 0–2 years | ✅ |
| `one_plus_year` | 1+ years — reachable with internships | soft |
| `one_to_two_years` | 1–2 years | soft |
| `two_plus_years` | 2+ years | ✗ |
| `three_plus_required` | 3+ years required | ✗ |
| `hard_blocker` | 4+ years required — rules you out | ✗ |
| `experience_unknown` | **The posting never said.** Not a judgement. | unknown |

Precedence avoids false positives. A required four-plus years disqualifies regardless of
wording. A required three-plus beats friendly wording, because that number is what a
recruiter screens on — and when the two disagree the card says so instead of picking one
silently. An explicit new-grad *title* is the employer's own label and outranks a remaining
number. *Required* and *preferred* are read very differently.

### Evidence confidence

Confidence describes **how much the posting told us**, in either direction — so
high-confidence *negative* evidence reads as high confidence:

- **High** — the employer said who the role is for, or stated a number: *new grad*,
  *entry level*, *0–2 years*, *no experience required* — and equally *3+ years required*.
- **Medium** — plausible but unproven: *1+ years*, a matching graduating class, an
  "all levels" ladder that genuinely reaches entry level.
- **Low** — context, not evidence: a CS degree mention, a location tag. These never lift a
  posting out of weak on their own.

### Work authorization and sponsorship — and its limits

Silence is not rejection. Most postings say nothing about sponsorship and are treated as
**unknown**, not excluded. Only explicit statements count against a job.

Detection is **scoped, not adjacent**. Real postings rarely write "we do not sponsor". They
write things like *"for the time being, we are unable to offer employment to people who live
outside of the United States and/or to anyone who requires sponsorship for an employment visa
(such as H-1B status)"* — 96 characters from the end of the refusal "unable to" to the start
of "sponsorship", the word it refuses. So each posting is split into sentences and a refusal
counts only when it **governs** a sponsorship object in the same sentence.

The card names one of four states rather than claiming more certainty than it has:

| State | Meaning |
| --- | --- |
| **Explicit sponsorship restriction** | Sponsorship is unavailable, or you must not need it |
| **Sponsorship appears available** | The posting offers or discusses providing sponsorship |
| **Sponsorship language present, eligibility uncertain** | Visas came up, no policy stated |
| **No sponsorship information found** | The subject never came up |

Restriction types stay separate because they are different problems: sponsorship refusal,
citizenship requirement, permanent-resident-only rules, ITAR/export control, OPT/CPT
exclusion, and security clearance each get their own flag.

Precision matters as much as recall. Equal-opportunity boilerplate names *citizenship* and
*immigration status* as protected characteristics and restricts nobody. Nor does *"we can't
successfully sponsor for every role, but if we make you an offer we'll make every reasonable
effort"* — a sponsorship-**friendly** sentence that reads word-for-word like a refusal. Both
are excluded deliberately, with tests.

> ### ⚠️ Limits of the sponsorship parser
>
> **This is a keyword-and-grammar screening aid, not legal or immigration advice.**
> It reads English text with regular expressions. It will miss unusual phrasing, it can
> misread a sentence whose structure it was not built for, and it knows nothing about your
> actual situation, visa status, or the employer's real policy — which is frequently
> different from what a job description says, and changes without the posting being updated.
>
> **Never decide not to apply based on this field. Always read the real posting.**

---

## How discovery works

SproutJobs does not ship a company list. It finds boards from public sources at runtime,
and every candidate is verified against Ashby's own API before it becomes a company — so a
dead slug simply fails and is dropped rather than becoming a phantom company.

Discovery is **pluggable**: each source is an independent provider that can fail, time out or
be disallowed without affecting the others or your existing boards.

| Source | Default | Yield |
| --- | --- | --- |
| Public job-list repositories (`raw.githubusercontent.com`) | **on** | ~600 candidate boards |
| Your `seeds.txt` | **on** | whatever you add |
| Your own public page URLs | on, empty | whatever you add |
| Internet Archive CDX (`web.archive.org`) | **off** | ~5,000 candidate boards |
| Common Crawl index | **off** | disallowed by their `robots.txt` |
| Search endpoints | **off**, empty | opt-in only |

**Why the Internet Archive sweep is off by default:** it is not a policy objection — the
index publishes no `robots.txt` and is documented for programmatic use. It is off because it
takes discovery from ~600 boards to ~5,000, which means a first run of half an hour instead
of two minutes, a database of gigabytes instead of megabytes, and thousands of verification
requests to Ashby from every machine running this tool. Turn it on with `use_wayback = true`
or `./run.sh --deep` once you have decided you want it.

**Why Common Crawl is off:** `index.commoncrawl.org` serves `Disallow: /` with an allow-list
that does not cover the query endpoint this would need. They said no. `python -m jobfinder
compliance` reports enabling it as a **FAIL**.

Every source — enabled and disabled — is documented with its hostname, access method, license
position, `robots.txt` result, review date and reason in **[SOURCES.md](SOURCES.md)**.

Run `python -m jobfinder discovery-report` to see where every candidate slug you have came
from, and how much the sources overlap.

### Coverage, stated honestly

**SproutJobs does not cover every Ashby posting, and does not claim to.** It finds boards
that somebody has published a link to somewhere public. A company whose board has never been
linked or archived anywhere is invisible to it — add it to `seeds.txt` and it will be
tracked like any other.

---

## Duplicate collapsing

Employers sometimes publish one opening several times — one Ashby requisition per headcount
slot, posted seconds apart with identical text. The reports collapse those into a single
entry marked `6× posted`, while **every source record stays in SQLite**.

Two entries are treated as the same opportunity only when the company, title, location,
workplace type and employment type all match **and** the posting itself matches (same or
near-identical description, or a shared job/apply URL). Anything that could matter to an
applicant keeps its own card: a different city, team, seniority, pay band or set of
requirements. This is deliberately conservative, so you may still see two similar cards.

`duplicate_count` and `duplicate_source_ids` in the CSV show exactly what was merged.

---

## Privacy and data storage

- **Job data stays local.** One SQLite file at `data/jobfinder.sqlite3`, plus exports in
  `output/`. There is no hosted database, no account, no sync. Fetching public sources
  does send network requests to them; [SOURCES.md](SOURCES.md) lists every one.
- **No telemetry.** Nothing reports what you searched, opened, or applied to. Anywhere.
- **Nothing is collected about you.** SproutJobs stores public job postings. It has no
  concept of a user profile, and prompts for no password, resume or personal identifier.
- **You are not tracked by employers through it.** SproutJobs reads a public API; clicking
  **Apply** takes you to the employer's own page, where their normal rules apply.
- **Generated data is git-ignored** — the database, the discovery cache, `output/jobs.csv`
  and `output/index.html` never enter the repository. They contain copied text from real
  postings, which belongs to the employers who wrote it. Keep it local; see
  [SECURITY.md](SECURITY.md).

---

## Configuration

Everything lives in `config.toml`, and the shipped defaults work as-is. The two settings
most worth a look:

```toml
[candidate]
graduation_date = "2026-05-31"   # your expected graduation; run `rescore` after changing

[discovery]
use_wayback = false              # true for ~5,000 boards instead of ~600
```

`config.toml` is fully commented. Delete it entirely and the built-in defaults are identical.

---

## Limitations

Read these before trusting a result.

- **Scoring is heuristic.** It is meant to sort your reading order, not to make decisions.
- **Ashby only.** Roles hosted on Greenhouse, Lever, Workday or a company's own site are not
  here.
- **Coverage is partial.** Discovery only finds boards someone has publicly linked. See above.
- **Display names are derived from the slug** (`retell-ai` → "Retell Ai"). Ashby's public
  posting API does not return a company name; the slug is the stored identity.
- **The sponsorship parser is a screening aid**, with the limits spelled out above.
- **Geography tagging leans on Ashby's structured address**, present on most live postings.
  For the rest it falls back to city and state names, so an obscure U.S. town with no state
  beside it may end up untagged rather than under **Other US**.
- **Duplicate merging is display-only and conservative.** Near-identical postings that differ
  in pay, team or requirements stay separate.
- **Postings go stale between runs.** A role can close minutes after you fetch it.
- **After changing scoring rules, run `python -m jobfinder rescore`**, or stored jobs keep
  the verdict they got under the old rules.

> ### ⚠️ Verify against the original posting
>
> Every eligibility verdict, score, experience category and sponsorship state in SproutJobs
> is a **heuristic guess produced by pattern-matching English text**. It is wrong sometimes,
> in both directions — it will rule out roles you could get, and pass through roles you
> cannot.
>
> **Treat it as a reading order, never as a decision.** Before you apply — and before you
> decide *not* to apply — open the real posting and read it yourself. The employer's page is
> the only authority on what a job requires.

---

## Sources and compliance

- **[SOURCES.md](SOURCES.md)** — every endpoint touched, why, and why it ships on or off,
  with `robots.txt` findings and review dates.
- **[COMPLIANCE.md](COMPLIANCE.md)** — rate limiting, bounded concurrency, backoff, 429
  handling, no auth or CAPTCHA bypass, no auto-apply, local-only storage.
- **[SECURITY.md](SECURITY.md)** — reporting issues, and why the dashboard must stay on
  loopback.

Audit your own checkout:

```bash
python -m jobfinder compliance
```

SproutJobs is **not affiliated with, endorsed by, or approved by Ashby** or any company whose
postings it reads.

---

## Contributing

See **[CONTRIBUTING.md](CONTRIBUTING.md)** for setup, architecture, how to add a discovery
provider, how to add parser rules, and the non-negotiables (no paid dependencies, no
prohibited scraping, deterministic scoring, regression tests for every rule change).

```bash
python -m pytest              # offline, deterministic
python -m pytest -m integration   # opt-in live checks
python -m jobfinder compliance
```

Bug reports are welcome, especially "this posting was scored wrong" with the URL and what you
expected.

---

## License

[MIT](LICENSE).

The license covers this source code, documentation and tests. It does not cover job postings
SproutJobs fetches when you run it — that text belongs to the employers who wrote it, is
stored only on your machine, and is not part of this repository.

---

## Disclaimer

SproutJobs is provided as-is, with no warranty. Its eligibility verdicts, scores and
sponsorship readings are **heuristic and frequently imprecise**, and must be verified against
the original posting before you act on them. Nothing here is legal, immigration or career
advice. You are responsible for how you use this tool and for complying with the terms and
law that apply to you.
