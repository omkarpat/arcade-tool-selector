"""Vetter rules with a stand-in judge (the Decisions API is the system boundary)."""

import anyio
import pytest
from mcp import types

from arcade_tool_selector.cards import build_cards
from arcade_tool_selector.catalog import Catalog
from arcade_tool_selector.policy import Evidence, PolicyConfig, ScopeConfig, Vetter


def tool(name: str, read_only: bool = False, destructive: bool = False) -> types.Tool:
    behavior = {"operations": [], "read_only": read_only, "destructive": destructive}
    return types.Tool(name=name, description="d", input_schema={"type": "object"}, _meta={"arcade": {"metadata": {"behavior": behavior}}})


CATALOG = Catalog(
    tools=[
        tool("GoogleCalendar_ListEvents", read_only=True),
        tool("GoogleCalendar_CreateEvent"),
        tool("Gmail_TrashEmail", destructive=True),
    ]
)
EVIDENCE = Evidence(purpose="Meeting scheduler.", user_messages=["Set up a sync with dana@acme.com"])


class Judge:
    def __init__(self, purpose=1.0, requested=1.0, reaches=0.0, discloses=0.0, fail=False):
        self.answers = {"within_purpose": purpose, "requested": requested, "reaches_people": reaches, "discloses_internal": discloses}
        self.fail = fail
        self.asked: list[list[str]] = []

    async def predicates(self, input, questions):
        self.asked.append(list(questions))
        if self.fail:
            raise TimeoutError
        return {name: self.answers[name] for name in questions}


def vet(judge, tool_name, scope=None):
    return anyio.run(Vetter(scope or ScopeConfig(), PolicyConfig(), build_cards(CATALOG), judge).vet, tool_name, {}, EVIDENCE)


def test_unknown_tool_denied_without_asking_the_judge():
    judge = Judge()
    assert vet(judge, "Nope_Tool").check == "unknown_tool"
    assert judge.asked == []


def test_only_writes_are_asked_whether_they_reach_people():
    judge = Judge()
    vet(judge, "GoogleCalendar_ListEvents")
    vet(judge, "Gmail_TrashEmail")
    vet(judge, "GoogleCalendar_CreateEvent")
    assert ["reaches_people" in asked for asked in judge.asked] == [False, False, True]


@pytest.mark.parametrize(
    ("tool_name", "reaches", "tier", "action"),
    [
        ("GoogleCalendar_ListEvents", 0.0, "read", "ask"),
        ("GoogleCalendar_CreateEvent", 0.1, "write", "ask"),
        ("GoogleCalendar_CreateEvent", 0.9, "send", "deny"),  # a write that reaches people carries send stakes
        ("Gmail_TrashEmail", 0.0, "destructive", "deny"),
    ],
)
def test_outside_purpose_follows_the_calls_tier(tool_name, reaches, tier, action):
    decision = vet(Judge(purpose=0.1, reaches=reaches), tool_name)
    assert (decision.tier, decision.action, decision.check) == (tier, action, "scope")


def test_unclear_request_asks_whatever_the_tier():
    decision = vet(Judge(purpose=0.95, requested=0.3, reaches=0.9), "GoogleCalendar_CreateEvent")
    assert (decision.action, decision.check) == ("ask", "scope")


def test_in_scope_allows_except_tiers_needing_approval():
    assert vet(Judge(reaches=0.9), "GoogleCalendar_CreateEvent").action == "allow"
    assert vet(Judge(), "Gmail_TrashEmail").action == "ask"


def test_disclosure_denies_only_calls_that_reach_people():
    assert vet(Judge(reaches=0.9, discloses=0.9), "GoogleCalendar_CreateEvent").check == "disclosure"
    assert vet(Judge(reaches=0.1, discloses=0.9), "GoogleCalendar_CreateEvent").action == "allow"  # stays with the user



def test_judge_down_denies_every_call():
    assert vet(Judge(fail=True), "GoogleCalendar_CreateEvent").check == "judge_unavailable"
    assert vet(Judge(fail=True), "GoogleCalendar_ListEvents").action == "deny"



def test_no_purpose_denies_every_call_without_asking_the_judge():
    judge = Judge()
    vetter = Vetter(ScopeConfig(), PolicyConfig(), build_cards(CATALOG), judge)
    decision = anyio.run(vetter.vet, "GoogleCalendar_ListEvents", {}, Evidence(purpose="  ", user_messages=["hi"]))
    assert (decision.action, decision.check) == ("deny", "no_purpose")
    assert judge.asked == []
