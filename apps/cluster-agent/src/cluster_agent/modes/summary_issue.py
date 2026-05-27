"""Daily-digest summary delivery (2026-05-27).

Operator-visible companion to the curated Finding-issues. The digest
emits 0-N Findings (LLM-judged actionable) but SEES every alert group
in the window. The remaining "skipped" groups (chronic-but-known,
self-healed transients, flapping background noise) vanish from the
operator's view — even though some of them are exactly the kind of
recurring-but-self-healing pattern that points at a real underlying
issue nobody has investigated.

This module produces ONE markdown summary per cluster per day listing
ALL alert groups (and log patterns) grouped by chronicity, with a
`rolled_into` column linking the Finding-issue covering each group.

**Delivery destinations** controlled by Doppler key `DIGEST_SUMMARY`
(CSV, empty = disabled):

    DIGEST_SUMMARY=               # disabled, no delivery
    DIGEST_SUMMARY=issue          # file as GH issue only
    DIGEST_SUMMARY=email          # send as email only
    DIGEST_SUMMARY=email,issue    # both

Cost impact: zero LLM cost (uses AlertGroup data already in memory).
1-2 extra GH API calls per delivery + 1 SMTP send per email recipient.

Issue lifecycle:
  - New issue per day per cluster (label `digest-summary` +
    `cluster-{dev,prd}` + `mode-A`)
  - Yesterday's issue auto-closed when today's is created — operator
    gets a fresh email notification each morning.
  - Title carries the date + raw counts so the inbox preview is
    self-explanatory without opening the issue.

Email lifecycle:
  - One email per cluster per day to `DIGEST_SUMMARY_EMAIL_TO`.
  - Subject: same as issue title.
  - Body: same markdown rendered as plain text + (Pre-wrapped) HTML
    alternative so tables stay aligned in HTML-capable clients.
"""
from __future__ import annotations
import datetime as dt
import html
import logging
import os
from typing import Any

from ..schema import AlertGroup, Finding, LogPattern
from ..tools.email import send_email
from ..tools.github import gh_issue_close, gh_issue_create, gh_issue_list


log = logging.getLogger(__name__)


def parse_destinations(raw: str | None) -> set[str]:
    """Parse the DIGEST_SUMMARY CSV into a set of destination tokens.

    Empty/unset → empty set (= disabled). Unknown tokens are silently
    dropped with a warning log — fail-open prevents a typo from
    breaking the whole digest.

    >>> sorted(parse_destinations(""))
    []
    >>> sorted(parse_destinations("issue"))
    ['issue']
    >>> sorted(parse_destinations("email, issue"))
    ['email', 'issue']
    >>> sorted(parse_destinations("EMAIL,issue,bogus"))
    ['email', 'issue']
    """
    valid = {"email", "issue"}
    if not raw:
        return set()
    tokens = {tok.strip().lower() for tok in raw.split(",") if tok.strip()}
    unknown = tokens - valid
    if unknown:
        log.warning("DIGEST_SUMMARY: ignoring unknown destination(s): %s",
                    sorted(unknown))
    return tokens & valid


# Chronicity bucket order for rendering (chronic/flapping = most
# operator-relevant noise → top; transients near the bottom).
_CHRONICITY_ORDER = ["chronic", "flapping", "active", "self_healed", "transient"]


def _bucket_label(chronicity: str) -> str:
    """Human-readable section header per chronicity bucket."""
    return {
        "chronic":     "Chronic (firing > 6h, never resolved)",
        "flapping":    "Flapping (> 3 fire/resolve cycles in 24h)",
        "active":      "Active (currently firing, < 6h)",
        "self_healed": "Self-healed transient (single cycle)",
        "transient":   "Transient (single short fire)",
    }.get(chronicity, chronicity.replace("_", " ").title())


def _fmt_duration(seconds: int) -> str:
    """Format seconds as e.g. '2h 14m' / '47m' / '38s'."""
    if seconds >= 3600:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h}h {m}m" if m else f"{h}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _fmt_time(ts: dt.datetime) -> str:
    """Format datetime as 'HH:MM UTC May-26' for terse table cells."""
    return ts.strftime("%H:%M UTC %b-%d")


def _alert_target(g: AlertGroup) -> str:
    """Best single-string identifier for what this alert is firing on.

    Strategy: try semantic label keys in priority order; fall back to
    raw labels view if nothing matched. Different alert families carry
    different label shapes:
      - Pod-level alerts (PodCrashLoopBackOff, KubePodNotReady) → pod label
      - Workload-level (KubeStatefulSetReplicasMismatch) → statefulset/deployment
      - Flux alerts (FluxKustomizationNotReady) → name + exported_namespace
      - Job-level (KubeJobFailed) → namespace + job_name
      - LokiRequestErrors → route + namespace
      - Generic → namespace alone
    """
    L = g.labels or {}
    ns = L.get("namespace") or L.get("exported_namespace") or ""

    if L.get("pod"):
        return f"{ns}/{L['pod']}" if ns else L["pod"]
    for key in ("statefulset", "deployment", "daemonset", "replicaset", "job", "job_name"):
        if L.get(key):
            return f"{ns}/{L[key]} ({key})" if ns else f"{L[key]} ({key})"
    # Flux: alerts on Kustomization/HelmRelease resources
    if L.get("name") and L.get("kind"):
        # exported_namespace is the Flux namespace, often "flux-system"
        return f"{L.get('exported_namespace') or ns or 'flux-system'}/{L['name']} ({L['kind']})"
    if L.get("route"):
        return f"{ns}/{L['route']}" if ns else L["route"]
    if L.get("container") and ns:
        return f"{ns}/{L['container']}"
    if ns:
        return ns
    # Last resort: show 2-3 most-distinguishing label k=v pairs so the
    # operator can tell rows apart even when no semantic key matched.
    skip = {"alertname", "severity", "alertstate", "prometheus", "tenant_id"}
    pairs = [f"{k}={v}" for k, v in L.items() if k not in skip][:3]
    return ", ".join(pairs) if pairs else "—"


def _finding_index_for_group(
    group: AlertGroup,
    findings: list[Finding],
    dispatch_refs: dict[str, str],
) -> str:
    """Return '#<issue_number>' if this AlertGroup is plausibly covered
    by one of the emitted findings; else empty string.

    `dispatch_refs` maps Finding.id → "owner/repo#NN" (from DispatchResult).
    Heuristic: match alertname substring in any evidence-source field.
    """
    for f in findings:
        ref = dispatch_refs.get(f.id, "")
        if not ref:
            continue
        for ev in getattr(f, "evidence", []) or []:
            source = getattr(ev, "source", "") or ""
            if group.alertname in source:
                return "#" + ref.rsplit("#", 1)[-1]
    return ""


def _render_groups_table(
    groups: list[AlertGroup],
    findings: list[Finding],
    dispatch_refs: dict[str, str],
) -> str:
    """Render an alert-groups markdown table, one row per group.

    Columns kept terse so the issue is scannable on mobile email
    notifications. `rolled into` column shows the GH issue number
    when a Finding covers this group, blank when it's orphan noise.
    """
    if not groups:
        return "_(none)_\n"
    rows = [
        "| alertname | target | fires | total firing | last seen | rolled into |",
        "|---|---|---:|---:|---|---|",
    ]
    for g in groups:
        rows.append(
            "| `{}` | {} | {} | {} | {} | {} |".format(
                g.alertname,
                _alert_target(g),
                g.fire_count,
                _fmt_duration(g.total_firing_seconds),
                _fmt_time(g.last_seen_at),
                _finding_index_for_group(g, findings, dispatch_refs) or " ",
            )
        )
    return "\n".join(rows) + "\n"


def _render_log_patterns_table(patterns: list[LogPattern]) -> str:
    """One-row-per-pattern table for the log-mining tail of the digest.

    `signal` is either the tripwire label (e.g. "OOMKilled") or the
    ratio descriptor ("3.4× baseline") for ratio-outlier patterns.
    """
    if not patterns:
        return "_(none)_\n"
    rows = [
        "| signal | namespace | level | count_24h | baseline | chronicity |",
        "|---|---|---|---:|---|---|",
    ]
    for p in patterns:
        if p.matched_tripwire:
            signal = p.matched_tripwire
            baseline = "—"
        else:
            signal = "ratio outlier"
            if p.ratio_vs_baseline is not None:
                baseline = f"{p.baseline_mean_24h:.0f} avg, {p.ratio_vs_baseline:.1f}×"
            else:
                baseline = "—"
        rows.append(
            "| `{}` | `{}` | {} | {} | {} | {} |".format(
                signal,
                p.namespace,
                p.level,
                p.count_24h,
                baseline,
                p.chronicity,
            )
        )
    return "\n".join(rows) + "\n"


def render_summary_body(
    *,
    cluster: str,
    groups: list[AlertGroup],
    log_patterns: list[LogPattern],
    findings: list[Finding],
    dispatch_refs: dict[str, str],
    window_hours: int,
    model: str,
    digest_summary: str,
) -> str:
    """Produce the markdown body for the per-cluster daily summary issue.

    `dispatch_refs` maps finding.id → "owner/repo#NN" so we can show
    GH issue links for both the actionable-findings list and the
    `rolled into` column.
    """
    # Watchdog is the synthetic always-firing alert that backs the
    # dead-man's-switch. Including it in "chronic" is technically
    # correct but misleading — operators expect it to fire forever.
    # Silently drop it from the rendering.
    groups = [g for g in groups if g.alertname != "Watchdog"]

    by_chronicity: dict[str, list[AlertGroup]] = {c: [] for c in _CHRONICITY_ORDER}
    for g in groups:
        by_chronicity.setdefault(g.chronicity, []).append(g)

    # Sort each bucket: chronic/flapping/active by total_firing_seconds desc
    # (worst first), self_healed/transient by last_seen_at desc (recent first).
    for c in ("chronic", "flapping", "active"):
        by_chronicity[c].sort(key=lambda g: -g.total_firing_seconds)
    for c in ("self_healed", "transient"):
        by_chronicity[c].sort(key=lambda g: g.last_seen_at, reverse=True)

    if not findings:
        actionable_block = (
            "_(no Findings emitted — quiet day, or all activity was "
            "covered by existing open issues / judged not worth a "
            "separate issue)_"
        )
    else:
        actionable_block = "\n".join(
            "- {ref} **[{sev}]** {title}".format(
                ref="#" + dispatch_refs[f.id].rsplit("#", 1)[-1]
                if dispatch_refs.get(f.id)
                else "(no issue)",
                sev=getattr(f, "severity", "?"),
                title=getattr(f, "title", "(untitled)"),
            )
            for f in findings
        )

    sections: list[str] = []
    sections.append(
        f"## Window\n\n{window_hours}h ending "
        f"{_fmt_time(dt.datetime.now(dt.timezone.utc))} · "
        f"model: `{model}` · cluster: **{cluster}**"
    )
    if digest_summary:
        sections.append(f"## Digest summary (LLM)\n\n{digest_summary}")
    sections.append(
        f"## Actionable findings ({len(findings)})\n\n" + actionable_block
    )
    sections.append(
        f"## Background activity ({len(groups)} alert groups, "
        f"Watchdog excluded)\n\n"
        + "The full alert-group landscape the LLM saw. Items with a "
        + "`rolled into` reference are covered by the actionable "
        + "findings above. **Chronic & flapping deserve a second look** "
        + "— something is repeatedly going wrong even if it self-heals. "
        + "Self-healed/transient is presumed noise but listed for "
        + "completeness."
    )
    for chronicity in _CHRONICITY_ORDER:
        gs = by_chronicity.get(chronicity, [])
        sections.append(
            f"### {_bucket_label(chronicity)} — {len(gs)}\n\n"
            + _render_groups_table(gs, findings, dispatch_refs)
        )
    if log_patterns:
        sections.append(
            f"## Log patterns ({len(log_patterns)})\n\n"
            + _render_log_patterns_table(log_patterns)
        )
    sections.append(
        "---\n"
        f"`digest-summary:{cluster}:"
        f"{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')}`"
    )
    return "\n\n".join(sections)


def _close_previous_summaries(
    *, repo: str, cluster: str, today_iso: str
) -> int:
    """Find any open summary issues for this cluster from a previous
    day and close them with a 'superseded' comment.

    Returns count closed. Tolerant of API errors — logs but doesn't
    raise, since the new issue creation is more important than the
    cleanup of old ones.
    """
    try:
        issues = gh_issue_list(
            repo, labels=["digest-summary", f"cluster-{cluster}"], state="open"
        )
    except Exception as e:
        log.warning("close_previous_summaries: list failed: %r", e)
        return 0

    closed = 0
    for issue in issues:
        # Skip if this is today's issue (defensive — we call this BEFORE
        # creating today's, so normally none should match, but if a manual
        # re-run happened the title-date check prevents self-close).
        if today_iso in (issue.get("title") or ""):
            continue
        try:
            gh_issue_close(
                repo,
                int(issue["number"]),
                comment=f"Superseded by today's summary issue ({today_iso}).",
            )
            closed += 1
        except Exception as e:
            log.warning("close_previous_summaries: close #%s failed: %r",
                        issue.get("number"), e)
    return closed


def _markdown_to_simple_html(body_md: str) -> str:
    """Wrap markdown body in a minimal HTML document with <pre> tags
    so tables stay aligned in HTML-capable mail clients (Gmail wraps
    plain-text-monospace badly otherwise).

    Not a real markdown→HTML conversion — we ship the markdown verbatim
    inside a <pre> block. Email clients that prefer HTML get readable
    monospace; clients that fall back to plain-text get the raw
    markdown directly. No new dependency.
    """
    escaped = html.escape(body_md)
    return (
        "<!doctype html><html><body>"
        '<pre style="font-family: ui-monospace, SFMono-Regular, Menlo, '
        'Consolas, monospace; font-size: 12px; line-height: 1.45; '
        'white-space: pre-wrap;">'
        f"{escaped}"
        "</pre></body></html>"
    )


def emit_summary_email(
    *,
    cluster: str,
    title: str,
    body_md: str,
) -> bool:
    """Deliver the daily summary as an email.

    Returns True on success, False on any failure (which is logged but
    never raised — summary delivery is best-effort).
    """
    to = os.environ.get("DIGEST_SUMMARY_EMAIL_TO", "").strip()
    if not to:
        log.warning(
            "emit_summary_email: DIGEST_SUMMARY_EMAIL_TO is unset; "
            "cannot deliver %s email", cluster,
        )
        return False
    try:
        send_email(
            to=to,
            subject=title,
            body_text=body_md,
            body_html=_markdown_to_simple_html(body_md),
        )
        return True
    except Exception as e:
        log.warning("emit_summary_email: send failed for %s: %r", cluster, e)
        return False


def _build_summary_title(*, cluster: str, groups: list[AlertGroup],
                         findings: list[Finding]) -> str:
    """Title shared by both the GH issue title and email subject.

    Excludes Watchdog from counts so the inbox-preview matches what
    the operator actually sees in the body's tables.
    """
    today_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    non_watchdog = [g for g in groups if g.alertname != "Watchdog"]
    return (
        f"Daily digest — {cluster} {today_iso} "
        f"({len(non_watchdog)} alerts · {len(findings)} actionable · "
        f"{max(0, len(non_watchdog) - len(findings))} background)"
    )


def emit_summary_issue(
    *,
    repo: str,
    cluster: str,
    title: str,
    body: str,
) -> dict[str, Any] | None:
    """Create today's summary issue (and close yesterday's).

    Title + body are pre-rendered by the caller (typically via
    `_build_summary_title` + `render_summary_body`), so the same
    rendered output can be reused for the email path.

    Returns the GH API response dict on success, None on failure.
    Failure to file the summary should NOT fail the entire digest run
    — the actionable findings are the primary deliverable; the summary
    is a UX nicety. All exceptions caught + logged.
    """
    today_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    closed = _close_previous_summaries(
        repo=repo, cluster=cluster, today_iso=today_iso
    )
    if closed:
        log.info("emit_summary_issue: closed %d previous summary(ies)", closed)
    try:
        resp = gh_issue_create(
            repo,
            title=title,
            body=body,
            labels=["digest-summary", f"cluster-{cluster}", "mode-A"],
        )
        log.info("emit_summary_issue: filed %s#%s",
                 repo, resp.get("number"))
        return resp
    except Exception as e:
        log.warning("emit_summary_issue: create failed: %r", e)
        return None


def emit_summary(
    *,
    repo: str,
    cluster: str,
    groups: list[AlertGroup],
    log_patterns: list[LogPattern],
    findings: list[Finding],
    dispatch_refs: dict[str, str],
    window_hours: int,
    model: str,
    digest_summary: str,
) -> dict[str, bool]:
    """Top-level summary-delivery orchestrator.

    Reads `DIGEST_SUMMARY` from env (CSV: "email", "issue",
    "email,issue", or empty = disabled), renders title + body once,
    and dispatches to each requested destination. Returns a dict
    mapping destination → success bool for caller visibility.
    Both destinations are best-effort; an exception in one does
    NOT block the other or fail the digest.
    """
    destinations = parse_destinations(os.environ.get("DIGEST_SUMMARY"))
    if not destinations:
        return {}

    title = _build_summary_title(cluster=cluster, groups=groups, findings=findings)
    body = render_summary_body(
        cluster=cluster,
        groups=groups,
        log_patterns=log_patterns,
        findings=findings,
        dispatch_refs=dispatch_refs,
        window_hours=window_hours,
        model=model,
        digest_summary=digest_summary,
    )

    results: dict[str, bool] = {}
    if "issue" in destinations:
        try:
            resp = emit_summary_issue(
                repo=repo, cluster=cluster, title=title, body=body,
            )
            results["issue"] = resp is not None
        except Exception as e:
            log.warning("emit_summary[issue]: %r", e)
            results["issue"] = False
    if "email" in destinations:
        try:
            results["email"] = emit_summary_email(
                cluster=cluster, title=title, body_md=body,
            )
        except Exception as e:
            log.warning("emit_summary[email]: %r", e)
            results["email"] = False
    log.info("emit_summary %s: %s", cluster, results)
    return results
