"""Vetter rules with a stand-in judge (the Decisions API is the system boundary)."""

import anyio
import pytest
from mcp import types

from arcade_tool_selector.cards import build_cards
from arcade_tool_selector.catalog import Catalog
from arcade_tool_selector.policy import ArgRule, Evidence, PolicyConfig, ScopeConfig, Vetter


def tool(name: str, read_only: bool = False, destructive: bool = False, params: tuple[str, ...] = ()) -> types.Tool:
    behavior = {"operations": [], "read_only": read_only, "destructive": destructive}
    return types.Tool(
        name=name,
        description="d",
        input_schema={"type": "object", "properties": {p: {"type": "string"} for p in params}},
        _meta={"arcade": {"metadata": {"behavior": behavior}}},
    )


CATALOG = Catalog.from_tools(
    [
        tool("GoogleCalendar_ListEvents", read_only=True),
        tool("GoogleCalendar_CreateEvent", params=("attendee_emails",)),  # send tier
        tool("Linear_CreateIssue", params=("team",)),
        tool("Gmail_TrashEmail", destructive=True),
    ]
)
EVIDENCE = Evidence(project_prompt="Meeting scheduler.", user_messages=["Set up a sync with dana@acme.com"])


class Judge:
    def __init__(self, purpose: float = 1.0, requested: float = 1.0, fail: bool = False):
        self.answers = {"within_purpose": purpose, "requested": requested}
        self.fail = fail
        self.calls = 0

    async def predicates(self, input, questions):
        self.calls += 1
        if self.fail:
            raise TimeoutError
        return self.answers


def vet(judge, tool_name, arguments=None, **policy):
    vetter = Vetter(ScopeConfig(deny=policy.pop("deny", [])), PolicyConfig(**policy), build_cards(CATALOG), judge)
    return anyio.run(vetter.vet, tool_name, arguments or {}, EVIDENCE)


def test_hard_limits_deny_without_asking_the_judge():
    judge = Judge()
    assert vet(judge, "Gmail_TrashEmail", deny=["Gmail_TrashEmail"]).check == "scope_deny"
    rules = {"GoogleCalendar_CreateEvent": {"attendee_emails": ArgRule(domains=["acme.com"])}}
    assert vet(judge, "GoogleCalendar_CreateEvent", {"attendee_emails": ["pat@gmail.com"]}, args=rules).check == "arg_rule"
    assert vet(judge, "Linear_CreateIssue", {"team": "ENG"}, args={"Linear_CreateIssue": {"team": ArgRule(allow=["SUP"])}}).action == "deny"
    assert vet(judge, "Nope_Tool").check == "unknown_tool"
    assert judge.calls == 0


@pytest.mark.parametrize(
    ("tool_name", "action"),
    [("GoogleCalendar_ListEvents", "ask"), ("Linear_CreateIssue", "ask"), ("GoogleCalendar_CreateEvent", "deny"), ("Gmail_TrashEmail", "deny")],
)
def test_outside_purpose_follows_tier(tool_name, action):
    decision = vet(Judge(purpose=0.1, requested=0.99), tool_name)
    assert (decision.action, decision.check) == (action, "scope")


def test_unclear_request_asks_whatever_the_tier():
    decision = vet(Judge(purpose=0.95, requested=0.3), "GoogleCalendar_CreateEvent")
    assert (decision.action, decision.check) == ("ask", "scope")


def test_in_scope_allows_except_tiers_needing_approval():
    assert vet(Judge(), "GoogleCalendar_CreateEvent").action == "allow"
    assert vet(Judge(), "Gmail_TrashEmail").action == "ask"


def test_judge_down_fails_closed_except_read():
    assert vet(Judge(fail=True), "Linear_CreateIssue").action == "deny"
    assert vet(Judge(fail=True), "GoogleCalendar_ListEvents").action == "ask"


def test_config_problems_surface_rules_that_never_fire():
    vetter = Vetter(
        ScopeConfig(deny=["Gmail_Typo"]),
        PolicyConfig(args={"Linear_CreateIssue": {"teem": ArgRule(allow=["SUP"])}}),
        build_cards(CATALOG),
        Judge(),
    )
    assert vetter.config_problems() == ["scope.deny names unknown tool Gmail_Typo", "policy.args.Linear_CreateIssue names unknown parameter teem"]
