"""GitHub App authentication + REST tool.

Flow:
  1. _build_app_jwt():        sign a short JWT (10min) with the App's
                              private key (RS256)
  2. gh_get_installation_token(): trade the JWT at
                              /app/installations/<id>/access_tokens
                              for an installation access token. Cached
                              in-memory for ~50min (GH default token TTL
                              is 1h; refresh at 50min for safety).
  3. Subsequent REST calls (gh_list_prs, gh_get_pr, etc.) include the
     installation token as a Bearer credential.

Per spec § 6.1: the App `cluster-agent` has per-repo fine-grained perms
(Issues RW everywhere, PRs RW on writable repos, Contents R or RW per
the table). It NEVER has admin/settings/org/secrets/packages/workflows.

Read tools used in P0 smoke; write tools (issue_create, issue_comment,
pr_comment) are functional but unexercised until P1+. P0 just needs the
auth path proven.
"""
from __future__ import annotations
import base64
import datetime as dt
import os
import time
from typing import Any

import httpx
import jwt   # PyJWT

from .audit import audit


_GH_API = "https://api.github.com"

# Single-process token cache. Mode runner calls run serially per spec
# § 4.1; no concurrent access expected. Refresh at 50min into the 1h
# TTL for safety margin.
_token_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}


def _build_app_jwt() -> str:
    """Sign a 10-minute JWT with the App's private key (RS256).

    iat slightly in the past (60s) to absorb clock-skew between this
    NAS and GitHub's auth servers.
    """
    app_id = os.environ["CLUSTER_AGENT_GH_APP_ID"]
    pem_b64 = os.environ["CLUSTER_AGENT_GH_APP_PRIVATE_KEY"]
    pem = base64.b64decode(pem_b64)
    now = int(time.time())
    return jwt.encode(
        {"iss": app_id, "iat": now - 60, "exp": now + 540},
        pem,
        algorithm="RS256",
    )


@audit(tool="gh_get_installation_token", redact_result=True)
def gh_get_installation_token() -> str:
    """Trade the App JWT for a short-lived installation token (cached).

    The token authenticates subsequent REST calls as the App-installation
    pair (not as a user). Per-repo perms come from the App's installed
    permissions, not from token scopes.
    """
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] - now > 60:
        return _token_cache["token"]
    installation_id = os.environ["CLUSTER_AGENT_GH_APP_INSTALLATION_ID"]
    r = httpx.post(
        f"{_GH_API}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {_build_app_jwt()}",
            "Accept": "application/vnd.github+json",
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    _token_cache["token"] = data["token"]
    _token_cache["expires_at"] = dt.datetime.fromisoformat(
        data["expires_at"].replace("Z", "+00:00")
    ).timestamp()
    return data["token"]


def _gh_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {gh_get_installation_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# ── Read tools (exercised in P0 smoke + Mode I) ──────────────────────────

@audit(tool="gh_list_prs")
def gh_list_prs(repo: str, *, state: str = "open") -> list[dict[str, Any]]:
    r = httpx.get(
        f"{_GH_API}/repos/{repo}/pulls",
        params={"state": state, "per_page": 50},
        headers=_gh_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


@audit(tool="gh_get_pr")
def gh_get_pr(repo: str, number: int) -> dict[str, Any]:
    r = httpx.get(
        f"{_GH_API}/repos/{repo}/pulls/{number}",
        headers=_gh_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


@audit(tool="gh_get_commit")
def gh_get_commit(repo: str, sha: str) -> dict[str, Any]:
    r = httpx.get(
        f"{_GH_API}/repos/{repo}/commits/{sha}",
        headers=_gh_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


# ── Write tools (P0 lands them, P1+ exercises them) ──────────────────────

# Hard-coded allowlist of repos this module may WRITE to. The GH App
# `cluster-agent[bot]` carries Issues RW + PRs RW on every repo it's
# installed in; this in-code gate is belt-and-suspenders against:
#   - Doppler typo on `SANDBOX_REPO` redirecting writes to a real repo
#   - Future code path taking `repo` from an LLM-controlled string
#   - Container env override via compromise
# Read tools (gh_issue_list, gh_get_pr) are NOT scoped — read-only is
# fine even cross-repo and we need it for cross-cluster awareness in
# future modes.
# To add a new write target, add it here AND ensure the GH App is
# installed there with Issues R/W only (no contents/PRs/workflows).
_WRITE_ALLOWED_REPOS: frozenset[str] = frozenset({
    # Daily digest-summary destination. Renamed from `-sandbox` when
    # Mode A graduated out of P1 soak (2026-07-06 graduation spec:
    # truenas-infra/docs/superpowers/specs/2026-07-06-cluster-agent-digest-graduation.md).
    "guntars-rakitko/cluster-agent-digest",
    # Transitional: kept so in-flight state.db findings whose stored
    # gh_issue_ref still points at the old repo can be commented on
    # through the rename cutover. Drop once no open finding references it.
    "guntars-rakitko/cluster-agent-sandbox",
    # Individual findings (all severities) graduated to the ops repo
    # (2026-07-06). The App is scoped to Issues R/W ONLY on kube-infra;
    # this in-code allowlist is the second lock.
    "guntars-rakitko/kube-infra",
})


def _assert_write_allowed(repo: str) -> None:
    """Raise if the target repo isn't in the write allowlist.

    Called by every write tool below. Raises a clear PermissionError
    that the dispatch layer's try/except can log + skip without
    pretending the issue was filed (vs e.g. a 403 from GitHub which
    looks more like a transient auth issue)."""
    if repo not in _WRITE_ALLOWED_REPOS:
        raise PermissionError(
            f"cluster-agent attempted GH write to {repo!r} which is "
            f"NOT in _WRITE_ALLOWED_REPOS={sorted(_WRITE_ALLOWED_REPOS)}. "
            f"This is an in-code safety gate against env-typo + LLM-"
            f"controlled-repo escape. Edit tools/github.py to add a "
            f"new allowed repo."
        )


@audit(
    tool="gh_issue_create",
    # `body` can contain raw log excerpts + kubectl describe output
    # (un-redacted per current digest pipeline). The audit log is
    # written to stdout → captured by Loki → indexed. Redact body to
    # keep secrets out of the log pipeline; title stays so operator
    # can still see WHICH issue was created from audit alone.
    redact=["body"],
)
def gh_issue_create(
    repo: str,
    title: str,
    body: str,
    *,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    _assert_write_allowed(repo)
    payload: dict[str, Any] = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    r = httpx.post(
        f"{_GH_API}/repos/{repo}/issues",
        json=payload,
        headers=_gh_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


@audit(tool="gh_issue_comment", redact=["body"])
def gh_issue_comment(repo: str, number: int, body: str) -> dict[str, Any]:
    _assert_write_allowed(repo)
    r = httpx.post(
        f"{_GH_API}/repos/{repo}/issues/{number}/comments",
        json={"body": body},
        headers=_gh_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


@audit(tool="gh_pr_comment")
def gh_pr_comment(repo: str, number: int, body: str) -> dict[str, Any]:
    # PR comments and issue comments share the same GH API endpoint —
    # the URL doesn't care whether the number is a PR or an issue.
    return gh_issue_comment(repo, number, body)


@audit(tool="gh_issue_list")
def gh_issue_list(
    repo: str,
    *,
    labels: list[str] | None = None,
    state: str = "open",
    per_page: int = 30,
) -> list[dict[str, Any]]:
    """List issues in a repo, optionally filtered by labels.

    Used by the daily-digest summary-issue path to find yesterday's
    summary so it can be auto-closed when today's is filed. Labels
    are comma-joined as the API expects (AND-match across all given
    labels). PRs are excluded — GH's /issues endpoint returns both
    but each item carries a `pull_request` key only for PRs.

    Returns the JSON list; caller filters further as needed.
    """
    params: dict[str, Any] = {"state": state, "per_page": per_page}
    if labels:
        params["labels"] = ",".join(labels)
    r = httpx.get(
        f"{_GH_API}/repos/{repo}/issues",
        params=params,
        headers=_gh_headers(),
        timeout=15,
    )
    r.raise_for_status()
    # Filter out PRs — the /issues endpoint mixes them in.
    return [item for item in r.json() if "pull_request" not in item]


@audit(tool="gh_issue_close")
def gh_issue_close(
    repo: str,
    number: int,
    *,
    comment: str | None = None,
) -> dict[str, Any]:
    """Close an issue. If `comment` is given, post it first (so the
    close action shows up below the comment in the issue timeline)."""
    _assert_write_allowed(repo)
    if comment:
        gh_issue_comment(repo, number, comment)
    r = httpx.patch(
        f"{_GH_API}/repos/{repo}/issues/{number}",
        json={"state": "closed"},
        headers=_gh_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()
