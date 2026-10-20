"""The ANSWER — `POST /api/ask/chat` (05 §5.4, app/ask/chat.py's prompt) and
`POST /v1/chat/completions` (07 §7.6): answered only from what the tools
returned, every claim cited by a returned record, nothing invented."""
import re

from tests_campaign.client import post

CITE = re.compile(r"\[(gutenberg:\d+)\]")


def _returned_uids(answer: dict) -> set[str]:
    """Every gutenberg uid the tools returned to the model (the turns)."""
    return set(re.findall(r"gutenberg:\d+", str(answer.get("turns"))))


def test_a_question_is_answered_from_the_tools_and_cites_what_they_returned():
    a = post("/api/ask/chat", {"question": "Who wrote Pride and Prejudice?", "collections": ["gutenberg"]})
    assert a.status == 200, a
    answer = a.json["answer"]
    assert "austen" in answer.lower(), answer
    assert a.json["turns"], "the answer was made with at least one tool call"
    cited = set(CITE.findall(answer))
    assert cited, f"the claim is cited by a record uid: {answer!r}"
    assert cited <= _returned_uids(a.json), f"cited but never returned: {cited - _returned_uids(a.json)}"


def test_a_question_the_collection_cannot_answer_invents_no_record():
    q = "What is the exact recipe for zorblax quantum pudding given in these books?"
    a = post("/api/ask/chat", {"question": q, "collections": ["gutenberg"]})
    assert a.status == 200, a
    cited = set(CITE.findall(a.json["answer"]))
    assert cited <= _returned_uids(a.json), f"invented citations: {cited - _returned_uids(a.json)}"


def test_the_openai_compatible_endpoint_answers_with_numbered_citations():
    """07 §7.6: retrieval then an answer; sources cited inline as [n]."""
    a = post("/v1/chat/completions", {"messages": [{"role": "user", "content": "Who is Captain Ahab?"}],
                                      "collections": ["gutenberg"], "stream": False})
    assert a.status == 200, a
    text = a.json["choices"][0]["message"]["content"]
    assert text and ("ahab" in text.lower() or "whale" in text.lower()), text
    nums = [int(n) for n in re.findall(r"\[(\d+)\]", text)]
    assert nums and all(n >= 1 for n in nums), f"numbered citations: {text!r}"
