"""The live dashboard shell served by the local HTTP server.

Unlike the static export, this page ships **no job data**.  It renders the
filter controls and an empty results area, then asks ``/api/jobs`` for one page
at a time.  Every filter, count and sort therefore runs in SQL over the whole
canonical opportunity set rather than over whatever subset a file happened to
contain — which is the entire point of moving off the static report.

The card markup is deliberately the same shape the static report produces, so
the two surfaces look identical and the styles are shared rather than forked.
"""

from __future__ import annotations

from typing import Any, Mapping

from .experience import FILTER_LABELS as EXPERIENCE_FILTERS
from .geo import GEO_TAGS, label_for as geo_label
from .reporting import _STYLES, _e


_FRESHNESS_OPTIONS = (
    ("all", "Any time"),
    ("today", "Today"),
    ("1", "Last 24 hours"),
    ("3", "Last 3 days"),
    ("7", "Last 7 days"),
    ("14", "Last 14 days"),
    ("30", "Last 30 days"),
)
_CATEGORY_OPTIONS = (
    ("all", "All categories"),
    ("strong_match", "Strong Match"),
    ("possible_match", "Possible Match"),
    ("weak_match", "Weak Match"),
    ("likely_ineligible", "Likely Ineligible"),
)
_SORT_OPTIONS = (
    ("best", "Best match"),
    ("entry_level", "Best entry-level fit"),
    ("published", "Newest published"),
    ("discovered", "Newest discovered"),
    ("company", "Company A–Z"),
    ("compensation", "Highest compensation"),
)


def render_shell(summary: Mapping[str, Any] | None = None) -> str:
    """Render the data-free dashboard page."""

    totals = dict(summary or {})
    opportunities = int(totals.get("opportunities") or 0)
    stats = (
        (int(totals.get("active_jobs") or 0), "Active postings"),
        (int(totals.get("software_jobs") or 0), "Software roles"),
        (opportunities, "Opportunities searchable"),
        (int(totals.get("strong_matches") or 0), "Strong matches"),
    )
    stat_cells = "".join(
        f'<div class="stat"><strong>{value:,}</strong><span>{_e(label)}</span></div>\n'
        for value, label in stats
    )
    geo_checkboxes = "".join(
        f'<label class="chip"><input type="checkbox" class="geo-check" value="{_e(tag)}">'
        f'<span>{_e(geo_label(tag))} <small data-geo-count="{_e(tag)}">–</small></span></label>'
        for tag in GEO_TAGS
    )
    experience_checkboxes = "".join(
        f'<label class="chip"><input type="checkbox" class="experience-check" value="{_e(value)}">'
        f'<span>{_e(label)} <small data-exp-count="{_e(value)}">–</small></span></label>'
        for value, label in EXPERIENCE_FILTERS
    )
    category_options = "".join(
        f'<option value="{_e(value)}">{_e(label)}</option>' for value, label in _CATEGORY_OPTIONS
    )
    freshness_options = "".join(
        f'<option value="{_e(value)}">{_e(label)}</option>' for value, label in _FRESHNESS_OPTIONS
    )
    sort_options = "".join(
        f'<option value="{_e(value)}">{_e(label)}</option>' for value, label in _SORT_OPTIONS
    )
    page_size_options = "".join(
        f'<option value="{size}"{" selected" if size == 50 else ""}>{size} per page</option>'
        for size in (25, 50, 100)
    )

    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        "<title>SproutJobs</title>\n"
        + _STYLES
        + _EXTRA_STYLES
        + "</head>\n<body>\n"
        '<a class="skip-link" href="#results">Skip to job results</a>\n'
        '<header class="hero">\n<div class="hero-inner">\n'
        '<div class="eyebrow"><span aria-hidden="true">✦</span> LOCAL JOB DISCOVERY</div>\n'
        "<h1>SproutJobs</h1>\n"
        '<p class="lede">Every stored opportunity is searchable here — filters and counts run '
        "against the full database, not a rendered sample.</p>\n"
        '<div class="summary-grid" aria-label="Report summary">\n'
        + stat_cells
        + "</div>\n"
        '<p class="generated">Served locally from your own database. '
        "Nothing leaves this machine.</p>\n"
        "</div>\n</header>\n"
        '<main class="layout">\n'
        '<section class="filters" aria-labelledby="filters-heading">\n'
        '<div class="filter-heading"><div><p class="kicker">Refine</p>'
        '<h2 id="filters-heading">Find your next role</h2></div>'
        '<button id="resetFilters" class="reset" type="button">Reset filters</button></div>\n'
        '<div class="filter-grid">\n'
        '<label class="field field-wide"><span>Search everything</span>'
        '<input id="searchInput" type="search" placeholder="Company, title, location, reason…" autocomplete="off"></label>\n'
        '<label class="field"><span>Title keyword</span>'
        '<input id="titleInput" type="search" placeholder="e.g. backend"></label>\n'
        '<label class="field"><span>Company</span>'
        '<input id="companyInput" type="search" placeholder="Exact company name" list="companyList">'
        '<datalist id="companyList"></datalist></label>\n'
        '<label class="field"><span>Match category</span><select id="categoryFilter">'
        + category_options
        + "</select></label>\n"
        '<label class="field"><span>Status</span><select id="statusFilter">'
        '<option value="active" selected>Active only</option>'
        '<option value="all">Active + closed</option>'
        '<option value="closed">Closed only</option></select></label>\n'
        '<label class="field"><span>Role type</span><select id="softwareFilter">'
        '<option value="software" selected>Software roles</option>'
        '<option value="all">All roles</option>'
        '<option value="other">Other roles</option></select></label>\n'
        '<label class="field"><span>Location contains</span>'
        '<input id="locationInput" type="search" placeholder="NYC, New Jersey, US…"></label>\n'
        '<label class="field"><span>Workplace</span><select id="remoteFilter">'
        '<option value="all">Any workplace</option>'
        '<option value="remote">Remote / distributed</option>'
        '<option value="onsite">Not marked remote</option></select></label>\n'
        '<label class="field"><span>Evidence confidence</span><select id="confidenceFilter">'
        '<option value="all">Any confidence</option>'
        '<option value="high">High only</option>'
        '<option value="medium">Medium or better</option>'
        '<option value="low">Low only</option></select></label>\n'
        '<label class="field"><span>Eligibility</span><select id="blockerFilter">'
        '<option value="all">Any status</option>'
        '<option value="clear">No blockers</option>'
        '<option value="review">Needs review</option>'
        '<option value="blocked">Hard blocker</option></select></label>\n'
        '<label class="field"><span>Authorization notes</span><select id="authFilter">'
        '<option value="all">Show all</option>'
        '<option value="clear">No explicit warning</option>'
        '<option value="flagged">Any warning</option>'
        '<option value="uncertain">Uncertain / review</option>'
        '<option value="restriction">Explicit restriction</option></select></label>\n'
        '<label class="field"><span>Freshness source</span><select id="freshnessSource">'
        '<option value="published">Date published</option>'
        '<option value="discovered">First discovered</option></select></label>\n'
        '<label class="field"><span>Freshness</span><select id="freshnessFilter">'
        + freshness_options
        + "</select></label>\n"
        '<label class="field"><span>Minimum score: '
        '<output id="scoreOutput" for="scoreFilter">0</output></span>'
        '<input id="scoreFilter" type="range" min="0" max="100" step="5" value="0"></label>\n'
        '<label class="field"><span>Sort by</span><select id="sortBy">'
        + sort_options
        + "</select></label>\n"
        '<label class="field"><span>Page size</span><select id="pageSize">'
        + page_size_options
        + "</select></label>\n"
        '<label class="check-field"><input id="newOnly" type="checkbox">'
        "<span><strong>New only</strong><small>First found in the latest run</small></span></label>\n"
        "</div>\n"
        '<div class="chip-group" id="geoGroup" role="group" aria-labelledby="geo-legend">\n'
        '<p class="chip-legend" id="geo-legend">Geography '
        "<small>tick any combination; none ticked shows every U.S. region</small></p>\n"
        '<div class="chips">'
        + geo_checkboxes
        + "</div>\n</div>\n"
        '<div class="chip-group" id="experienceGroup" role="group" aria-labelledby="experience-legend">\n'
        '<p class="chip-legend" id="experience-legend">Experience evidence '
        "<small>&ldquo;Experience unknown&rdquo; means the posting never said — those roles may still fit</small></p>\n"
        '<div class="chips">'
        + experience_checkboxes
        + "</div>\n</div>\n"
        "</section>\n"
        '<section id="results" class="results" aria-labelledby="results-heading">\n'
        '<div class="results-bar"><div><p class="kicker">Results</p>'
        '<h2 id="results-heading"><span id="visibleCount">…</span> matching opportunities</h2></div>'
        '<p id="resultStatus" class="result-status" role="status" aria-live="polite">Loading…</p></div>\n'
        '<div id="emptyState" class="empty" hidden><span aria-hidden="true">⌁</span>'
        "<h3>No jobs match these filters</h3>"
        "<p>Try lowering the score or clearing a location, status, or freshness filter.</p>"
        '<button id="emptyReset" type="button">Reset filters</button></div>\n'
        '<div id="jobList" class="job-list"></div>\n'
        + _PAGER
        + "</section>\n</main>\n"
        "<footer><p>Work-authorization notes are keyword-based screening aids, not legal advice. "
        "Always verify the full posting before applying.</p></footer>\n"
        + _SCRIPT
        + "</body>\n</html>\n"
    )


_PAGER = (
    '<nav class="pager" id="pager" aria-label="Pagination">\n'
    '<button id="firstPage" type="button" class="page-button">« First</button>\n'
    '<button id="prevPage" type="button" class="page-button">‹ Previous</button>\n'
    '<span class="page-status" id="pageStatus">Page 1 of 1</span>\n'
    '<button id="nextPage" type="button" class="page-button">Next ›</button>\n'
    '<button id="lastPage" type="button" class="page-button">Last »</button>\n'
    "</nav>\n"
)

_EXTRA_STYLES = """<style>
.pager { display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: .55rem; margin-top: 1.6rem; }
.page-button { padding: .5rem .9rem; border: 1px solid var(--line); border-radius: 8px; background: var(--card); color: var(--ink); font: inherit; font-size: .8rem; font-weight: 700; cursor: pointer; }
.page-button:hover:not(:disabled) { border-color: var(--green); color: var(--green); }
.page-button:disabled { opacity: .4; cursor: default; }
.page-status { padding: 0 .6rem; color: var(--muted); font-size: .8rem; font-weight: 700; }
.loading .job-list { opacity: .45; transition: opacity .12s ease; }
.detail-toggle { margin-top: .75rem; padding: .4rem .7rem; border: 1px dashed var(--line); border-radius: 8px; background: transparent; color: var(--muted); font: inherit; font-size: .74rem; font-weight: 700; cursor: pointer; }
.detail-toggle:hover { border-color: var(--green); color: var(--green); }
.detail-body { margin-top: .7rem; padding: .8rem; border-radius: 9px; background: rgba(127,127,127,.07); font-size: .82rem; line-height: 1.55; white-space: pre-wrap; max-height: 22rem; overflow-y: auto; }
@media (prefers-color-scheme: dark) {
  .page-button { background: #121a16; border-color: #3b4841; }
}
</style>
"""

_SCRIPT = r"""<script>
(function () {
  "use strict";
  const byId = (id) => document.getElementById(id);
  const controls = {
    search: byId("searchInput"), title: byId("titleInput"), company: byId("companyInput"),
    category: byId("categoryFilter"), status: byId("statusFilter"), software: byId("softwareFilter"),
    location: byId("locationInput"), remote: byId("remoteFilter"), confidence: byId("confidenceFilter"),
    blocker: byId("blockerFilter"), authorization: byId("authFilter"),
    freshness_source: byId("freshnessSource"), freshness: byId("freshnessFilter"),
    min_score: byId("scoreFilter"), sort: byId("sortBy"), page_size: byId("pageSize"),
    new_only: byId("newOnly")
  };
  const defaults = {};
  Object.entries(controls).forEach(([key, el]) => {
    defaults[key] = el.type === "checkbox" ? el.checked : el.value;
  });
  const geoBoxes = Array.from(document.querySelectorAll(".geo-check"));
  const expBoxes = Array.from(document.querySelectorAll(".experience-check"));
  const list = byId("jobList");
  let page = 1;
  let totalPages = 1;
  let pending = null;

  const esc = (value) => String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const checked = (boxes) => boxes.filter((b) => b.checked).map((b) => b.value);

  function params(extra) {
    const query = new URLSearchParams();
    Object.entries(controls).forEach(([key, el]) => {
      const value = el.type === "checkbox" ? (el.checked ? "1" : "") : el.value;
      if (value && value !== "all") query.set(key, value);
    });
    checked(geoBoxes).forEach((v) => query.append("geo", v));
    checked(expBoxes).forEach((v) => query.append("experience", v));
    Object.entries(extra || {}).forEach(([k, v]) => query.set(k, v));
    return query;
  }

  function card(job, index) {
    const badges = [
      `<span class="badge category category-${esc(job.category)}">${esc(job.category_display)}</span>`
    ];
    if (job.new_since_previous_run) badges.push('<span class="badge new-badge">New</span>');
    if (!job.active) badges.push('<span class="badge closed-badge">Closed</span>');
    if (job.remote) badges.push('<span class="badge remote-badge">Remote</span>');
    if (job.duplicate_count > 1) {
      badges.push(`<span class="badge duplicate-badge" title="This employer posted this same opening ${job.duplicate_count} times">${job.duplicate_count}× posted</span>`);
    }
    (job.geo_tags || []).forEach((tag, i) => {
      badges.push(`<span class="badge geo-badge geo-${esc(tag)}">${esc((job.geo_labels || [])[i] || tag)}</span>`);
    });

    const detail = (label, value, cls) =>
      `<div${cls ? ` class="${cls}"` : ""}><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`;
    const details = [
      detail("Experience", job.experience_summary || job.experience_label,
             job.experience_known ? "experience-known" : "experience-unknown")
    ];
    if (job.workplace_type) details.push(detail("Workplace", job.workplace_type));
    if (job.employment_type) details.push(detail("Employment", job.employment_type));
    if (job.department) details.push(detail("Team", job.department));
    if (job.compensation) details.push(detail("Compensation", job.compensation, "compensation"));
    details.push(detail("Published", job.published_display));
    details.push(detail("First discovered", job.first_seen_display));
    details.push(detail("Entry-level fit", `${job.entry_level_score} / 100`));

    const reasons = job.score_reasons || [];
    const supporting = reasons.filter((r) => (r.delta || 0) > 0).sort((a, b) => (b.delta || 0) - (a.delta || 0));
    const against = reasons.filter((r) => (r.delta || 0) < 0);
    const neutral = reasons.filter((r) => !(r.delta || 0));
    const ordered = supporting.concat(against, neutral);
    const realSupport = supporting.filter((r) => (r.confidence || "low") !== "low");
    let heading = "Why this scores low";
    if (realSupport.length) heading = "Why this suits an entry-level candidate";
    else if (!job.experience_known) heading = "Experience requirement not stated";
    const items = ordered.slice(0, 6).map((r) => {
      const value = Number(r.delta || 0);
      const delta = value ? `<span class="delta">${value > 0 ? "+" : ""}${value}</span>` : "";
      return `<li${value < 0 ? ' class="against"' : ""}>${delta}<span>${esc(r.label)}</span></li>`;
    });
    if (ordered.length > items.length) {
      items.push(`<li class="more">+${ordered.length - items.length} more signal(s)</li>`);
    }
    const note = (!realSupport.length && job.experience_detail)
      ? `<p class="evidence-note">${esc(job.experience_detail)}</p>` : "";
    const evidence = ordered.length
      ? `<div class="evidence"><h4>${esc(heading)} <span class="confidence confidence-${esc(job.evidence_confidence)}">${esc(job.evidence_confidence)} confidence</span></h4>${note}<ul class="reason-list">${items.join("")}</ul></div>`
      : "";

    const warnings = (job.warning_flags || []).slice();
    if (job.authorization_state !== "clear" && warnings.indexOf(job.authorization_flag) === -1) {
      warnings.unshift(job.authorization_flag);
    }
    const warningHtml = warnings.length
      ? `<div class="warnings" role="note"><strong>Review before applying</strong><ul>${warnings.slice(0, 5).map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>`
      : `<p class="auth-clear"><span aria-hidden="true">✓</span> ${esc(job.authorization_flag)}${(job.notes || []).length ? " · " + esc((job.notes || []).join("; ")) : ""}</p>`;

    const jobLink = job.job_url
      ? `<a class="button secondary" href="${esc(job.job_url)}" target="_blank" rel="noopener noreferrer">View Job <span aria-hidden="true">↗</span></a>`
      : '<span class="button secondary disabled" aria-disabled="true">Job link unavailable</span>';
    const applyLink = job.apply_url
      ? `<a class="button apply" href="${esc(job.apply_url)}" target="_blank" rel="noopener noreferrer">Apply <span aria-hidden="true">→</span></a>`
      : '<span class="button apply disabled" aria-disabled="true">Apply link unavailable</span>';
    const secondary = job.secondary_locations
      ? `<p class="secondary-location">Also: ${esc(job.secondary_locations)}</p>` : "";

    return `<article class="job-card" id="job-${index}" data-id="${esc(job.id)}"
      data-category="${esc(job.category)}" data-experience="${esc(job.experience_category)}"
      data-experience-bucket="${esc(job.experience_bucket)}" data-blocker="${esc(job.blocker_status)}"
      data-geo="${esc((job.geo_tags || []).join(" "))}" data-auth="${esc(job.authorization_state)}"
      data-confidence="${esc(job.evidence_confidence)}" data-score="${esc(job.score)}"
      data-status="${job.active ? "active" : "closed"}">
      <div class="card-main">
      <div class="score" aria-label="Match score ${esc(job.score_display)} out of 100"><strong>${esc(job.score_display)}</strong><span>/ 100</span></div>
      <div class="job-copy"><div class="badges">${badges.join("")}</div>
      <p class="company">${esc(job.company)}</p><h3>${esc(job.title)}</h3>
      <p class="location"><span aria-hidden="true">⌖</span> ${esc(job.location)}</p>${secondary}
      </div></div>
      <dl class="details">${details.join("")}</dl>
      ${evidence}${warningHtml}
      <div class="actions">${jobLink}${applyLink}</div>
      <button class="detail-toggle" type="button" data-detail="${esc(job.id)}">Show full description</button>
      </article>`;
  }

  function renderFacets(facets) {
    Object.entries(facets.geo || {}).forEach(([tag, count]) => {
      const el = document.querySelector(`[data-geo-count="${tag}"]`);
      if (el) el.textContent = count.toLocaleString();
    });
    Object.entries(facets.experience || {}).forEach(([bucket, count]) => {
      const el = document.querySelector(`[data-exp-count="${bucket}"]`);
      if (el) el.textContent = count.toLocaleString();
    });
    const blockerEl = document.querySelector('[data-exp-count="blocker"]');
    if (blockerEl && facets.blocker) blockerEl.textContent = (facets.blocker.blocked || 0).toLocaleString();
    const datalist = byId("companyList");
    if (datalist && facets.companies) {
      datalist.innerHTML = facets.companies.slice(0, 1200)
        .map((c) => `<option value="${esc(c.name)}">${esc(c.name)} (${c.count})</option>`).join("");
    }
  }

  async function load() {
    if (pending) pending.abort();
    const controller = new AbortController();
    pending = controller;
    document.body.classList.add("loading");
    byId("resultStatus").textContent = "Loading…";
    try {
      const query = params({ page: page });
      const [jobsRes, facetRes] = await Promise.all([
        fetch("/api/jobs?" + query.toString(), { signal: controller.signal }),
        fetch("/api/facets?" + params().toString(), { signal: controller.signal })
      ]);
      const data = await jobsRes.json();
      const facets = await facetRes.json();
      page = data.page;
      totalPages = data.total_pages;
      list.innerHTML = data.results.map(card).join("");
      byId("visibleCount").textContent = data.total.toLocaleString();
      byId("emptyState").hidden = data.total !== 0;
      byId("pageStatus").textContent = `Page ${data.page.toLocaleString()} of ${data.total_pages.toLocaleString()}`;
      const first = data.total ? (data.page - 1) * data.page_size + 1 : 0;
      const last = (data.page - 1) * data.page_size + data.returned;
      byId("resultStatus").textContent = data.total
        ? `Showing ${first.toLocaleString()}–${last.toLocaleString()} of ${data.total.toLocaleString()}`
        : "No matching opportunities";
      byId("firstPage").disabled = data.page <= 1;
      byId("prevPage").disabled = data.page <= 1;
      byId("nextPage").disabled = data.page >= data.total_pages;
      byId("lastPage").disabled = data.page >= data.total_pages;
      byId("pager").hidden = data.total_pages <= 1;
      renderFacets(facets);
    } catch (error) {
      if (error.name !== "AbortError") {
        byId("resultStatus").textContent = "Could not load results: " + error.message;
      }
    } finally {
      if (pending === controller) { pending = null; document.body.classList.remove("loading"); }
    }
  }

  let timer = null;
  function reload(resetPage) {
    if (resetPage !== false) page = 1;
    clearTimeout(timer);
    timer = setTimeout(load, 90);
  }

  Object.entries(controls).forEach(([key, el]) => {
    const event = el.matches('input[type="search"], input[type="range"]') ? "input" : "change";
    el.addEventListener(event, () => {
      if (key === "min_score") byId("scoreOutput").value = el.value;
      reload();
    });
  });
  geoBoxes.concat(expBoxes).forEach((box) => box.addEventListener("change", () => reload()));

  byId("firstPage").addEventListener("click", () => { page = 1; load(); scrollTop(); });
  byId("prevPage").addEventListener("click", () => { page = Math.max(1, page - 1); load(); scrollTop(); });
  byId("nextPage").addEventListener("click", () => { page = Math.min(totalPages, page + 1); load(); scrollTop(); });
  byId("lastPage").addEventListener("click", () => { page = totalPages; load(); scrollTop(); });
  function scrollTop() { byId("results").scrollIntoView({ behavior: "smooth", block: "start" }); }

  function reset() {
    Object.entries(defaults).forEach(([key, value]) => {
      if (typeof value === "boolean") controls[key].checked = value;
      else controls[key].value = value;
    });
    geoBoxes.concat(expBoxes).forEach((b) => { b.checked = false; });
    byId("scoreOutput").value = controls.min_score.value;
    reload();
  }
  byId("resetFilters").addEventListener("click", reset);
  byId("emptyReset").addEventListener("click", reset);

  // Descriptions are fetched one card at a time, never bulk-loaded.
  list.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-detail]");
    if (!button) return;
    const article = button.closest(".job-card");
    const existing = article.querySelector(".detail-body");
    if (existing) { existing.remove(); button.textContent = "Show full description"; return; }
    button.textContent = "Loading…";
    try {
      const response = await fetch("/api/jobs/" + encodeURIComponent(button.dataset.detail));
      const detail = await response.json();
      const body = document.createElement("div");
      body.className = "detail-body";
      body.textContent = detail.description_text || "No description stored for this posting.";
      article.appendChild(body);
      button.textContent = "Hide full description";
    } catch (error) {
      button.textContent = "Could not load description";
    }
  });

  load();
})();
</script>
"""


__all__ = ["render_shell"]
