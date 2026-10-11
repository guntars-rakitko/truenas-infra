"""Tests for the daily summary body (modes/summary_issue.py)."""
from __future__ import annotations

from cluster_agent.modes.summary_issue import render_summary_body


def _body(**kw) -> str:
    return render_summary_body(
        cluster="dev", groups=[], log_patterns=[], findings=[],
        dispatch_refs={}, window_hours=24, model="m", digest_summary="",
        **kw,
    )


def test_summary_names_log_mining_coverage_gaps():
    """An unchecked tripwire must not read like a clean one: the summary
    lists every Loki query that failed."""
    body = _body(log_mining_failures=[
        "tripwire OOMKilled: ReadTimeout",
        "ratio_outlier: RuntimeError",
    ])
    assert "Log-mining coverage gaps (2)" in body
    assert "- `tripwire OOMKilled: ReadTimeout`" in body
    assert "- `ratio_outlier: RuntimeError`" in body
    assert "not checked" in body


def test_summary_has_no_gap_section_when_every_query_ran():
    assert "coverage gaps" not in _body()
    assert "coverage gaps" not in _body(log_mining_failures=[])


# ── Rolled into, bucket headers, known events (2026-10-10) ────────────

import datetime as dt

from cluster_agent.modes.digest_aggregator import aggregate
from cluster_agent.modes.summary_issue import _bucket_label
from cluster_agent.schema import AlertGroup, Evidence, Finding, KnownEvent, LogPattern

_T = dt.datetime(2026, 10, 10, 14, 0, tzinfo=dt.timezone.utc)


def _group(alertname, signal_id=None, tracked_by=None, chronicity="chronic"):
    return AlertGroup(alertname=alertname, fingerprint=f"fp-{alertname}", labels={"namespace": "giks-db"},
                      fire_count=1, total_firing_seconds=7200, chronicity=chronicity,
                      first_seen_at=_T, last_seen_at=_T, currently_firing=True,
                      signal_id=signal_id, tracked_by=tracked_by)


def _finding(fid, signals, evidence_ref="Alertmanager/Nothing@now"):
    return Finding(id=fid * 26, mode="A", cluster="dev", severity="low", title=f"finding {fid}",
                   summary="s", evidence=[Evidence(type="alert", ref=evidence_ref)], confidence=0.5,
                   dedup_key="k", signals=signals)


def _row(body, needle):
    return next(line for line in body.splitlines() if needle in line)


def test_rolled_into_follows_the_signals_a_finding_cites():
    """Until 2026-10-10 the column read `ev.source`, which Evidence never had,
    so it was empty on every summary."""
    groups = [_group("KubeJobFailed", "A1"), _group("CnpgBackupTooOld", "A2"),
              _group("Lonely", "A3"), _group("Tracked", "A4", tracked_by="guntars-rakitko/kube-infra#1600")]
    pattern = LogPattern(namespace="giks-db", level="fatal", chronicity="tripwire", count_24h=3,
                         matched_tripwire="fatal", first_seen_at=_T, last_seen_at=_T, signal_id="L1")
    findings = [_finding("1", ["A1", "L1"]), _finding("2", ["A2"])]
    refs = {"1" * 26: "guntars-rakitko/kube-infra#1601", "2" * 26: "guntars-rakitko/kube-infra#1602"}
    body = render_summary_body(cluster="dev", groups=groups, log_patterns=[pattern], findings=findings,
                               dispatch_refs=refs, window_hours=24, model="m", digest_summary="")
    assert _row(body, "`KubeJobFailed`").endswith("| guntars-rakitko/kube-infra#1601 |")
    assert _row(body, "`CnpgBackupTooOld`").endswith("| guntars-rakitko/kube-infra#1602 |")
    assert _row(body, "`Lonely`").endswith("|   |")
    assert _row(body, "`Tracked`").endswith("| guntars-rakitko/kube-infra#1600 (already open) |")
    assert _row(body, "| L1 |").endswith("| guntars-rakitko/kube-infra#1601 |")


def test_a_finding_without_signals_still_rolls_up_by_its_alert_evidence():
    groups = [_group("KubeJobFailed", "A1")]
    findings = [_finding("1", [], evidence_ref="Alertmanager/KubeJobFailed@2026-10-10T14:00:00Z")]
    body = render_summary_body(cluster="dev", groups=groups, log_patterns=[], findings=findings,
                               dispatch_refs={"1" * 26: "guntars-rakitko/kube-infra#9"},
                               window_hours=24, model="m", digest_summary="")
    assert _row(body, "`KubeJobFailed`").endswith("| guntars-rakitko/kube-infra#9 |")


def _classify(cycles: list[tuple[int, int]], firing_now: bool) -> str:
    """Classify an alert that fired for each (start_min_ago, length_min)."""
    now = dt.datetime(2026, 10, 11, 3, 0, tzinfo=dt.timezone.utc)
    values = []
    for start, length in cycles:
        values += [[(now - dt.timedelta(minutes=start - i)).timestamp(), "1"] for i in range(length)]
    if firing_now:
        values.append([now.timestamp(), "1"])
    (group,) = aggregate({"data": {"result": [{"metric": {"alertname": "X"}, "values": values}]}}, now=now)
    return group.chronicity


def test_bucket_headers_state_the_thresholds_the_classifier_uses():
    """The headers said "> 6h" and "> 3 cycles" while the code classified at
    1h and >= 3. Each header's claim is checked against aggregate()."""
    assert _bucket_label("chronic") == "Chronic (firing ≥ 1h in total)"
    assert _classify([(300, 61)], False) == "chronic"                       # 61 min, resolved
    assert "≥ 3 fire/resolve cycles" in _bucket_label("flapping")
    assert _classify([(300, 5), (200, 5), (100, 5)], False) == "flapping"   # exactly 3
    assert _classify([(300, 5), (200, 5)], False) == "self_healed"          # 2 cycles
    assert "≤ 5m" in _bucket_label("transient")
    assert _classify([(300, 4)], False) == "transient"
    assert _classify([(300, 10)], False) == "self_healed"                  # one fire > 5 min
    assert _bucket_label("active").startswith("Active (firing now, < 1h")


def test_known_events_and_their_gaps_are_listed():
    events = [KnownEvent(kind="node_boot", at=_T, what="node msa2-dev-01 booted", signal_id="E1")]
    body = _body(known_events=events, known_event_failures=["known events restore drills: ToolError"])
    assert "## Known events (1)" in body
    assert "| E1 | 14:00 UTC Oct-10 | node msa2-dev-01 booted |" in body
    assert "Known-events coverage gaps (1)" in body
    assert "- `known events restore drills: ToolError`" in body
    plain = _body()
    assert "## Known events (0)" in plain and "Known-events coverage gaps" not in plain


def test_a_known_events_noise_names_the_events_and_its_peak():
    noise = LogPattern(namespace="cnpg-system", level="error", chronicity="ratio_outlier", count_24h=94,
                       baseline_mean_24h=12.8, ratio_vs_baseline=7.32, first_seen_at=_T, last_seen_at=_T,
                       peak_hour=_T, peak_share=0.97, explained_by=["E11", "E14"], signal_id="L1")
    still = LogPattern(namespace="kube-system", level="error", chronicity="ratio_outlier", count_24h=1076,
                       baseline_mean_24h=126.7, ratio_vs_baseline=8.49, ratio_outside_events=3.26,
                       first_seen_at=_T, last_seen_at=_T, signal_id="L2")
    body = render_summary_body(cluster="dev", groups=[], log_patterns=[noise, still], findings=[],
                               dispatch_refs={}, window_hours=24, model="m", digest_summary="")
    assert _row(body, "| L1 |").endswith("| 14:00 UTC Oct-10, 97% | ratio_outlier | noise of E11, E14 |")
    assert "| 127 avg, 8.5× (3.3× outside events) | — | ratio_outlier |   |" in _row(body, "| L2 |")
