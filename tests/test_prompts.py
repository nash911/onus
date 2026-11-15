from __future__ import annotations

import pytest

from onus.llm.prompts import PromptError, PromptRegistry, parse_prompt, render_template
from tests.conftest import write_prompt


def test_render_substitutes_and_allows_spaces():
    assert render_template("a {{x}} b {{ y }}", {"x": 1, "y": "two"}) == "a 1 b two"


def test_render_missing_variable_is_an_error():
    with pytest.raises(PromptError, match="z"):
        render_template("{{x}} {{z}}", {"x": 1})


def test_render_is_single_pass():
    # paper text containing {{...}} must not be expanded again
    assert render_template("{{text}}", {"text": "see {{secret}}", "secret": "X"}) == "see {{secret}}"


def test_parse_with_system_and_user_sections(prompts_tmp):
    p = write_prompt(prompts_tmp, "demo", "# system\nBe exact.\n\n# user\nClaim: {{claim}}", role="judge")
    prompt = PromptRegistry(prompts_tmp).get("demo")
    assert prompt.path == p and prompt.role == "judge" and prompt.version == "1"
    assert prompt.variables() == {"claim"}
    assert prompt.render({"claim": "X"}) == [
        {"role": "system", "content": "Be exact."},
        {"role": "user", "content": "Claim: X"},
    ]


def test_parse_without_sections_is_user_only(prompts_tmp):
    write_prompt(prompts_tmp, "plain", "Just {{a}}.")
    assert PromptRegistry(prompts_tmp).get("plain").render({"a": "this"}) == [
        {"role": "user", "content": "Just this."}
    ]


def test_front_matter_is_required_and_validated(prompts_tmp):
    with pytest.raises(PromptError, match="front matter"):
        parse_prompt("no front matter")
    with pytest.raises(PromptError, match="role"):
        parse_prompt("---\nid: x\nversion: 1\nrole: wizard\ntemperature: 0\nmax_tokens: 5\n---\nhi")
    with pytest.raises(PromptError, match="max_tokens"):
        parse_prompt("---\nid: x\nversion: 1\nrole: fast\ntemperature: 0\n---\nhi")


def test_id_must_match_file_name(prompts_tmp):
    (prompts_tmp / "a.md").write_text("---\nid: b\nversion: 1\nrole: fast\ntemperature: 0\nmax_tokens: 5\n---\nhi")
    with pytest.raises(PromptError, match="file name"):
        PromptRegistry(prompts_tmp).get("a")


def test_registry_lists_ids_and_reloads_on_change(prompts_tmp):
    p = write_prompt(prompts_tmp, "one", "v1")
    reg = PromptRegistry(prompts_tmp)
    assert reg.ids() == ["one"]
    assert reg.get("one").user == "v1"
    import os
    import time

    write_prompt(prompts_tmp, "one", "v2")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    time.sleep(0)
    assert reg.get("one").user == "v2"


def test_missing_prompt(prompts_tmp):
    with pytest.raises(PromptError, match="No prompt file"):
        PromptRegistry(prompts_tmp).get("nope")
