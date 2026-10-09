"""`llm.language_restriction` against a chat model that THINKS FIRST.

On the node (2026-10-09) the chat model — Qwen3-8B on LocalAI — ignores
`chat_template_kwargs.enable_thinking`, and the restriction call's 120
tokens went to thinking: `content` "" with `finish_reason: length`, on all
451 queries of docs/notes/one-language-per-work.md. So no restriction was
ever honoured and no language plan was ever cached. With "/no_think" in the
prompt the same request answered in 25 tokens (measured). The call now also
names the language the query is written in (one hit per work shows a work
in it), has room for a thought, reads the LAST JSON object of the reply,
has its own timeout, and remembers each answer."""
import pytest

from app import collections as collections_mod, language_plan, llm


class ThinkingChat:
    """A model that thinks before it answers, as Qwen3 does."""

    def __init__(self, answer: str, *, cut: bool = False):
        self.answer, self.cut, self.calls = answer, cut, []

    def __call__(self, messages, temperature=0.3, max_tokens=1500, num_ctx=16384, *,
                 level=None, timeout=None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens, "timeout": timeout})
        thought = '<think>The user wrote {"phrase"?} hmm, maybe {only: de}…'
        if self.cut:
            return thought                       # the budget ended inside the thought
        if "/no_think" not in messages[0]["content"]:
            return ""                            # what LocalAI returned: all thinking, no content
        return thought + "</think>\n" + self.answer


def test_the_model_is_told_not_to_think_and_its_answer_is_read_after_any_thought(monkeypatch):
    fake = ThinkingChat('{"phrase": "solo testi in tedesco", "only": ["de"], "lang": "it"}')
    monkeypatch.setattr(llm, "chat", fake)
    got = llm.language_restriction("la caccia alla balena, solo testi in tedesco")
    assert got == (["de"], "la caccia alla balena"), "still the pair every caller reads"
    assert got.lang == "it", "and the language the query is written in"
    (call,) = fake.calls
    assert call["messages"][0]["content"].endswith("/no_think")
    assert call["max_tokens"] >= 400, "room for a thought where a model thinks anyway"
    assert call["timeout"] == llm.RESTRICT_TIMEOUT_S <= 30, "its own bound on the search's path"


def test_a_reply_that_is_only_thought_is_no_answer_and_is_not_remembered(monkeypatch):
    fake = ThinkingChat("", cut=True)
    monkeypatch.setattr(llm, "chat", fake)
    assert llm.language_restriction("whales") is None, \
        "the braces inside a cut-off thought are not the answer"
    assert llm.language_restriction("whales") is None
    assert len(fake.calls) == 2, "a failure is asked again, never cached"


def test_an_answer_is_remembered_per_query_until_reset(monkeypatch):
    fake = ThinkingChat('{"phrase": "", "only": [], "lang": "fr"}')
    monkeypatch.setattr(llm, "chat", fake)
    first = llm.language_restriction("la baleine blanche")
    assert first == ([], "la baleine blanche") and first.lang == "fr"
    assert llm.language_restriction("la baleine blanche") is first
    assert len(fake.calls) == 1
    language_plan.reset()
    llm.language_restriction("la baleine blanche")
    assert len(fake.calls) == 2, "language_plan.reset clears it too"


@pytest.mark.parametrize("lang, want", [("DE", "de"), ("German", None), (7, None), (None, None)])
def test_a_language_that_is_not_a_code_is_none(monkeypatch, lang, want):
    import json
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps({"phrase": "", "only": [], "lang": lang}))
    assert llm.language_restriction(f"q {lang}").lang == want


def test_the_plan_carries_the_query_language(isolated_config, monkeypatch):
    c = collections_mod.default_collection()
    monkeypatch.setattr(language_plan.fts, "existing_groups", lambda collection=None: ["en"])
    monkeypatch.setattr(llm, "chat", ThinkingChat('{"phrase": "", "only": [], "lang": "it"}'))
    monkeypatch.setattr(llm, "translate_query", lambda q, langs: {"en": "the white whale"})
    plan = language_plan.plan("la balena bianca", [c], True)
    assert plan.status == "ok" and plan.query_lang == "it"
    assert "query_lang" not in plan.summary(), "the summary's shape is unchanged"
    assert language_plan.plan("lang:en whales", [c], True).query_lang is None, \
        "an explicit lang: asks no model, so nobody said"
