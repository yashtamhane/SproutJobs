"""The project must stay free to run: no paid APIs, no keys, no model calls.

These are guard tests.  They are cheap and they fail loudly the moment someone
adds a dependency that would put this project behind a bill or an account.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "jobfinder"

#: Clients that would require a paid account or an API key to function.
FORBIDDEN_IMPORTS = (
    "openai",
    "anthropic",
    "google.generativeai",
    "cohere",
    "replicate",
    "huggingface_hub",
    "transformers",
    "torch",
    "langchain",
    "llama_index",
    "boto3",
    "serpapi",
    "algoliasearch",
)

#: Environment variables that would mean a key is being read from somewhere.
KEY_PATTERN = re.compile(
    r"""os\.(?:environ(?:\.get)?|getenv)\s*[\(\[]\s*["'][^"']*"""
    r"""(?:API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)""",
    re.IGNORECASE,
)


def source_files() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


class TestNoPaidDependencies:
    def test_requirements_stay_minimal(self) -> None:
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        packages = {
            re.split(r"[<>=!\[]", line.strip(), maxsplit=1)[0].lower()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        assert packages <= {"requests", "pytest"}, f"unexpected dependency: {packages}"

    @pytest.mark.parametrize("module", FORBIDDEN_IMPORTS)
    def test_no_paid_or_model_client_is_imported(self, module: str) -> None:
        pattern = re.compile(rf"^\s*(?:import|from)\s+{re.escape(module)}\b", re.MULTILINE)
        offenders = [
            path.relative_to(ROOT)
            for path in source_files()
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        assert not offenders, f"{module} imported in {offenders}"

    def test_no_api_key_is_read_from_the_environment(self) -> None:
        offenders = []
        for path in source_files():
            for match in KEY_PATTERN.finditer(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
        assert not offenders, f"credentials read at {offenders}"

    def test_the_only_network_hosts_are_free_and_public(self) -> None:
        """Every outbound host must be a keyless public endpoint."""

        allowed = {
            "api.ashbyhq.com",  # Ashby's own unauthenticated posting API
            "jobs.ashbyhq.com",  # public board URLs
            "index.commoncrawl.org",  # free public URL index
            "web.archive.org",  # free public URL index
            "raw.githubusercontent.com",  # public repository documents
            "127.0.0.1",  # the local dashboard, which never leaves the machine
        }
        host_pattern = re.compile(r"https?://([A-Za-z0-9.-]+)")
        found = set()
        for path in source_files():
            found.update(host_pattern.findall(path.read_text(encoding="utf-8")))
        # Example/documentation hosts in docstrings are not real endpoints.
        found -= {"example.com", "search.example", "www.example.com", "localhost"}
        unexpected = {host for host in found if host not in allowed}
        assert not unexpected, f"unexpected network host(s): {sorted(unexpected)}"

    def test_scoring_is_deterministic(self) -> None:
        """Same input, same verdict — no sampling, no model, no clock."""

        from jobfinder.scoring import score_job

        results = [
            score_job(
                "Software Engineer, New Grad",
                "0-2 years of experience. BS in Computer Science.",
                "New York, NY",
            ).as_dict()
            for _ in range(5)
        ]
        assert all(item == results[0] for item in results)


class TestOfflineOperation:
    def test_reports_rebuild_with_no_network_at_all(self, tmp_path: Path, monkeypatch) -> None:
        """`report` must work on a plane."""

        import requests

        from jobfinder.config import AppConfig
        from jobfinder.db import Database
        from jobfinder.models import NormalizedJob
        from jobfinder.pipeline import write_reports
        from jobfinder.scoring import analyze_normalized_job

        def forbidden(*args, **kwargs):
            raise AssertionError("report must not touch the network")

        monkeypatch.setattr(requests.Session, "request", forbidden)
        monkeypatch.setattr(requests, "get", forbidden)

        config = AppConfig()
        config.project_root = tmp_path
        config.database.path = "jobs.sqlite3"
        config.output.directory = "output"
        database = Database(config.database_path)
        database.initialize()
        try:
            job = NormalizedJob(
                source="ashby",
                source_company_slug="acme",
                source_job_id="1",
                company_name="Acme",
                title="Software Engineer, New Grad",
                location="New York, NY",
                description_text="0-2 years of experience required.",
                content_hash="hash-1",
            )
            analyze_normalized_job(job)
            database.ingest_company_jobs("acme", [job], database.start_run("test"))
            report = write_reports(config, database)
        finally:
            database.close()
        assert report.dashboard_path.exists()
        assert report.csv_path.exists()


class TestNoLiveJobDataIsShipped:
    """The repository holds code, rules, docs and tests — never a run's output."""

    #: Directories that are runtime output, not sources.
    IGNORED_ROOTS = {".venv", ".git", "data", "output", "__pycache__", ".pytest_cache"}

    def source_tree(self):
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            parts = set(path.relative_to(ROOT).parts)
            if parts & self.IGNORED_ROOTS:
                continue
            yield path

    def test_no_database_or_export_sits_in_the_source_tree(self) -> None:
        offenders = [
            str(path.relative_to(ROOT))
            for path in self.source_tree()
            if path.suffix in {".sqlite3", ".sqlite", ".db", ".csv"}
        ]
        assert not offenders, f"generated job data in the source tree: {offenders}"

    def test_no_populated_html_export_is_present(self) -> None:
        offenders = [
            str(path.relative_to(ROOT))
            for path in self.source_tree()
            if path.suffix in {".html", ".htm"}
        ]
        assert not offenders, f"HTML export in the source tree: {offenders}"

    def test_the_seed_file_ships_without_any_company(self) -> None:
        """No third-party company list is redistributed; see SOURCES.md."""

        text = (ROOT / "seeds.txt").read_text(encoding="utf-8")
        slugs = [
            line.split("#", 1)[0].strip()
            for line in text.splitlines()
            if line.split("#", 1)[0].strip()
        ]
        assert slugs == [], f"seeds.txt ships company slugs: {slugs}"

    def test_gitignore_covers_the_runtime_artifacts(self) -> None:
        rules = {
            line.strip()
            for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        for required in ("data/", "output/", "*.sqlite3", ".venv/", "__pycache__/"):
            assert required in rules, f".gitignore is missing {required!r}"

    def test_the_public_docs_make_no_approval_claim(self) -> None:
        """These phrases may appear only as denials, never as assertions.

        The docs deliberately say "not approved by Ashby", so a bare substring
        search would flag exactly the sentence that makes the disclaimer.
        """

        forbidden = ("100% legal", "fully compliant", "approved by ashby")
        negations = ("not", "never", "no ", "cannot", "does not", "is not", "nor")
        offenders = []
        for name in ("README.md", "COMPLIANCE.md", "SOURCES.md", "SECURITY.md"):
            text = (ROOT / name).read_text(encoding="utf-8").lower()
            for phrase in forbidden:
                start = text.find(phrase)
                while start != -1:
                    context = text[max(0, start - 80) : start]
                    if not any(word in context for word in negations):
                        offenders.append(f"{name}: ...{context[-60:]}{phrase}")
                    start = text.find(phrase, start + 1)
        assert not offenders, f"unqualified claim(s): {offenders}"

    def test_no_absolute_personal_path_is_committed(self) -> None:
        """A home-directory path with a real user name in it, anywhere."""

        home_path = re.compile(
            r"(?:/Users/|/home/|[A-Z]:\\\\Users\\\\)[A-Za-z0-9._-]+[/\\\\]"
        )
        offenders = []
        for path in self.source_tree():
            if path.suffix not in {".py", ".toml", ".md", ".sh", ".txt", ".json"}:
                continue
            match = home_path.search(path.read_text(encoding="utf-8", errors="ignore"))
            if match:
                offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
        assert not offenders, f"absolute local paths in {offenders}"
