"""AGENT-HUB-053: a specialist's structured decision must stop the ReAct loop.

A valid ``waiting_decision`` pause previously travelled back into
``create_react_agent`` as an ordinary tool-result string, so the LLM got
another turn: it redispatched the specialist (3 redispatches each in runs
9f4fa67e and e48eca10) and/or synthesised an automatic answer before the
decision ever reached Telegram. Two layers now enforce the contract:

* Layer 1 — ``_make_agent_tool._call_agent`` refuses to dispatch while the run
  is already ``waiting_decision`` (covers parallel tool calls emitted in the
  same superstep), without transitioning the paused run back to ``routed``.
* Layer 2 — ``HubOrchestrator.invoke`` stops pulling the graph stream as soon
  as the run becomes ``waiting_decision``. The stream generator executes each
  superstep lazily (verified against the installed langgraph 1.2.5), so the
  break prevents the next agent/LLM step — no auto-answer, no re-dispatch.

Only ``provide_decision`` / ``provide_decision_reply`` resume the paused run,
re-dispatching the same persisted run with its existing ``request_id``. Hub
still validates replies against the specialist-declared option list — never a
fixed whitelist — and ``/approve`` + ``/reject`` still act only on a true
``waiting_approval`` run.

Every fixture here is inert: a scripted ``subprocess.Popen`` double and fake
streaming graphs shaped like ``create_react_agent``'s superstep events. No
real coding agent, real ATL process, or live approval path is ever started.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

from agent_hub.orchestrator import HubOrchestrator, _make_agent_tool
from agent_hub.registry import AgentSpec
from agent_hub.task_runs import (
    TASK_STATE_CANCELLED,
    TASK_STATE_FAILED,
    TASK_STATE_SUCCEEDED,
    TASK_STATE_WAITING_APPROVAL,
    TASK_STATE_WAITING_CLARIFICATION,
    TASK_STATE_WAITING_DECISION,
    active_task_run,
    get_task_run_store,
)

DECISION_PROMPT = "Approve deleting the unused backlog loader module?"
ATL_APPROVAL_OPTIONS = [
    {"name": "approve"},
    {"name": "request_changes", "needs_text": True},
    {"name": "ask_question", "needs_text": True},
    {"name": "cancel"},
]

DECISION_OUTPUT = {
    "status": "waiting_decision",
    "summary": "A decision is required.",
    "pending_decision": {
        "kind": "approval",
        "prompt": DECISION_PROMPT,
        "options": ATL_APPROVAL_OPTIONS,
    },
}

SUCCESS_OUTPUT = {
    "status": "success",
    "summary": "Deleted the unused backlog loader module.",
}

CLARIFICATION_OUTPUT = {
    "status": "needs_clarification",
    "summary": "Which module should I delete?",
}

EXPECTED_DECISION_REPLY = (
    f"[AI Tech Lead] Decision needed: {DECISION_PROMPT}\n"
    "1. approve\n"
    "2. request_changes (needs text)\n"
    "3. ask_question (needs text)\n"
    "4. cancel\n"
    "Reply with the option number or name. Add guidance after it if needed."
)

EXPECTED_CLARIFICATION_REPLY = "[AI Tech Lead] Clarification needed: Which module should I delete?"


def _atl_spec(tmp_path: Path) -> AgentSpec:
    """The AI Tech Lead shape used across the AGENT-HUB-053 contract tests."""
    return AgentSpec(
        id="ai-tech-lead",
        name="AI Tech Lead",
        purpose="Implements code changes",
        runtime={
            "mode": "subprocess",
            "entrypoint": "fake-agent",
            "working_directory": str(tmp_path),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
    )


def _scripted_popen(responses: list[dict]) -> type:
    """Build an inert ``subprocess.Popen`` double returning scripted results.

    Dispatches beyond the script return an explicit failure instead of
    hanging, so an unexpected extra specialist call fails the test loudly.
    """
    calls: list[dict] = []

    class _Popen:
        def __init__(self, cmd, cwd, stdout, stderr, text, **kwargs):
            input_path = Path(cmd[-3])
            output_path = Path(cmd[-1])
            payload = json.loads(input_path.read_text(encoding="utf-8"))
            calls.append(payload)
            Path(payload["progress_jsonl"]).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": payload["run_id"],
                        "request_id": payload["request_id"],
                        "sequence": len(calls),
                        "event_type": "phase",
                        "phase": "working",
                        "human_summary": "Specialist is working.",
                        "occurred_at": "2026-07-23T00:00:00+00:00",
                        "metadata": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            response = (
                responses[len(calls) - 1]
                if len(calls) <= len(responses)
                else {"status": "failed", "summary": "unexpected extra dispatch"}
            )
            output_path.write_text(json.dumps(response), encoding="utf-8")
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def communicate(self):
            return ("", "")

    _Popen.calls = calls
    return _Popen


def _orchestrator(monkeypatch, specialists: list[AgentSpec], graph: object | None = None):
    build_target = graph if graph is not None else object()
    monkeypatch.setattr(
        HubOrchestrator,
        "_build_graph",
        lambda self, registry=None, **kwargs: build_target,
    )
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: list(specialists))
    return HubOrchestrator()


def _tool_call_message(spec: AgentSpec) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": spec.id, "args": {"task": "Clean up dead code"}, "id": "call1"}],
    )


class _DecisionStoppingStream:
    """Fake ReAct stream mirroring create_react_agent's superstep ordering.

    Verified against the installed langgraph 1.2.5: each superstep emits its
    ``tasks``/``updates`` events, then a state ``values`` event, and the next
    superstep's nodes only run when the consumer pulls again. The tools step
    runs the real dispatch (scripted ``Popen``), so the run actually pauses on
    ``waiting_decision`` at the second ``values`` event. A second agent step
    follows — it must never execute.
    """

    def __init__(self, tool: object, spec: AgentSpec) -> None:
        self._tool = tool
        self._spec = spec
        self.reached_second_agent_step = False
        self.get_state_calls = 0

    def get_state(self, config):
        """Post-break snapshot like the real graph: the next node is pending
        but carries no interrupts — so the interrupt check after the stream
        loop must run without raising the unsupported-interrupt error."""
        self.get_state_calls += 1
        return SimpleNamespace(tasks=(SimpleNamespace(interrupts=()),))

    def stream(self, payload, config, stream_mode):
        assert stream_mode == ["tasks", "updates", "values"]
        human = payload["messages"][0]
        tool_call = _tool_call_message(self._spec)

        # Superstep 1: the LLM selects the specialist.
        yield ("tasks", {"name": "agent"})
        yield ("updates", {"agent": {"messages": [tool_call]}})
        yield ("values", {"messages": [human, tool_call]})

        # Superstep 2: the tools node dispatches; the specialist reports a
        # structured decision and the run is persisted as waiting_decision.
        yield ("tasks", {"name": "tools"})
        decision_text = self._tool.invoke({"task": "Clean up dead code"})
        tool_message = ToolMessage(content=decision_text, tool_call_id="call1")
        yield ("updates", {"tools": {"messages": [tool_message]}})
        yield ("values", {"messages": [human, tool_call, tool_message]})

        # Superstep 3: the LLM's next turn (auto-answer / redispatch).
        self.reached_second_agent_step = True
        auto_answer = AIMessage(content="The model approved the change itself.")
        yield ("tasks", {"name": "agent"})
        yield ("updates", {"agent": {"messages": [auto_answer]}})
        yield ("values", {"messages": [human, tool_call, tool_message, auto_answer]})


class _DecisionRelayStream:
    """Fake stream whose single ``values`` event already carries a trailing
    model paraphrase after the decision ToolMessage — the shape
    ``_relay_specialist_terminal_message`` exists for."""

    def __init__(self, tool: object, spec: AgentSpec) -> None:
        self._tool = tool
        self._spec = spec

    def stream(self, payload, config, stream_mode):
        assert stream_mode == ["tasks", "updates", "values"]
        human = payload["messages"][0]
        tool_call = _tool_call_message(self._spec)
        yield ("tasks", {"name": "agent"})
        yield ("updates", {"agent": {"messages": [tool_call]}})
        yield ("tasks", {"name": "tools"})
        decision_text = self._tool.invoke({"task": "Clean up dead code"})
        tool_message = ToolMessage(content=decision_text, tool_call_id="call1")
        paraphrase = AIMessage(content="The AI Tech Lead wants an approval decision.")
        yield ("updates", {"tools": {"messages": [tool_message]}})
        yield ("updates", {"agent": {"messages": [paraphrase]}})
        yield ("values", {"messages": [human, tool_call, tool_message, paraphrase]})


class _ClarificationKeepsRunningStream:
    """Fake stream proving Layer 2 only stops on ``waiting_decision``: a
    ``waiting_clarification`` pause keeps the existing behaviour where the LLM
    gets its normal final turn and the relay suppresses its paraphrase."""

    def __init__(self, tool: object, spec: AgentSpec) -> None:
        self._tool = tool
        self._spec = spec
        self.reached_final_agent_step = False

    def stream(self, payload, config, stream_mode):
        assert stream_mode == ["tasks", "updates", "values"]
        human = payload["messages"][0]
        tool_call = _tool_call_message(self._spec)
        yield ("tasks", {"name": "agent"})
        yield ("updates", {"agent": {"messages": [tool_call]}})
        yield ("values", {"messages": [human, tool_call]})

        yield ("tasks", {"name": "tools"})
        clarification_text = self._tool.invoke({"task": "Clean up dead code"})
        tool_message = ToolMessage(content=clarification_text, tool_call_id="call1")
        yield ("updates", {"tools": {"messages": [tool_message]}})
        yield ("values", {"messages": [human, tool_call, tool_message]})

        self.reached_final_agent_step = True
        paraphrase = AIMessage(content="The specialist is asking a clarifying question.")
        yield ("tasks", {"name": "agent"})
        yield ("updates", {"agent": {"messages": [paraphrase]}})
        yield ("values", {"messages": [human, tool_call, tool_message, paraphrase]})


# --------------------------------------------------------------------------
# 1 + 3: the stream loop stops before a second agent step, and the decision
# (prompt + options) is surfaced verbatim as the operator reply.
# --------------------------------------------------------------------------


def test_waiting_decision_stops_stream_before_second_agent_call(monkeypatch, tmp_path):
    fake_popen = _scripted_popen([DECISION_OUTPUT])
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", fake_popen)
    spec = _atl_spec(tmp_path)
    graph = _DecisionStoppingStream(_make_agent_tool(spec), spec)
    orchestrator = _orchestrator(monkeypatch, specialists=[], graph=graph)

    reply = orchestrator.invoke("Clean up dead code")

    # Exactly one specialist dispatch — no automatic redispatch.
    assert len(fake_popen.calls) == 1
    # The LLM's next turn never executed (lazy generator halted by the break).
    assert graph.reached_second_agent_step is False
    # The post-loop interrupt check ran on the paused snapshot and did NOT
    # raise "Unsupported Hub graph interrupt".
    assert graph.get_state_calls >= 1
    # The decision text — prompt and every specialist-declared option — is the
    # verbatim operator reply, not an auto-answer.
    assert reply == EXPECTED_DECISION_REPLY
    assert "The model approved the change itself." not in reply

    runs = get_task_run_store().list_runs(session_id=orchestrator.session_id)
    assert len(runs) == 1
    assert runs[0].state == TASK_STATE_WAITING_DECISION
    assert runs[0].final_response == EXPECTED_DECISION_REPLY
    assert runs[0].context["specialist_pending_decision"]["options"] == ATL_APPROVAL_OPTIONS


def test_decision_relay_path_still_surfaces_verbatim_reply(monkeypatch, tmp_path):
    """When a trailing model message exists, the relay returns the
    ``"] Decision needed:"`` ToolMessage content verbatim (paraphrase
    suppressed) and the paused run records it as ``final_response``."""
    fake_popen = _scripted_popen([DECISION_OUTPUT])
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", fake_popen)
    spec = _atl_spec(tmp_path)
    graph = _DecisionRelayStream(_make_agent_tool(spec), spec)
    orchestrator = _orchestrator(monkeypatch, specialists=[], graph=graph)

    reply = orchestrator.invoke("Clean up dead code")

    assert len(fake_popen.calls) == 1
    assert reply == EXPECTED_DECISION_REPLY
    assert "wants an approval decision" not in reply

    runs = get_task_run_store().list_runs(session_id=orchestrator.session_id)
    assert len(runs) == 1
    assert runs[0].state == TASK_STATE_WAITING_DECISION
    assert runs[0].final_response == EXPECTED_DECISION_REPLY


# --------------------------------------------------------------------------
# 2: Layer 1 — a second dispatch inside the same run is refused.
# --------------------------------------------------------------------------


def test_call_agent_refuses_second_dispatch_while_decision_pending(monkeypatch, tmp_path):
    fake_popen = _scripted_popen([DECISION_OUTPUT])
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", fake_popen)
    spec = _atl_spec(tmp_path)
    orchestrator = _orchestrator(monkeypatch, specialists=[spec])
    tool = _make_agent_tool(spec)
    run = get_task_run_store().create_run(
        session_id=orchestrator.session_id, user_message="Clean up dead code"
    )

    with active_task_run(run.id):
        first = tool.invoke({"task": "Clean up dead code"})
        second = tool.invoke({"task": "Clean up dead code again"})

    assert len(fake_popen.calls) == 1
    assert first == EXPECTED_DECISION_REPLY
    assert "already pending" in second
    assert "no new specialist call was made" in second
    # The refusal must not clobber the pause back to routed.
    current = get_task_run_store().get_run(run.id)
    assert current is not None and current.state == TASK_STATE_WAITING_DECISION

    # Parallel tool call emitted in the same superstep: a run already paused
    # on waiting_decision refuses before dispatching at all.
    paused = get_task_run_store().create_run(
        session_id=orchestrator.session_id, user_message="Dispatch anyway"
    )
    get_task_run_store().transition(
        paused.id,
        TASK_STATE_WAITING_DECISION,
        selected_agent_id=spec.id,
        context_updates={"specialist_pending_decision": DECISION_OUTPUT["pending_decision"]},
    )
    with active_task_run(paused.id):
        refused = tool.invoke({"task": "Dispatch anyway"})

    assert len(fake_popen.calls) == 1
    assert "already pending" in refused
    still_paused = get_task_run_store().get_run(paused.id)
    assert still_paused is not None and still_paused.state == TASK_STATE_WAITING_DECISION


# --------------------------------------------------------------------------
# 4: an authorised operator reply resumes the same persisted run.
# --------------------------------------------------------------------------


def test_operator_reply_resumes_same_run_with_same_request_id(monkeypatch, tmp_path):
    fake_popen = _scripted_popen([DECISION_OUTPUT, SUCCESS_OUTPUT])
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", fake_popen)
    spec = _atl_spec(tmp_path)
    orchestrator = _orchestrator(monkeypatch, specialists=[spec])
    tool = _make_agent_tool(spec)
    run = get_task_run_store().create_run(
        session_id=orchestrator.session_id, user_message="Clean up dead code"
    )

    with active_task_run(run.id):
        tool.invoke({"task": "Clean up dead code"})

    paused = get_task_run_store().get_run(run.id)
    assert paused is not None and paused.state == TASK_STATE_WAITING_DECISION


    assert len(fake_popen.calls) == 1
    first_request_id = fake_popen.calls[0]["request_id"]
    assert fake_popen.calls[0]["run_id"] == run.id

    # An option the specialist never declared is rejected without dispatching
    # — Hub validates against the specialist's own list, never a whitelist.
    rejected = orchestrator.provide_decision_reply("bogus_option")
    assert "not a valid option" in rejected
    assert len(fake_popen.calls) == 1
    still_paused = get_task_run_store().get_run(run.id)
    assert still_paused is not None and still_paused.state == TASK_STATE_WAITING_DECISION

    # The authorised reply resumes the SAME persisted run and request_id.
    reply = orchestrator.provide_decision_reply("approve")

    assert "Deleted the unused backlog loader module." in reply
    assert len(fake_popen.calls) == 2
    assert fake_popen.calls[1]["request_id"] == first_request_id
    assert fake_popen.calls[1]["run_id"] == run.id
    assert fake_popen.calls[1]["decision"] == {
        "option": "approve",
        "text": "",
        "actor": "human",
    }
    final = get_task_run_store().get_run(run.id)
    assert final is not None and final.state == TASK_STATE_SUCCEEDED
    runs = get_task_run_store().list_runs(session_id=orchestrator.session_id)
    assert [candidate.id for candidate in runs] == [run.id]


# --------------------------------------------------------------------------
# 5: existing waiting_approval behaviour is unchanged.
# --------------------------------------------------------------------------


def test_approve_and_reject_still_act_on_true_waiting_approval_runs(monkeypatch, tmp_path):
    fake_popen = _scripted_popen([SUCCESS_OUTPUT])
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", fake_popen)
    spec = _atl_spec(tmp_path)
    orchestrator = _orchestrator(monkeypatch, specialists=[spec])
    store = get_task_run_store()

    approved = store.create_run(
        session_id=orchestrator.session_id, user_message="Delete the generated files"
    )
    store.transition(
        approved.id,
        TASK_STATE_WAITING_APPROVAL,
        selected_agent_id=spec.id,
        dispatched_task="Delete the generated files",
        approval_token="approve-123",
        context_updates={"agent_request_id": "req-1"},
    )

    approve_reply = orchestrator.approve_pending()

    assert "Deleted the unused backlog loader module." in approve_reply
    assert len(fake_popen.calls) == 1
    assert fake_popen.calls[0]["human_approved"] is True
    assert fake_popen.calls[0]["request_id"] == "req-1"
    after_approve = store.get_run(approved.id)
    assert after_approve is not None and after_approve.state == TASK_STATE_SUCCEEDED

    rejected = store.create_run(
        session_id=orchestrator.session_id, user_message="Delete more generated files"
    )
    store.transition(
        rejected.id,
        TASK_STATE_WAITING_APPROVAL,
        selected_agent_id=spec.id,
        dispatched_task="Delete more generated files",
        approval_token="approve-456",
        context_updates={"agent_request_id": "req-2"},
    )

    reject_reply = orchestrator.reject_pending("not safe")

    assert reject_reply == "[AI Tech Lead] Request rejected: not safe"
    # Rejecting never dispatches the specialist again.
    assert len(fake_popen.calls) == 1
    after_reject = store.get_run(rejected.id)
    assert after_reject is not None and after_reject.state == TASK_STATE_FAILED


# --------------------------------------------------------------------------
# 6: cancellation still works for a run paused on waiting_decision.
# --------------------------------------------------------------------------


def test_stop_cancels_a_run_paused_on_waiting_decision(monkeypatch, tmp_path):
    spec = _atl_spec(tmp_path)
    orchestrator = _orchestrator(monkeypatch, specialists=[spec])
    store = get_task_run_store()
    run = store.create_run(session_id=orchestrator.session_id, user_message="Clean up dead code")
    store.transition(
        run.id,
        TASK_STATE_WAITING_DECISION,
        selected_agent_id=spec.id,
        context_updates={"specialist_pending_decision": DECISION_OUTPUT["pending_decision"]},
    )

    confirmation = orchestrator.stop_current_task("operator changed their mind")

    assert "cancelled" in confirmation.lower()
    current = store.get_run(run.id)
    assert current is not None and current.state == TASK_STATE_CANCELLED
    assert current.cancellation_reason == "operator changed their mind"
    # A cancelled run is no longer resumable as a pending decision.
    assert orchestrator.pending_run() is None
    assert "No task is currently active." in orchestrator.stop_current_task()


# --------------------------------------------------------------------------
# 7: needs_clarification / waiting_clarification is unaffected.
# --------------------------------------------------------------------------


def test_decision_guard_does_not_block_clarification_dispatch(monkeypatch, tmp_path):
    """Layer 1 is narrow: a run paused on waiting_clarification still
    dispatches exactly as before this change."""
    fake_popen = _scripted_popen([CLARIFICATION_OUTPUT, CLARIFICATION_OUTPUT])
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", fake_popen)
    spec = _atl_spec(tmp_path)
    orchestrator = _orchestrator(monkeypatch, specialists=[spec])
    tool = _make_agent_tool(spec)
    run = get_task_run_store().create_run(
        session_id=orchestrator.session_id, user_message="Clean up dead code"
    )

    with active_task_run(run.id):
        first = tool.invoke({"task": "Clean up dead code"})
        second = tool.invoke({"task": "Clean up dead code"})

    assert first == EXPECTED_CLARIFICATION_REPLY
    assert second == EXPECTED_CLARIFICATION_REPLY
    assert len(fake_popen.calls) == 2
    current = get_task_run_store().get_run(run.id)
    assert current is not None and current.state == TASK_STATE_WAITING_CLARIFICATION


def test_stream_loop_is_not_stopped_by_a_clarification_pause(monkeypatch, tmp_path):
    """Layer 2 keys on waiting_decision only: a clarification pause keeps the
    existing behaviour — the LLM's final turn runs and the relay surfaces the
    specialist's clarification text verbatim."""
    fake_popen = _scripted_popen([CLARIFICATION_OUTPUT])
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", fake_popen)
    spec = _atl_spec(tmp_path)
    graph = _ClarificationKeepsRunningStream(_make_agent_tool(spec), spec)
    orchestrator = _orchestrator(monkeypatch, specialists=[], graph=graph)

    reply = orchestrator.invoke("Clean up dead code")

    assert len(fake_popen.calls) == 1
    # The loop was NOT broken early: the normal final agent step ran.
    assert graph.reached_final_agent_step is True
    assert reply == EXPECTED_CLARIFICATION_REPLY
    assert "clarifying question" not in reply

    runs = get_task_run_store().list_runs(session_id=orchestrator.session_id)
    assert len(runs) == 1
    assert runs[0].state == TASK_STATE_WAITING_CLARIFICATION
    assert runs[0].final_response == EXPECTED_CLARIFICATION_REPLY


def test_concurrent_direct_dispatch_cannot_bypass_pending_decision(monkeypatch, tmp_path):
    """Two overlapping tool calls share a run; the second must not re-dispatch.

    The first call is held inside an inert Popen double while the second starts.
    This reproduced in_progress -> routed before per-run serialization existed.
    """
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    fake_popen = _scripted_popen([DECISION_OUTPUT, DECISION_OUTPUT])
    original_init = fake_popen.__init__
    first_in_subprocess = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def blocked_init(self, *args, **kwargs):
        if not first_in_subprocess.is_set():
            first_in_subprocess.set()
            assert release_first.wait(timeout=5)
        return original_init(self, *args, **kwargs)

    fake_popen.__init__ = blocked_init
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", fake_popen)
    spec = _atl_spec(tmp_path)
    orchestrator = _orchestrator(monkeypatch, specialists=[spec])
    run = get_task_run_store().create_run(
        session_id=orchestrator.session_id, user_message="Parallel tools"
    )
    tool = _make_agent_tool(spec)

    def call():
        with active_task_run(run.id):
            return tool.invoke({"task": "Clean up dead code"})

    def second_call():
        second_started.set()
        return call()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(call)
        assert first_in_subprocess.wait(timeout=5)
        second = pool.submit(second_call)
        try:
            assert second_started.wait(timeout=5)
            time.sleep(0.1)  # Let the sibling contend while the first is blocked.
            assert not second.done()
        finally:
            release_first.set()
        first_reply = first.result(timeout=5)
        second_reply = second.result(timeout=5)

    assert first_reply == EXPECTED_DECISION_REPLY
    assert "already pending" in second_reply
    assert len(fake_popen.calls) == 1
    paused = get_task_run_store().get_run(run.id)
    assert paused is not None and paused.state == TASK_STATE_WAITING_DECISION


def test_real_langgraph_pauses_before_second_model_turn(monkeypatch, tmp_path):
    """Real create_react_agent supersteps, inert LLM and specialist process."""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.prebuilt import create_react_agent

    class BoundFake(FakeMessagesListChatModel):
        generated: int = 0

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.generated += 1
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    fake_popen = _scripted_popen([DECISION_OUTPUT, SUCCESS_OUTPUT])
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", fake_popen)
    spec = _atl_spec(tmp_path)
    model = BoundFake(
        responses=[
            _tool_call_message(spec),
            AIMessage(content="I approved the request without asking the operator."),
        ]
    )
    graph = create_react_agent(
        model=model,
        tools=[_make_agent_tool(spec)],
        checkpointer=MemorySaver(),
    )
    orchestrator = _orchestrator(monkeypatch, specialists=[spec], graph=graph)
    reply = orchestrator.invoke("Clean up dead code")

    assert reply == EXPECTED_DECISION_REPLY
    assert model.generated == 1
    assert len(fake_popen.calls) == 1
    run = get_task_run_store().list_runs(session_id=orchestrator.session_id)[0]
    assert run.state == TASK_STATE_WAITING_DECISION
    request_id = fake_popen.calls[0]["request_id"]
    assert "not a valid option" in orchestrator.provide_decision_reply("bogus_option")
    assert len(fake_popen.calls) == 1
    approved = orchestrator.provide_decision_reply("approve")
    assert "Deleted the unused backlog loader module." in approved
    assert model.generated == 1
    assert len(fake_popen.calls) == 2
    assert fake_popen.calls[1]["request_id"] == request_id
    assert fake_popen.calls[1]["run_id"] == run.id
    final = get_task_run_store().get_run(run.id)
    assert final is not None and final.state == TASK_STATE_SUCCEEDED



def test_missing_run_record_fails_closed_before_dispatch(monkeypatch, tmp_path):
    """A run id without a persisted record cannot dispatch a specialist."""
    import pytest

    fake_popen = _scripted_popen([SUCCESS_OUTPUT])
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", fake_popen)
    tool = _make_agent_tool(_atl_spec(tmp_path))
    with active_task_run("nonexistent-run-id"):
        with pytest.raises(KeyError, match="Unknown task run"):
            tool.invoke({"task": "Do not dispatch"})
    assert len(fake_popen.calls) == 0
