"""Phase 16 (v1.1) pipeline fixes, on synthetic fixtures."""

from __future__ import annotations

from onus.checks.text_values import compare_text_values, quantities
from onus.evidence.explicit import ObjectIndex, external_pointers
from onus.evidence.select import _link_check
from onus.schemas.result import CheckResult, Evidence
from onus.verdict.adjudicate import code_parse_problems
from onus.verdict.rules import rule_verdict
from tests.test_claims import make_doc

# ---------------------------------------------------------------- 1. evidence outside the paper


def _span(doc, quote: str) -> tuple[int, int]:
    i = doc.text.index(quote)
    return i, i + len(quote)


def _doc(extra_section: tuple[str, str, list[str]] | None = None):
    secs = [("Introduction", "introduction", ["We study robot grasping in clutter."]),
            ("Results", "results", [("The robot grasps 9 of 10 objects. Videos of every run are shown in the "
                                    "accompanying video."), ("We use the model to generate educational videos for "
                                    "students."), "Further ablations are given in the supplementary material.",
                                    "Our accuracy is 81%, as reported in Table 1."])]
    if extra_section:
        secs.append(extra_section)
    return make_doc(secs)


def test_pointers_outside_the_pdf_are_found_in_the_claim_context():
    doc = _doc()
    idx = ObjectIndex(doc)
    assert external_pointers(idx, *_span(doc, "The robot grasps 9 of 10 objects.")) == ["a video"]
    assert external_pointers(idx, *_span(doc, "Further ablations are given in the supplementary material.")) == [
        "supplementary material"]
    # a claim about videos is not a pointer to one
    assert external_pointers(idx, *_span(doc, "Our accuracy is 81%, as reported in Table 1.")) == [
        "supplementary material"]  # ±1 sentence: the previous sentence points outside
    doc2 = make_doc([("Results", "results", ["We use the model to generate educational videos for students."])])
    assert external_pointers(ObjectIndex(doc2), *_span(doc2, "We use the model to generate educational videos")) == []


def test_a_supplementary_section_in_the_pdf_is_inside_the_paper():
    doc = _doc(("Supplementary Material", "appendix", ["Ablation A removes the gripper camera."]))
    idx = ObjectIndex(doc)
    assert external_pointers(idx, *_span(doc, "Further ablations are given in the supplementary material.")) == []
    assert external_pointers(idx, *_span(doc, "The robot grasps 9 of 10 objects.")) == ["a video"]


def _link(external=(), dangling=(), evidence=()):
    return _link_check(list(evidence), list(dangling), 5, False, list(external))


def test_only_an_outside_pointer_makes_the_claim_not_checkable():
    link = _link(external=["a video"])
    assert link.outcome == "inconclusive" and "outside the paper" in link.detail
    rv = rule_verdict([], [link], [])
    assert rv.verdict == "not_checkable" and rv.row == 2


def test_no_pointer_or_a_dangling_reference_stays_untraced():
    assert rule_verdict([], [_link()], []).verdict == "untraced"
    link = _link(external=["supplementary material"], dangling=["Table 7"])
    assert rule_verdict([], [link], ["Table 7"]).verdict == "untraced"


def test_in_paper_evidence_wins_over_an_outside_pointer():
    ev = Evidence(id="E1", kind="text", label="p. 2, ¶ 1", snippet="9 of 10", relation="supports",
                  link_method="llm_selected")
    link = _link(external=["a video"], evidence=[ev])
    assert link.outcome == "pass" and link.data["external"] == ["a video"]
    assert rule_verdict([ev], [link, CheckResult(check="strength", outcome="pass", detail="")], []).verdict == \
        "supported"


# ---------------------------------------------------------------- 3. text values against contradicting passages



def _ev(eid: str, relation: str, kind: str = "text") -> Evidence:
    return Evidence(id=eid, kind=kind, label=f"p. 1, ¶ {eid}", snippet="", relation=relation, link_method="llm_selected")


def test_quantities_read_units_and_bare_integers_but_not_names():
    q = quantities("Training used a single NVIDIA A100 GPU with 24 CPU cores and took approximately 4 hours (81.5%).")
    assert [(x.value, x.unit) for x in q] == [(24.0, "cores"), (4.0, "hours"), (81.5, "%")]
    assert quantities("see Table 3 and Section 4 hours later") == []


def test_a_contradicting_passage_with_another_value_is_a_code_mismatch():
    claim = "Training took approximately 4 hours on one GPU."
    mm = compare_text_values(claim, [(_ev("E1", "contradicts"), "The full training run took 6 hours on one GPU.")])
    assert len(mm) == 1 and mm[0].claimed.text == "4 hours" and [v.text for v in mm[0].found] == ["6 hours"]


def test_no_mismatch_when_the_value_is_printed_or_the_passage_only_points():
    claim = "Training took approximately 4 hours."
    # the claim's value appears in another linked passage: not a mismatch
    assert compare_text_values(claim, [(_ev("E1", "contradicts"), "Inference took 6 hours."),
                                       (_ev("E2", "supports"), "Training took 4 hours.")]) == []
    # a passage the model did not mark as contradicting is never used to flag
    assert compare_text_values(claim, [(_ev("E1", "partial"), "Inference took 6 hours.")]) == []
    # a contradicting passage that states no value in that unit
    assert compare_text_values(claim, [(_ev("E1", "contradicts"), "Training was slow.")]) == []
    # rounding to the claim's precision
    assert compare_text_values("Accuracy is 81%.", [(_ev("E1", "contradicts"), "Accuracy reached 81.3%.")]) == []


async def test_number_check_uses_text_values_only_where_no_table_comparison_ran():
    from onus.checks.context import ClaimContext
    from onus.checks.numbers import check_numbers

    class Q:
        quote = "Training took approximately 4 hours on a single GPU."

    ctx = ClaimContext(claim=Q(), ce=None, linked=[(_ev("E1", "contradicts"), "Training took 6 hours in total.")])
    r = await check_numbers(ctx, llm=None, run_index=0, paper_id="p", metas=[])
    assert r.outcome == "fail" and r.severity == "contradicts" and r.evidence_ids == ["E1"]
    assert r.data["text_values"][0]["found"] == ["6 hours"] and r.data["table_check"] == "no number in the claim"
    # rule row 1: a computed contradiction
    link = _link(evidence=[_ev("E1", "contradicts")])
    assert rule_verdict([_ev("E1", "contradicts")], [link, r], []).verdict == "contradicted"
    # the judge may downgrade a unit-only pairing to not_checkable (a code-recorded parse problem), never clear it
    assert code_parse_problems([r]) and "by unit only" in code_parse_problems([r])[0]
