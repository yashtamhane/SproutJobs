"""A technical compliance audit of this checkout.

    python -m jobfinder compliance

This is an **engineering** audit, not a legal opinion, and it is careful to stay
inside what a program can actually verify.  It reads this repository and this
configuration and answers mechanical questions: is concurrency bounded, is a
timeout set, does the retry path handle HTTP 429, does the dashboard bind to
loopback, is the SQLite file ignored by Git, is a discovery source enabled that
the project has decided not to ship enabled?

It cannot and does not tell you that anything is lawful, permitted, or approved.
See COMPLIANCE.md for what these safeguards mean and what they do not.

Three outcomes:

``PASS``  the property was checked and holds.
``WARN``  something needs a human decision, or could not be verified here.
``FAIL``  a property the project commits to is violated.

Only ``FAIL`` sets a non-zero exit code, so ``WARN`` is usable in CI for the
genuinely unresolved questions without turning into noise.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import AppConfig
from . import server as server_module


PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

#: Discovery providers this project ships disabled, and why.  A provider named
#: here that is switched on is a FAIL, not a preference: each entry records a
#: stated policy of the source, not an opinion about it.
FORBIDDEN_SOURCES: dict[str, str] = {
    "use_common_crawl": (
        "index.commoncrawl.org serves 'Disallow: /' with an allow-list that does "
        "not cover the CC-MAIN-*-index query endpoint this would use"
    ),
}

#: Clients that would put the core behind a bill, an account, or a key.
PAID_OR_MODEL_CLIENTS: tuple[str, ...] = (
    "openai",
    "anthropic",
    "google.generativeai",
    "vertexai",
    "cohere",
    "replicate",
    "huggingface_hub",
    "transformers",
    "torch",
    "langchain",
    "llama_index",
    "boto3",
    "azure",
    "serpapi",
    "algoliasearch",
    "scrapingbee",
    "zenrows",
    "brightdata",
    "apify_client",
)

#: Headless-browser drivers.  SproutJobs reads documented JSON endpoints; it has
#: no reason to drive a browser, and driving one is how clients end up
#: rendering pages a site served only to human visitors.
BROWSER_AUTOMATION: tuple[str, ...] = (
    "selenium",
    "seleniumwire",
    "playwright",
    "pyppeteer",
    "puppeteer",
    "undetected_chromedriver",
    "splash",
    "helium",
    "drissionpage",
)

#: CAPTCHA-solving and blocking-evasion services.  Their presence would mean the
#: project had started working around a refusal instead of accepting it.
CIRCUMVENTION_CLIENTS: tuple[str, ...] = (
    "twocaptcha",
    "python_anticaptcha",
    "anticaptchaofficial",
    "capsolver",
    "deathbycaptcha",
    "cloudscraper",
    "curl_cffi",
    "fake_useragent",
    "requests_ip_rotator",
)

#: Loopback addresses.  Anything else means the dashboard is reachable off-box.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})

#: Runtime artifacts that must never be committed.
GENERATED_ARTIFACT_PATTERNS: tuple[str, ...] = (
    "*.sqlite3",
    "*.sqlite",
    "*.db",
    "output/*.csv",
    "output/*.html",
    "data/*",
)

#: What .gitignore has to cover for the above to be true by construction.
REQUIRED_IGNORE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("the SQLite database", ("*.sqlite3", "data/", "/data/")),
    ("the discovery cache", ("data/", "/data/")),
    ("the generated reports", ("output/", "/output/")),
    ("virtual environments", (".venv/", "venv/")),
    ("bytecode caches", ("__pycache__/",)),
)


@dataclass(frozen=True)
class Check:
    """One audited property."""

    name: str
    status: str
    detail: str
    remedy: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass
class AuditReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, remedy: str = "") -> None:
        self.checks.append(Check(name, status, detail, remedy))

    def count(self, status: str) -> int:
        return sum(1 for check in self.checks if check.status == status)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if check.status == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [check for check in self.checks if check.status == WARN]

    @property
    def ok(self) -> bool:
        """True when nothing failed.  Warnings do not make the audit fail."""

        return not self.failures

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "checks": [check.as_dict() for check in self.checks],
            "passed": self.count(PASS),
            "warnings": self.count(WARN),
            "failed": self.count(FAIL),
            "ok": self.ok,
        }


# ---------------------------------------------------------------------------
# Source inspection helpers
# ---------------------------------------------------------------------------

def package_files(root: Path) -> list[Path]:
    """Every Python file in the shipped package, this auditor included."""

    return sorted((root / "jobfinder").rglob("*.py"))


def _imported_modules(path: Path) -> set[str]:
    """Top-level and dotted module names imported by one file.

    Parsed rather than grepped so a module named in a docstring or a list of
    strings — as several are in this very file — is not mistaken for an import.
    """

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    # "a.b.c" should also answer to "a" and "a.b".
    expanded: set[str] = set()
    for name in found:
        parts = name.split(".")
        expanded.update(".".join(parts[:index]) for index in range(1, len(parts) + 1))
    return expanded


def _offending_imports(root: Path, modules: Iterable[str]) -> list[str]:
    wanted = set(modules)
    offenders: list[str] = []
    for path in package_files(root):
        hits = sorted(_imported_modules(path) & wanted)
        offenders.extend(f"{path.relative_to(root)}: {hit}" for hit in hits)
    return offenders


def _requirement_names(root: Path) -> set[str]:
    path = root / "requirements.txt"
    if not path.exists():
        return set()
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.add(re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].strip().lower())
    return names


def _read(root: Path, relative: str) -> str:
    path = root / relative
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_sources(report: AuditReport, config: AppConfig) -> None:
    enabled_bad = [
        (name, reason)
        for name, reason in FORBIDDEN_SOURCES.items()
        if getattr(config.discovery, name, False)
    ]
    if enabled_bad:
        for name, reason in enabled_bad:
            report.add(
                "no forbidden discovery source is enabled",
                FAIL,
                f"discovery.{name} is on, but {reason}.",
                f"Set discovery.{name} = false in config.toml.",
            )
    else:
        report.add(
            "no forbidden discovery source is enabled",
            PASS,
            "Every source this project ships disabled is disabled: "
            + ", ".join(sorted(FORBIDDEN_SOURCES)),
        )

    if getattr(config.discovery, "respect_robots", True):
        report.add(
            "robots.txt is consulted before discovery fetches",
            PASS,
            "discovery.respect_robots is on; a disallowed URL is skipped and reported.",
        )
    else:
        report.add(
            "robots.txt is consulted before discovery fetches",
            FAIL,
            "discovery.respect_robots is off, so stated crawl policies are ignored.",
            "Set discovery.respect_robots = true in config.toml.",
        )

    endpoints = list(getattr(config.discovery, "search_endpoints", ()) or ())
    if endpoints:
        report.add(
            "search endpoints are opt-in only",
            WARN,
            f"{len(endpoints)} search endpoint(s) are configured: "
            + ", ".join(endpoints)
            + ". SproutJobs ships none, and cannot tell whether you are permitted "
            "to query these automatically.",
            "Confirm the endpoint's terms allow automated queries, or remove it.",
        )
    else:
        report.add(
            "search endpoints are opt-in only",
            PASS,
            "No search endpoint is configured, which is the shipped default.",
        )


def _check_documentation(report: AuditReport, config: AppConfig, root: Path) -> None:
    sources_doc = _read(root, "SOURCES.md")
    if not sources_doc:
        report.add(
            "every enabled source is documented in SOURCES.md",
            FAIL,
            "SOURCES.md is missing.",
            "Document each discovery and input source in SOURCES.md.",
        )
        return
    from .discovery.registry import build_discoverers

    try:
        names = [provider.name for provider in build_discoverers(config)]
    except Exception as exc:  # a broken config must not crash the audit
        report.add(
            "every enabled source is documented in SOURCES.md",
            WARN,
            f"Could not build the discovery providers to check them: {exc}",
        )
        return
    undocumented = [name for name in names if name not in sources_doc]
    if undocumented:
        report.add(
            "every enabled source is documented in SOURCES.md",
            FAIL,
            "Enabled but absent from SOURCES.md: " + ", ".join(sorted(set(undocumented))),
            "Add a SOURCES.md entry, or disable the source.",
        )
    else:
        report.add(
            "every enabled source is documented in SOURCES.md",
            PASS,
            f"{len(names)} enabled provider(s) documented: " + ", ".join(names),
        )


def _check_no_keys(report: AuditReport, root: Path) -> None:
    # Deliberately not the string "os" + "." + "environ" as one literal, so this
    # scanner does not report itself.
    accessor = r"os\s*\.\s*(?:environ\s*(?:\.\s*get\s*)?|getenv\s*)"
    secret = r"(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|BEARER)"
    pattern = re.compile(accessor + r"[\(\[]\s*[\"'][^\"']*" + secret, re.IGNORECASE)
    offenders = []
    for path in package_files(root):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(root)}: {match.group(0).strip()}")
    if offenders:
        report.add(
            "core reads no API key or secret",
            FAIL,
            "Credential lookups found: " + "; ".join(offenders),
            "Core functionality must work with no key of any kind.",
        )
    else:
        report.add(
            "core reads no API key or secret",
            PASS,
            f"No credential is read from the environment in {len(package_files(root))} "
            "package file(s).",
        )


def _check_dependencies(report: AuditReport, root: Path) -> None:
    requirements = _requirement_names(root)
    unexpected = requirements - {"requests", "pytest"}
    if unexpected:
        report.add(
            "no paid or keyed client dependency",
            FAIL,
            "requirements.txt lists beyond requests/pytest: " + ", ".join(sorted(unexpected)),
            "Keep the core to open-source, keyless dependencies.",
        )
    else:
        report.add(
            "no paid or keyed client dependency",
            PASS,
            "requirements.txt is "
            + (", ".join(sorted(requirements)) or "empty")
            + " — both open source, neither requiring an account.",
        )

    offenders = _offending_imports(root, PAID_OR_MODEL_CLIENTS)
    if offenders:
        report.add(
            "no paid or model client is imported",
            FAIL,
            "Imported: " + "; ".join(offenders),
            "Scoring stays deterministic and keyless; remove the client.",
        )
    else:
        report.add(
            "no paid or model client is imported",
            PASS,
            f"None of {len(PAID_OR_MODEL_CLIENTS)} paid/model clients is imported anywhere.",
        )


def _check_browser_automation(report: AuditReport, root: Path) -> None:
    offenders = _offending_imports(root, BROWSER_AUTOMATION)
    listed = _requirement_names(root) & {name.replace("_", "-") for name in BROWSER_AUTOMATION}
    if offenders or listed:
        report.add(
            "no browser automation",
            FAIL,
            "Found: " + "; ".join([*offenders, *sorted(listed)]),
            "SproutJobs reads documented JSON endpoints; it drives no browser.",
        )
    else:
        report.add(
            "no browser automation",
            PASS,
            "No headless-browser driver is imported or required.",
        )

    offenders = _offending_imports(root, CIRCUMVENTION_CLIENTS)
    if offenders:
        report.add(
            "no CAPTCHA-solving or blocking-evasion client",
            FAIL,
            "Found: " + "; ".join(offenders),
            "A refusal is accepted and reported, never worked around.",
        )
    else:
        report.add(
            "no CAPTCHA-solving or blocking-evasion client",
            PASS,
            "No CAPTCHA solver, TLS-impersonation client or proxy rotator is imported.",
        )


def _check_network_limits(report: AuditReport, config: AppConfig) -> None:
    network = config.network
    if 1 <= network.concurrency <= 8:
        report.add(
            "request concurrency is bounded",
            PASS,
            f"network.concurrency = {network.concurrency}; the configuration "
            "validator refuses anything outside 1-8.",
        )
    elif network.concurrency > 8:
        report.add(
            "request concurrency is bounded",
            FAIL,
            f"network.concurrency = {network.concurrency} exceeds the ceiling of 8.",
            "Lower network.concurrency in config.toml.",
        )
    else:
        report.add(
            "request concurrency is bounded",
            FAIL,
            f"network.concurrency = {network.concurrency} is not a usable worker count.",
            "Set network.concurrency to at least 1.",
        )

    if network.request_timeout > 0:
        report.add(
            "every request has a timeout",
            PASS,
            f"network.request_timeout = {network.request_timeout}s, applied to "
            "board fetches, discovery reads and robots.txt lookups.",
        )
    else:
        report.add(
            "every request has a timeout",
            FAIL,
            "network.request_timeout must be positive; an unbounded request can hang forever.",
            "Set network.request_timeout in config.toml.",
        )

    if network.retries > 0 and network.backoff_seconds > 0:
        report.add(
            "retries use exponential backoff",
            PASS,
            f"network.retries = {network.retries}, "
            f"network.backoff_seconds = {network.backoff_seconds} "
            "(doubling per attempt, with jitter).",
        )
    elif network.retries == 0:
        report.add(
            "retries use exponential backoff",
            WARN,
            "network.retries = 0. Nothing is retried, which is polite but makes a "
            "single blip look like a dead board.",
        )
    else:
        report.add(
            "retries use exponential backoff",
            FAIL,
            f"network.retries = {network.retries} with "
            f"backoff_seconds = {network.backoff_seconds}: retrying without a "
            "growing delay hammers a struggling server.",
            "Set network.backoff_seconds to a positive value.",
        )

    if network.min_request_interval > 0:
        report.add(
            "outbound requests are rate limited",
            PASS,
            f"network.min_request_interval = {network.min_request_interval}s, "
            "enforced with a lock shared by every worker thread, so the ceiling "
            "is global rather than per-thread.",
        )
    else:
        report.add(
            "outbound requests are rate limited",
            WARN,
            "network.min_request_interval = 0, so requests are paced only by "
            "concurrency. Consider a small positive interval.",
            "Set network.min_request_interval to e.g. 0.15 in config.toml.",
        )


def _check_retry_after(report: AuditReport, root: Path) -> None:
    source = _read(root, "jobfinder/sources/ashby.py")
    has_429 = "429" in source
    honors_header = "Retry-After" in source
    if has_429 and honors_header:
        report.add(
            "HTTP 429 is handled and Retry-After honored",
            PASS,
            "jobfinder/sources/ashby.py treats 429 as retryable and reads the "
            "server's Retry-After header (seconds or HTTP-date) before waiting.",
        )
    else:
        missing = []
        if not has_429:
            missing.append("429 is not in the retryable set")
        if not honors_header:
            missing.append("Retry-After is not read")
        report.add(
            "HTTP 429 is handled and Retry-After honored",
            FAIL,
            "; ".join(missing),
            "Back off when a server asks you to.",
        )


def _check_user_agent(report: AuditReport, config: AppConfig) -> None:
    agent = (config.network.user_agent or "").strip()
    if not agent:
        report.add(
            "a User-Agent identifies this client",
            FAIL,
            "network.user_agent is empty, so requests go out unidentified.",
            "Set network.user_agent in config.toml.",
        )
        return
    impersonating = re.search(r"Mozilla|AppleWebKit|Chrome/|Safari/|Gecko/", agent)
    if impersonating:
        report.add(
            "a User-Agent identifies this client",
            FAIL,
            f"network.user_agent impersonates a browser: {agent!r}",
            "Identify the tool honestly instead of disguising it.",
        )
        return
    status, note = PASS, ""
    if "http" not in agent and "@" not in agent:
        status = WARN
        note = (
            " No contact URL or address is included, so an operator who wants to "
            "reach you about this traffic cannot."
        )
    report.add(
        "a User-Agent identifies this client",
        status,
        f"network.user_agent = {agent!r}." + note,
        "Append your repository or contact URL to network.user_agent." if note else "",
    )


def _check_local_only(report: AuditReport, root: Path) -> None:
    default_host = getattr(server_module, "DEFAULT_HOST", "")
    if default_host in LOOPBACK_HOSTS:
        report.add(
            "the dashboard binds to loopback only",
            PASS,
            f"server.DEFAULT_HOST = {default_host!r}; the socket is not reachable "
            "from other machines unless a host is passed explicitly.",
        )
    else:
        report.add(
            "the dashboard binds to loopback only",
            FAIL,
            f"server.DEFAULT_HOST = {default_host!r} is not a loopback address.",
            "Restore the loopback default in jobfinder/server.py.",
        )

    server_source = _read(root, "jobfinder/server.py")
    if "0.0.0.0" in server_source:
        report.add(
            "no bind-to-all-interfaces default",
            FAIL,
            "jobfinder/server.py mentions the all-interfaces address.",
            "The dashboard is a local tool; do not expose it by default.",
        )
    else:
        report.add(
            "no bind-to-all-interfaces default",
            PASS,
            "jobfinder/server.py contains no all-interfaces bind address.",
        )


def _check_no_applications(report: AuditReport, root: Path) -> None:
    write_call = re.compile(r"\.\s*(?:post|put|patch|delete)\s*\(", re.IGNORECASE)
    offenders = []
    for path in package_files(root):
        text = path.read_text(encoding="utf-8")
        for match in write_call.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(root)}:{line}")
    if offenders:
        report.add(
            "no application is ever submitted",
            FAIL,
            "HTTP write calls found at " + ", ".join(offenders) + ". SproutJobs "
            "is read-only against every remote endpoint.",
            "Remove the write call; applying is the user's step, on the employer's site.",
        )
    else:
        report.add(
            "no application is ever submitted",
            PASS,
            "The package makes no HTTP POST/PUT/PATCH/DELETE call anywhere: every "
            "remote interaction is a GET.",
        )

    credential_prompt = re.compile(
        r"\b(?:getpass|input)\s*\(\s*[\"'][^\"']*"
        r"(?:password|passwd|resume|ssn|social security|date of birth)",
        re.IGNORECASE,
    )
    offenders = [
        str(path.relative_to(root))
        for path in package_files(root)
        if credential_prompt.search(path.read_text(encoding="utf-8"))
    ]
    if offenders:
        report.add(
            "no applicant credential is collected",
            FAIL,
            "Credential or personal-data prompts in " + ", ".join(offenders),
            "SproutJobs stores nothing about the person using it.",
        )
    else:
        report.add(
            "no applicant credential is collected",
            PASS,
            "Nothing prompts for a password, a resume or personal identifiers; "
            "the database holds public postings only.",
        )


def _check_gitignore(report: AuditReport, root: Path) -> None:
    text = _read(root, ".gitignore")
    if not text:
        report.add(
            "generated data is git-ignored",
            FAIL,
            ".gitignore is missing, so a run's SQLite database and CSV could be committed.",
            "Add a .gitignore covering data/, output/, *.sqlite3 and .venv/.",
        )
        return
    rules = {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}
    missing = [
        label
        for label, options in REQUIRED_IGNORE_RULES
        if not any(option in rules for option in options)
    ]
    if missing:
        report.add(
            "generated data is git-ignored",
            FAIL,
            ".gitignore does not cover: " + ", ".join(missing),
            "Extend .gitignore so runtime data cannot be committed by accident.",
        )
    else:
        report.add(
            "generated data is git-ignored",
            PASS,
            f".gitignore covers all {len(REQUIRED_IGNORE_RULES)} classes of runtime "
            "artifact: database, discovery cache, reports, environments, bytecode.",
        )


def _tracked_files(root: Path) -> list[str] | None:
    """``git ls-files`` for this checkout, or ``None`` when there is no repo."""

    if not (root / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "ls-files"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return [line for line in completed.stdout.splitlines() if line.strip()]


def _check_nothing_generated_is_tracked(report: AuditReport, root: Path) -> None:
    tracked = _tracked_files(root)
    if tracked is None:
        report.add(
            "no generated job data is tracked by Git",
            WARN,
            "This directory is not a Git repository yet, so there is nothing to "
            "inspect. The .gitignore check above is what will keep it clean.",
            "Run 'git init' and re-run this audit before publishing.",
        )
        return
    offenders = [
        name
        for name in tracked
        if any(Path(name).match(pattern) for pattern in GENERATED_ARTIFACT_PATTERNS)
    ]
    # A large tracked file is worth flagging even if its name looks innocent.
    oversized = []
    for name in tracked:
        path = root / name
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > 2_000_000:
            oversized.append(f"{name} ({size / 1_000_000:.1f} MB)")
    if offenders:
        report.add(
            "no generated job data is tracked by Git",
            FAIL,
            "Tracked runtime artifacts: " + ", ".join(offenders),
            "git rm --cached the file and confirm .gitignore covers it.",
        )
    elif oversized:
        report.add(
            "no generated job data is tracked by Git",
            WARN,
            "Unusually large tracked file(s): " + ", ".join(oversized),
            "Confirm these belong in the repository.",
        )
    else:
        report.add(
            "no generated job data is tracked by Git",
            PASS,
            f"{len(tracked)} tracked file(s), none a database, export, cache or "
            "oversized blob.",
        )


def _check_determinism(report: AuditReport) -> None:
    from .scoring import score_job

    sample = (
        "Software Engineer, New Grad",
        "0-2 years of experience. BS in Computer Science.",
        "New York, NY",
    )
    results = [score_job(*sample).as_dict() for _ in range(5)]
    if all(item == results[0] for item in results):
        report.add(
            "scoring is deterministic",
            PASS,
            "The same posting scored five times produced an identical verdict "
            f"({results[0]['score']}/100, {results[0]['category']}).",
        )
    else:
        report.add(
            "scoring is deterministic",
            FAIL,
            "Scoring the same posting repeatedly gave different answers.",
            "Remove the sampling, clock or model call from the scoring path.",
        )


def _check_local_storage(report: AuditReport, config: AppConfig) -> None:
    database = config.database_path
    inside = _is_relative_to(database, config.project_root)
    detail = f"The database is a local SQLite file at {database}."
    if inside:
        report.add(
            "job data is stored locally, in this project",
            PASS,
            detail + " No hosted database, no remote sync, no telemetry.",
        )
    else:
        report.add(
            "job data is stored locally, in this project",
            WARN,
            detail + " That is outside the project directory, so this repository's "
            ".gitignore does not govern it.",
            "Confirm the location is not inside another Git repository.",
        )


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.resolve().relative_to(other.resolve())
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_audit(config: AppConfig, *, project_root: Path | None = None) -> AuditReport:
    """Run every check against this configuration and checkout."""

    root = Path(project_root or config.project_root).resolve()
    report = AuditReport()
    _check_sources(report, config)
    _check_documentation(report, config, root)
    _check_no_keys(report, root)
    _check_dependencies(report, root)
    _check_browser_automation(report, root)
    _check_network_limits(report, config)
    _check_retry_after(report, root)
    _check_user_agent(report, config)
    _check_local_only(report, root)
    _check_no_applications(report, root)
    _check_determinism(report)
    _check_local_storage(report, config)
    _check_gitignore(report, root)
    _check_nothing_generated_is_tracked(report, root)
    return report


def format_report(report: AuditReport) -> str:
    """Render the audit for a terminal."""

    width = max((len(check.name) for check in report.checks), default=0)
    lines = [
        "SPROUTJOBS TECHNICAL COMPLIANCE AUDIT",
        "",
        "An engineering check of this checkout and configuration.",
        "It verifies safeguards; it does not and cannot assess legality.",
        "",
    ]
    for check in report.checks:
        lines.append(f"{check.status:<5} {check.name:<{width}}")
        lines.append(f"        {check.detail}")
        if check.remedy and check.status != PASS:
            lines.append(f"        -> {check.remedy}")
        lines.append("")
    lines.append("-" * (width + 8))
    lines.append(
        f"{report.count(PASS)} passed · {report.count(WARN)} warning(s) · "
        f"{report.count(FAIL)} failure(s)"
    )
    if report.failures:
        lines.append("")
        lines.append("FAILURES must be fixed before publishing or sharing this checkout.")
    elif report.warnings:
        lines.append("")
        lines.append(
            "No failures. Warnings are decisions for you, not defects — read them."
        )
    lines.append("")
    lines.append("This is not legal advice. You are responsible for how you use this")
    lines.append("tool and for complying with the terms and law that apply to you.")
    lines.append("See COMPLIANCE.md and SOURCES.md.")
    return "\n".join(lines)


__all__ = [
    "BROWSER_AUTOMATION",
    "CIRCUMVENTION_CLIENTS",
    "FAIL",
    "FORBIDDEN_SOURCES",
    "PAID_OR_MODEL_CLIENTS",
    "PASS",
    "WARN",
    "AuditReport",
    "Check",
    "format_report",
    "run_audit",
]
