"""LaTeX-tolerant grammar for llama.cpp (the json_schema grammar forced `\\lambda` into `\\frac{\\tilde...}`)."""

import json
import re

import pytest

from axiom.model_adapters.base import ChatRequest
from axiom.model_adapters.gbnf import UnsupportedSchema, schema_to_gbnf
from axiom.model_adapters.openai_compat import OpenAICompatAdapter


def _rules(g: str) -> tuple[set[str], set[str]]:
    defined = {l.split(" ::= ")[0] for l in g.splitlines() if " ::= " in l}
    bodies = " ".join(l.split(" ::= ", 1)[1] for l in g.splitlines() if " ::= " in l)
    bodies = re.sub(r'"(?:[^"\\]|\\.)*"', " ", bodies)  # drop literals
    bodies = re.sub(r"\[(?:[^\]\\]|\\.)*\]", " ", bodies)  # drop char classes
    used = set(re.findall(r"[a-zA-Z][a-zA-Z0-9-]*", bodies))
    return defined, used


def test_every_axiom_schema_converts_and_all_rules_are_defined():
    from axiom.exercises import mastery, solutions
    from axiom.transformations import optimize
    from axiom.verification import faithfulness
    from axiom.visualization import generate
    for sc in (optimize._schema(["B1"]), faithfulness._schema(["U1"]), solutions.SOLUTION_SCHEMA,
               solutions.COMPARE_SCHEMA, solutions.CHECK_SCHEMA, mastery._schema(["U1"], 6), generate.SCHEMA,
               generate.REVIEW_SCHEMA):
        for reasoning in (False, True):
            defined, used = _rules(schema_to_gbnf(sc, reasoning))
            assert used <= defined, used - defined


def test_string_rule_accepts_backslash_letter_and_reasoning_prefix():
    g = schema_to_gbnf({"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"],
                        "additionalProperties": False}, reasoning=True)
    assert '"\\\\" ( ["\\\\/bfnrt] | "u" [0-9a-fA-F]{4} | [a-zA-Z] )' in g  # \lambda is allowed
    assert g.splitlines()[[l.startswith("root ::=") for l in g.splitlines()].index(True)].startswith(
        'root ::= thought "</think>"')


def test_unsupported_schema_falls_back_to_response_format(monkeypatch):
    with pytest.raises(UnsupportedSchema):
        schema_to_gbnf({"type": "object", "properties": {"a": {"type": "string", "pattern": "x"}}, "required": ["a"]})
    sent = []

    class R:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "model": "m"}
    ad = OpenAICompatAdapter("http://x/v1", "m", "local_text")
    monkeypatch.setattr(ad._client, "post", lambda url, json: (sent.append(json), R())[1])
    ok = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": False}
    ad.chat(ChatRequest("s", "u", json_schema=ok, thinking=True))
    ad.chat(ChatRequest("s", "u", json_schema={"type": "object", "properties": {"a": {"pattern": "x"}}, "required": ["a"]}))
    assert "grammar" in sent[0] and "response_format" not in sent[0] and "</think>" in sent[0]["grammar"]
    assert "response_format" in sent[1] and "grammar" not in sent[1]
    remote = OpenAICompatAdapter("http://x/v1", "m", "premium", local_llamacpp=False)
    monkeypatch.setattr(remote._client, "post", lambda url, json: (sent.append(json), R())[1])
    remote.chat(ChatRequest("s", "u", json_schema=ok))
    assert "response_format" in sent[2]


def test_cache_key_depends_on_decoding_only_for_schema_requests():
    from axiom.orchestration.llm import _cache_key
    sch = ChatRequest("s", "u", json_schema={"type": "string"})
    txt = ChatRequest("s", "u")
    assert _cache_key("m", "t", "v", sch, "gbnf-latex-v1") != _cache_key("m", "t", "v", sch, "")
    assert _cache_key("m", "t", "v", txt, "gbnf-latex-v1") == _cache_key("m", "t", "v", txt, "")  # transcriptions keep their cache


def test_backslash_letter_output_is_repaired_by_parse_json():
    from axiom.orchestration.llm import parse_json
    raw = '{"text": "wavelength $\\lambda_0$, $\\gamma = 1/\\sqrt{1-v^2}$, $A_0 = \\phi$, $U_\\alpha \\cap U_\\beta \\neq \\emptyset$"}'
    assert parse_json(raw)["text"] == ("wavelength $\\lambda_0$, $\\gamma = 1/\\sqrt{1-v^2}$, $A_0 = \\phi$, "
                                       "$U_\\alpha \\cap U_\\beta \\neq \\emptyset$")
    json.dumps(parse_json(raw))
