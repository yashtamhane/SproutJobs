# Compliance and engineering safeguards

## What this document is

A description of how SproutJobs behaves as a network client, and of the
safeguards built into it.

## What this document is not

**This is not legal advice, and it is not a claim that anything here is
lawful, permitted, or approved.**

Specifically, this project does **not** claim to be "100% legal", does **not**
claim to be "fully compliant" with anything, and is **not approved by, endorsed
by, affiliated with, or connected to Ashby** or any other company named in it.
No permission has been sought from or granted by any operator.

The safeguards below are engineering decisions made in good faith. Whether they
are sufficient for *your* situation depends on where you are, what you do with
the results, and the terms that apply to you. **You are responsible for your own
compliance with applicable terms of service and law.** If that matters to your
use, get advice from someone qualified to give it.

---

## Safeguards

### Public, documented endpoints are preferred

Job data comes from Ashby's public, unauthenticated posting endpoint — the same
JSON API that serves employers' own embedded job boards — rather than from
scraping rendered pages. Discovery reads openly published documents and a public
archive index. Every source is itemised in [SOURCES.md](SOURCES.md), with what
was observed on a stated review date.

### Rate limiting

A minimum interval separates the *start* of any two outbound requests
(`network.min_request_interval`, default 0.15s). It is enforced with a lock
shared by every worker thread, so the ceiling is global rather than per-thread —
raising concurrency does not raise the request rate.

### Bounded concurrency

`network.concurrency` defaults to 4 and the configuration validator refuses any
value above 8. There is no setting that turns SproutJobs into a wide fan-out
against anyone's infrastructure.

### Timeouts, retries and backoff

Every request carries a timeout (`network.request_timeout`, default 20s). Failed
requests are retried a bounded number of times (default 3) with an exponentially
growing delay plus jitter, so several workers do not retry in lockstep.

### HTTP 429 handling

A 429 response is treated as retryable, and the server's own `Retry-After`
header is honored — both the seconds form and the HTTP-date form — capped at
120 seconds. When a server asks for a pause, it gets one.

### robots.txt is consulted, and a refusal is accepted

Before any discovery fetch, the host's `robots.txt` is retrieved, cached for the
run, and evaluated with `*` and `$` wildcard support. A disallowed URL is
skipped and reported in the run summary.

A refusal is never worked around. Not with a different `User-Agent`, not through
a proxy or a mirror, not by trying a different entry point. Common Crawl's index
is disabled by default for exactly this reason, and the compliance audit reports
enabling it as a failure.

### No authentication bypass

SproutJobs reads no endpoint that requires an account. It sends no cookies, no
tokens and no credentials. It has no login flow. It cannot access private,
member-only, or logged-in data, and no configuration makes it able to.

### No CAPTCHA bypass

There is no CAPTCHA solver, no CAPTCHA-solving service client, no TLS
fingerprint impersonation, no `User-Agent` rotation, and no address rotation. If
something interposes a challenge, SproutJobs fails that request and reports it.

### No browser automation

No headless browser is driven — no Selenium, Playwright, Puppeteer or
equivalent. SproutJobs reads documented JSON over HTTPS.

### Honest identification

Requests carry a `User-Agent` naming the tool and its version. It does not
impersonate a browser. You are encouraged to append your own contact URL so an
operator who wants to talk to you about your traffic can.

### No automatic applications

**SproutJobs never applies to anything.** The package makes no HTTP POST, PUT,
PATCH or DELETE request anywhere — every remote interaction is a read. It
creates no accounts, submits no forms, uploads no files, and sends no messages
to employers.

### No applicant credential collection

Nothing prompts for or stores a password, a resume, a phone number, a date of
birth, or any other personal identifier. The database contains public job
postings and nothing about the person running the tool.

### Direct linking to the original application

Every result links to the employer's own posting on `jobs.ashbyhq.com`.
SproutJobs is a way to find the posting, not a replacement for it — the
application always happens on the employer's page, in your hands.

### Local-only storage

The database is a SQLite file inside your own project directory. The dashboard
is an HTTP server bound to `127.0.0.1`. There is no hosted database, no account,
no sync, no telemetry, and no analytics. Nothing you do with SproutJobs is
reported anywhere.

### Generated job data stays local and untracked

Fetched postings, the CSV export, the HTML export and the discovery cache are
all covered by `.gitignore`. This repository contains code, rules, docs and
tests — **no job data is published with it**, and no company or slug list is
shipped. The compliance audit checks both the ignore rules and, in a Git
checkout, the actual tracked file list.

---

## Verifying it yourself

```bash
python -m jobfinder compliance
```

This audits *this checkout and this configuration* mechanically: whether
concurrency is bounded, a timeout is set, 429 is handled, the server binds to
loopback, generated data is ignored, a forbidden source has been enabled, a paid
client or browser driver has crept in, an HTTP write call exists, and more.

It reports `PASS`, `WARN` or `FAIL` per check, and exits non-zero only on a
`FAIL` — so unresolved questions that need a human decision can stay visible as
warnings without blocking anything.

**It is an engineering audit. It does not, and cannot, tell you that anything is
legal.** A `PASS` line means a specific technical property was verified, and
nothing more than that.

---

## Reporting a problem

If you operate a service SproutJobs reads and you want it to stop, or you
maintain one of the repositories listed in [SOURCES.md](SOURCES.md) and would
rather not be read: open an issue. The source will be removed. No argument, no
justification asked for.

Security issues: see [SECURITY.md](SECURITY.md).
