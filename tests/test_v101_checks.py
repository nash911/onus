"""v1.0.1: a ± spread is not a claimed value, and wording checks do not lift partial-only evidence."""

from __future__ import annotations

from onus.checks.numbers import extract_numbers
from onus.verdict.rules import rule_verdict
from tests.test_verdict import LINK_PASS, ck, ev


def test_spread_after_plus_minus_is_not_a_claimed_number() -> None:
    got = [(m.value, m.unit) for m in extract_numbers("reaches 96.81 ± 0.42% accuracy and an F1 of 96.79±0.42%")]
    assert got == [(96.81, "percent"), (96.79, "percent")]
    assert [m.value for m in extract_numbers("0.81 +/- 0.02 vs 0.75")] == [0.81, 0.75]
    assert [(m.value, m.unit) for m in extract_numbers("85.2% ± 1.3 on average")] == [(85.2, "percent")]
    assert [m.value for m in extract_numbers("a gain of 12.5% over 3.25")] == [12.5, 3.25]


def test_wording_checks_do_not_lift_partial_only_evidence() -> None:
    partial = [ev("ev1", "partial", "p. 7, ¶ 5", "text")]
    r = rule_verdict(partial, [LINK_PASS, ck("strength", "pass"), ck("scope", "pass")], [])
    assert (r.verdict, r.row) == ("not_checkable", 4)
    r = rule_verdict(partial, [LINK_PASS, ck("number", "pass")], [])
    assert r.verdict == "supported"
    r = rule_verdict([ev("ev1", "supports", "p. 7, ¶ 5", "text")], [LINK_PASS, ck("strength", "pass")], [])
    assert r.verdict == "supported"
