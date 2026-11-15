"""Cross-run claim clustering and majority verdicts. (Phase 6)

For runs = 3: claims are clustered across runs by quote similarity (rapidfuzz ratio >= 85 on the quote match
keys) or by overlapping spans (the same text quoted with different sentence boundaries); at most one claim per
run in a cluster. Clusters present in >= 2 runs are kept. The verdict is the
majority; a tie goes to the **less** severe verdict. `Stability(runs, agree, verdicts)` with agree = the number
of runs that gave the final verdict. The claim shown is the first run's member with that verdict.
"""

from __future__ import annotations

from collections import Counter

from rapidfuzz import fuzz

from onus.claims.quotes import match_key
from onus.schemas.result import RISK, Claim, Stability, Verdict

CLUSTER_MIN = 85.0
MIN_RUNS = 2


def _overlap(a: Claim, b: Claim) -> float:
    """Shared characters over the shorter span (0 when the spans do not overlap)."""
    lo, hi = max(a.location.start, b.location.start), min(a.location.end, b.location.end)
    short = min(a.location.end - a.location.start, b.location.end - b.location.start)
    return (hi - lo) / short if hi > lo and short > 0 else 0.0


def cluster(runs: list[list[Claim]]) -> list[list[tuple[int, Claim]]]:
    clusters: list[list[tuple[int, Claim]]] = []
    keys: list[str] = []  # match key of each cluster's first member
    for r, claims in enumerate(runs):
        for c in claims:
            k = match_key(c.quote)
            best, best_score = None, CLUSTER_MIN
            for i, cl in enumerate(clusters):
                if any(rr == r for rr, _ in cl):
                    continue
                first = cl[0][1]
                if "checklist" in (first.type, c.type) and (first.type, first.checklist_key) != (c.type,
                                                                                               c.checklist_key):
                    continue  # checklist claims only cluster with the same item
                score = 100.0 if k == keys[i] else fuzz.ratio(k, keys[i])
                if score < CLUSTER_MIN and c.type != "checklist" and _overlap(c, first) > 0:
                    score = CLUSTER_MIN + 10 * _overlap(c, first)  # same text, different boundaries
                if score > best_score or (best is None and score >= CLUSTER_MIN):
                    best, best_score = i, score
            if best is None:
                clusters.append([(r, c)])
                keys.append(k)
            else:
                clusters[best].append((r, c))
    return clusters


def majority(verdicts: list[Verdict]) -> Verdict:
    counts = Counter(verdicts)
    top = max(counts.values())
    tied = [v for v, n in counts.items() if n == top]
    return min(tied, key=lambda v: RISK[v])


def merge_runs(runs: list[list[Claim]]) -> tuple[list[Claim], int]:
    """Final claims and the number of single-run clusters dropped."""
    n = len(runs)
    if n == 1:
        return [c.model_copy() for c in runs[0]], 0
    out: list[Claim] = []
    dropped = 0
    for cl in cluster(runs):
        if len({r for r, _ in cl}) < min(MIN_RUNS, n):
            dropped += 1
            continue
        cl.sort(key=lambda x: x[0])
        verdicts = [c.verdict for _, c in cl]
        final = majority(verdicts)
        rep = next(c for _, c in cl if c.verdict == final)
        out.append(rep.model_copy(update={"stability": Stability(runs=n, agree=verdicts.count(final),
                                                                 verdicts=verdicts)}))
    return out, dropped
