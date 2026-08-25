"""Minimal, cached ``robots.txt`` gate for discovery fetches.

Discovery reads other people's servers, so it asks permission first.  A host
that disallows the path is skipped and reported, never worked around: no
alternate user-agent, no proxy, no retry against a different entry point.

The matcher implements the parts of RFC 9309 that actually matter in practice,
including the ``*`` and ``$`` wildcards that Python's stdlib parser ignores.
Getting those right matters in *both* directions — Common Crawl publishes
``Disallow: /`` plus ``Allow: /collinfo.json$``, and reading that literally
(as the stdlib does) both forbids a file they explicitly permit and would let a
naive prefix match through paths they do not.

A host whose ``robots.txt`` cannot be reached is treated as *allowed*, which is
the conventional reading — an unreachable policy file is not a prohibition.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import requests


LOGGER = logging.getLogger(__name__)


def _to_regex(pattern: str) -> re.Pattern[str]:
    """Compile one robots path pattern, honoring ``*`` and a trailing ``$``."""

    anchored_end = pattern.endswith("$")
    body = pattern[:-1] if anchored_end else pattern
    parts = [re.escape(piece) for piece in body.split("*")]
    expression = "^" + ".*".join(parts) + ("$" if anchored_end else "")
    return re.compile(expression)


class _Group:
    """One ``User-agent`` group's rules, in longest-match-wins order."""

    def __init__(self) -> None:
        self.agents: List[str] = []
        self.rules: List[Tuple[int, bool, re.Pattern[str]]] = []

    def add(self, path: str, allow: bool) -> None:
        if not path:
            return
        self.rules.append((len(path.rstrip("$")), allow, _to_regex(path)))

    def allows(self, path: str) -> bool:
        best_length = -1
        verdict = True
        for length, allow, pattern in self.rules:
            if not pattern.match(path):
                continue
            # Longer (more specific) rule wins; Allow wins ties, per the spec.
            if length > best_length or (length == best_length and allow):
                best_length = length
                verdict = allow
        return verdict


class _Policy:
    def __init__(self, text: str) -> None:
        self.groups: List[_Group] = []
        current: Optional[_Group] = None
        expecting_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, _, value = line.partition(":")
            field = field.strip().lower()
            value = value.strip()
            if field == "user-agent":
                if current is None or not expecting_agent:
                    current = _Group()
                    self.groups.append(current)
                    expecting_agent = True
                current.agents.append(value.lower())
            elif field in ("allow", "disallow") and current is not None:
                expecting_agent = False
                if field == "disallow" and not value:
                    # "Disallow:" with no path means "nothing is disallowed".
                    continue
                current.add(value, field == "allow")

    def _group_for(self, user_agent: str) -> Optional[_Group]:
        token = user_agent.split("/", 1)[0].strip().lower()
        best: Optional[_Group] = None
        best_length = -1
        for group in self.groups:
            for agent in group.agents:
                if agent == "*":
                    if best_length < 0:
                        best = group
                elif agent and agent in token and len(agent) > best_length:
                    best, best_length = group, len(agent)
        return best

    def allows(self, user_agent: str, path: str) -> bool:
        group = self._group_for(user_agent)
        return True if group is None else group.allows(path or "/")


class RobotsPolicy:
    """Fetch and cache ``robots.txt`` per host for the life of one run."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
        enabled: bool = True,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.session = session
        self.enabled = enabled
        self._cache: Dict[str, Optional[_Policy]] = {}
        self._lock = threading.Lock()

    def _policy(self, url: str) -> Optional[_Policy]:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            if host in self._cache:
                return self._cache[host]
        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        policy: Optional[_Policy] = None
        try:
            getter = self.session.get if self.session is not None else requests.get
            response = getter(
                robots_url,
                headers={"User-Agent": self.user_agent, "Accept": "text/plain"},
                timeout=self.timeout,
            )
            if 200 <= response.status_code < 300:
                policy = _Policy(response.text)
            elif response.status_code in (401, 403):
                # An access-controlled policy file means "stay out".
                policy = _Policy("User-agent: *\nDisallow: /\n")
        except requests.RequestException as exc:
            LOGGER.debug("Could not read %s: %s", robots_url, exc)
        with self._lock:
            self._cache[host] = policy
        return policy

    def allows(self, url: str) -> bool:
        """True when this URL may be fetched under the host's stated policy."""

        if not self.enabled:
            return True
        try:
            policy = self._policy(url)
        except Exception:  # a policy lookup must never abort discovery
            LOGGER.debug("robots.txt lookup failed for %s", url, exc_info=True)
            return True
        if policy is None:
            return True
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return policy.allows(self.user_agent, path)


__all__ = ["RobotsPolicy"]
