# Sources

Every network endpoint SproutJobs touches, why it touches it, and why it ships
enabled or disabled.

**Review date for every entry below: 2026-08-24.** Each `robots.txt` result was
fetched on that date and is reproduced verbatim. Policies change; re-check
before relying on any of this.

This document records what was observed and what was decided. It does **not**
claim that any operator has approved, endorsed, or permitted this project. No
such approval has been sought or given. See [COMPLIANCE.md](COMPLIANCE.md).

---

## Summary

| Source | Host | Role | Auth | Default |
|---|---|---|---|---|
| Ashby Job Posting API | `api.ashbyhq.com` | Ingestion (the core) | None | **Enabled** |
| Ashby public boards | `jobs.ashbyhq.com` | Apply links only, never fetched | None | **Enabled** |
| Public job-list repositories | `raw.githubusercontent.com` | Discovery | None | **Enabled** |
| User seed file | local disk | Discovery | n/a | **Enabled** |
| User-configured public pages | any | Discovery | None | Enabled, ships empty |
| Internet Archive CDX | `web.archive.org` | Discovery | None | **Disabled** — cost |
| Common Crawl index | `index.commoncrawl.org` | Discovery | None | **Disabled** — robots.txt |
| Search endpoint | any | Discovery | varies | **Disabled** — ships empty |

Sources SproutJobs deliberately does **not** have, and will not accept:
LinkedIn or any other site scraped against its terms; Google, Bing or any other
general search engine scraped to recover coverage; anything behind a login,
a paywall, or a CAPTCHA; any endpoint reached with rotated user agents,
rotated addresses, or a headless browser pretending to be a person.

---

## Ingestion

### Ashby Job Posting API — **enabled**

- **Host:** `api.ashbyhq.com`
- **Endpoint:** `GET /posting-api/job-board/{slug}?includeCompensation=true`
- **Purpose:** The core of the tool. Returns the complete current public job
  board for one company, as JSON.
- **Access:** Plain HTTPS GET. No account, no key, no token, no cookie.
- **Authentication required:** No.
- **License:** Not applicable — this is an API response, not a dataset. The job
  text belongs to the employers who wrote it. SproutJobs stores it locally for
  the person running the tool and redistributes none of it.
- **Robots/terms notes:**
  - `https://api.ashbyhq.com/robots.txt` returns **HTTP 401**. That is the API
    gateway declining an unauthenticated request for a path it does not serve,
    not a crawl directive — there is no policy document to read. It is recorded
    here because it is the kind of detail that should not be quietly omitted.
  - This is Ashby's public, unauthenticated posting endpoint: the same one that
    serves employers' embedded job boards. On the review date it responded
    `HTTP 200` with `Access-Control-Allow-Origin: *` and
    `Cache-Control: public, max-age=60`, which is how a deliberately public,
    cacheable endpoint is configured.
  - SproutJobs is a read-only client of it. It never posts, never applies,
    never authenticates.
- **Why enabled:** It is the entire point of the project, it is public and
  keyless by design, and every posting is linked back to the employer's own
  application page rather than re-hosted.
- **Politeness:** 4 workers by default, configurable up to 8, one request
  every 0.15s globally across all of them, 20s timeout, 3 retries with
  doubling backoff, and HTTP 429 handled by honoring the server's own
  `Retry-After`.

### Ashby public boards — **enabled (as links)**

- **Host:** `jobs.ashbyhq.com`
- **Purpose:** Constructing the **Apply** / **View job** URL for each posting so
  you apply on the employer's real page.
- **Access:** SproutJobs builds these URLs. It does not fetch them. Every
  posting's content comes from the JSON API above.
- **Robots/terms notes:** `https://jobs.ashbyhq.com/robots.txt` disallows
  `/meeting/`, `/b/` and `/api/`. SproutJobs's slug filter independently
  refuses `meeting`, `b` and `api` as board names, so it cannot construct a URL
  under a disallowed path even by accident.
- **Why enabled:** Sending a person to the original posting is the correct
  behavior. Nothing is scraped.

---

## Discovery

Discovery answers one narrow question: *which Ashby board slugs might exist?*
Every answer is an unverified guess until Ashby's own API returns a real board
for it. Nothing enumerates or brute-forces slugs, and no source can create a
company on its own.

### Public job-list repositories — **enabled**

- **Provider id:** `public_dataset`
- **Host:** `raw.githubusercontent.com`
- **Purpose:** These are community-maintained new-grad and internship lists that
  already publish Ashby board links. Reading them yields roughly **600**
  candidate board slugs.
- **Access:** One HTTPS GET per file, at most once every 24 hours (cached).
  Thirteen files total. No account, no key.
- **Authentication required:** No.
- **Robots/terms notes:** `https://raw.githubusercontent.com/robots.txt`
  returned **HTTP 404** on the review date — no crawl policy is published for
  that host. SproutJobs requests it anyway before every fetch, so the day one
  appears it is honored without a code change.
- **License — read this part carefully:**
  **None of the thirteen repositories carries a license file.** Checked via the
  GitHub API on the review date; every one reported no license. Under GitHub's
  default terms that means the authors retain their rights, and a permissive
  license cannot be assumed.

  What SproutJobs does with that constraint:
  - It **redistributes none of their content.** No slug list, no listing, no
    README text, and no derived dataset is committed to this repository. Look
    at the tracked files: there is no seed list to ship.
  - It reads them **at runtime, on your machine**, and keeps only the extracted
    board slugs — short factual identifiers such as `notion` — discarding the
    documents themselves. The on-disk cache stores slugs, never bodies.
  - It fetches thirteen small files once a day at most.

  That is a considered position, not a legal conclusion. If you maintain one of
  these repositories and would rather not be read this way, open an issue and
  the URL will be removed.
- **Why enabled:** Public repository files, no published crawl restriction,
  negligible load, nothing redistributed, and it is what makes a first run
  useful in two minutes.
- **The thirteen documents:**

  | Repository | Files read | License |
  |---|---|---|
  | `SimplifyJobs/New-Grad-Positions` | `listings.json`, `README.md` | none |
  | `SimplifyJobs/Summer2026-Internships` | `listings.json` | none |
  | `vanshb03/New-Grad-2026` | `README.md` | none |
  | `vanshb03/Summer2026-Internships` | `README.md` | none |
  | `speedyapply/2026-SWE-College-Jobs` | `NEW_GRAD_USA.md`, `README.md` | none |
  | `pittcsc/Summer2026-Internships` | `README.md` | none |
  | `Ouckah/Summer2025-Internships` | `README.md` | none |
  | `coderQuad/New-Grad-Positions` | `README.md` | none |
  | `ReaVNaiL/New-Grad-2024` | `README.md` | none |
  | `jobright-ai/2025-Software-Engineer-New-Grad` | `README.md` | none |
  | `cvrve/New-Grad-2025` | `README.md` | none |

  Remove any of them by deleting its line from `discovery.public_dataset_urls`
  in `config.toml`. Nothing else changes.

### User seed file — **enabled**

- **Provider id:** `user_seed_file` (and `user_seed` for `add-company` / `add-url`)
- **Source:** `seeds.txt` on your own disk.
- **Purpose:** Track a company the automated sources have never heard of.
- **Access:** A local file read. No network.
- **Authentication required:** No. **License:** Yours.
- **Why enabled:** It is your file. Seeds are still verified against Ashby, so
  a typo fails to verify instead of creating a phantom company.
- **Note:** The tracked `seeds.txt` contains comments only — no slugs. This
  repository ships no company list of any kind.

### User-configured public pages — **enabled, ships empty**

- **Provider id:** `public_page`
- **Host:** whatever you put in `discovery.public_page_urls`.
- **Purpose:** Mine Ashby board links out of any other public page.
- **Access:** HTTPS GET, cached for 24 hours, `robots.txt` checked first and a
  refusal reported rather than worked around.
- **Authentication required:** No, and pages needing one are not supported.
- **Enabled by default:** The provider is on; the list is empty, so it does
  nothing until you add a URL.
- **Why:** It is the extension point that keeps you from needing to patch code.
  Whether a given page may be read automatically is your call, on that page's
  own terms.

### Internet Archive Wayback CDX index — **disabled by default**

- **Provider id:** `wayback`
- **Host:** `web.archive.org`
- **Endpoint:** `GET /cdx/search/cdx?url=jobs.ashbyhq.com&matchType=domain&…`
- **Purpose:** Reports every `jobs.ashbyhq.com` path anyone has ever archived.
  Measured on the review date: one request, 197,364 rows, **5,016** distinct
  candidate slugs — roughly eight times what the repository sources give.
- **Access:** One HTTPS GET. No account, no key. The CDX index is a documented
  programmatic interface, not a page being scraped.
- **Authentication required:** No.
- **Robots/terms notes:** `https://web.archive.org/robots.txt` returned
  **HTTP 404** on the review date; no crawl policy is published for that host.
  SproutJobs requests it before querying regardless.
- **License:** Not applicable. Only URL strings are read — no archived page
  content is fetched, stored, or redistributed.
- **Why disabled by default — and this is not a policy objection:**
  Nothing observed suggests this query is unwelcome, and turning it on is a
  reasonable thing to do. It is off because of what it costs *other people* and
  *you*:
  1. It takes discovery from ~600 candidate boards to ~5,000, which means
     roughly 5,000 verification requests to **Ashby** on first run — from every
     machine running SproutJobs, not just one. A default that multiplies load
     on a third party across an unknown number of public users is the wrong
     default even when each individual request is fine.
  2. First run goes from about two minutes to well over half an hour.
  3. The database grows from megabytes to gigabytes, because it keeps the full
     text of every posting it has ever seen.

  A tool that is unpleasant on first run does not get a second one. Breadth is
  the right choice for someone who has decided they want it, not the right
  choice to impose on a stranger who just cloned the repository.
- **How to enable:** `use_wayback = true` in `config.toml`, or `./run.sh --deep`
  for a single run. Please leave `wayback_limit` where it is.

### Common Crawl URL index — **disabled, and it should stay disabled**

- **Provider id:** `common_crawl`
- **Host:** `index.commoncrawl.org`
- **Endpoint that would be needed:** `GET /CC-MAIN-{...}-index?url=…`
- **Purpose:** The same question as the Wayback index, from a different crawl.
- **Access:** Would be an HTTPS GET, no key.
- **Robots/terms notes — this is the deciding fact.**
  `https://index.commoncrawl.org/robots.txt` returned this on the review date:

  ```
  User-agent: *
  Disallow: /

  Allow: /$
  Allow: /index.html$
  Allow: /web-graphs-index.html$
  Allow: /collinfo.json$
  Allow: /graphinfo.json$
  Allow: /ccbot.json$
  Allow: /.well-known/*.txt$
  ```

  The site disallows everything and then permits seven specific paths. The
  `CC-MAIN-*-index` query endpoint is not among them. The refusal is explicit.
- **License:** Common Crawl publishes its corpus under permissive terms. That is
  irrelevant here: the question is not whether the *data* may be used but
  whether *this endpoint* may be queried automatically, and the answer given is
  no.
- **Why disabled:** Because they said no. SproutJobs does not route around a
  stated refusal — not with a different user agent, not through a mirror, not
  via a proxy. `python -m jobfinder compliance` reports this source being
  enabled as a **FAIL**, so it cannot be turned on quietly.
- **What it costs:** Almost nothing. When it was last measured against a full
  Wayback sweep it contributed 141 slugs the Wayback index did not already
  have.
- **The adapter is still in the tree** (`jobfinder/discovery/archives.py`), and
  it correctly refuses itself: it checks `robots.txt` first and reports
  `skipped: disallowed by robots.txt` rather than proceeding. It stays because
  deleting it would not make the decision visible, and because the policy could
  change.

### Search endpoint — **disabled, ships empty**

- **Provider id:** `search_engine`
- **Host:** whatever you configure. Nothing is configured.
- **Purpose:** Mining Ashby links out of search results.
- **Why disabled:** Most large search engines forbid automated querying in their
  terms of service. SproutJobs does not work around that, and **does not add
  Google or Bing scraping to recover the coverage lost by disabling other
  sources.** Losing coverage is the correct outcome of respecting a refusal.
- **What it is for:** The genuine case where you have an endpoint you are
  allowed to query — a SearXNG instance you host yourself, an institutional
  search API. Point `discovery.search_endpoints` at a URL template containing
  `{query}`.
- **If you configure one,** the compliance audit reports a **WARN**, because it
  cannot know whether you are permitted to query it. That is a question for you
  and that endpoint's terms.

---

## Adding a source

The provider interface is one class with a `name` and a `discover()` method;
see [CONTRIBUTING.md](CONTRIBUTING.md). A new provider does not touch ingestion,
storage, scoring, eligibility, canonicalization or the dashboard.

A source is only accepted here if it is free, keyless, and reachable without
defeating any access control — and if it can be given an honest entry in this
file, including a real robots/terms note and a real license answer.
