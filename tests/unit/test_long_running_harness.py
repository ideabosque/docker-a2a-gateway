"""Offline harness checks for the long-running A2A task lifecycle.

Covers the shared helpers added to ``tests/a2a_test_utils.py`` for
``docs/LONG_RUNNING_TASKS_TEST_PLAN.md``:

  * explicit blocking / immediate (``configuration.returnImmediately``) modes
  * envelope validation (direct Message reply or A2A Task)
  * bounded ``tasks/get`` polling with a monotonic deadline
  * a real SSE parser (one HTTP chunk is never one event)
  * redaction and content-free metrics

Everything runs offline: fake clocks, scripted HTTP/SSE transports, no
network and no sleeping.
"""
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import a2a_test_utils as utils  # noqa: E402  (path bootstrap above)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
class _FakeClock:
    """Monotonic clock whose sleep() advances time deterministically."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _rpc(result):
    return {"jsonrpc": "2.0", "id": "1", "result": result}


def _response(payload, status_code=200):
    response = Mock(status_code=status_code)
    response.json.return_value = payload
    response.text = json.dumps(payload)
    return response


def _task(state="TASK_STATE_WORKING", task_id="task-1", context_id="ctx-1",
          artifacts=None):
    result = {"id": task_id, "contextId": context_id,
              "status": {"state": state}}
    if artifacts is not None:
        result["artifacts"] = artifacts
    return result


# ---------------------------------------------------------------------------
# 1. Return-mode construction
# ---------------------------------------------------------------------------
def test_message_send_params_default_output_is_unchanged():
    params = utils.message_send_params("hi", "agent", "task-1")
    assert json.dumps(params, sort_keys=True) == json.dumps({
        "message": {"role": "ROLE_USER", "parts": [{"text": "hi"}]},
        "metadata": {
            "operation": "task_execution",
            "agent_uuid": "agent",
            "stream": True,
            "task_data": {"task_id": "task-1", "task_type": "a2a_test"},
        },
    }, sort_keys=True)
    assert "configuration" not in params


def test_message_send_params_blocking_mode_adds_no_configuration():
    params = utils.message_send_params("hi", "agent", "task-1",
                                       return_mode="blocking")
    assert "configuration" not in params


@pytest.mark.parametrize("mode", ["immediate", "async", "IMMEDIATE"])
def test_message_send_params_immediate_mode_sets_return_immediately(mode):
    params = utils.message_send_params("hi", "agent", "task-1",
                                       return_mode=mode)
    assert params["configuration"] == {"returnImmediately": True}


def test_message_send_params_rejects_unknown_return_mode():
    with pytest.raises(ValueError):
        utils.message_send_params("hi", "agent", "task-1", return_mode="later")


def test_message_send_params_keeps_existing_options():
    params = utils.message_send_params(
        "hi", "agent", "task-1", task_type="live", stream=False,
        system_prompt="sys", thread_uuid="thread-1", show_reasoning=False)
    metadata = params["metadata"]
    assert metadata["stream"] is False
    assert metadata["system_prompt"] == "sys"
    assert metadata["thread_uuid"] == "thread-1"
    assert metadata["show_reasoning"] is False
    assert metadata["task_data"] == {"task_id": "task-1",
                                     "task_type": "live"}
    assert "configuration" not in params


def test_normalize_return_mode_canonicalizes():
    assert utils.normalize_return_mode("blocking") == "blocking"
    assert utils.normalize_return_mode("async") == "immediate"
    assert utils.normalize_return_mode(None) == "blocking"


# ---------------------------------------------------------------------------
# 2. Envelope validation and id extraction
# ---------------------------------------------------------------------------
def test_parse_task_envelope_accepts_a_task():
    envelope = utils.parse_task_envelope(_rpc(_task(
        state="TASK_STATE_WORKING",
        artifacts=[{"artifactId": "a1", "parts": [{"text": "partial"}]}])))
    assert envelope.kind == "task"
    assert envelope.task_id == "task-1"
    assert envelope.context_id == "ctx-1"
    assert envelope.state == "working"
    assert envelope.text == "partial"
    assert not envelope.is_terminal


def test_parse_task_envelope_accepts_a_direct_message_reply():
    body = _rpc({"kind": "message", "role": "ROLE_AGENT", "messageId": "m-1",
                 "taskId": "task-9", "contextId": "ctx-3",
                 "parts": [{"text": "direct answer"}]})
    envelope = utils.parse_task_envelope(body)
    assert envelope.kind == "message"
    assert envelope.task_id == "task-9"
    assert envelope.context_id == "ctx-3"
    assert envelope.state == ""
    assert envelope.text == "direct answer"


def test_parse_task_envelope_rejects_a_missing_task_id():
    body = _rpc({"kind": "message", "parts": [{"text": "x"}]})
    with pytest.raises(utils.A2AEnvelopeError):
        utils.parse_task_envelope(body)


def test_parse_task_envelope_can_allow_a_missing_task_id():
    body = _rpc({"kind": "message", "parts": [{"text": "x"}]})
    envelope = utils.parse_task_envelope(body, require_task_id=False)
    assert envelope.task_id == ""
    assert envelope.text == "x"


def test_parse_task_envelope_rejects_a_jsonrpc_error_body():
    body = {"jsonrpc": "2.0", "id": "1",
            "error": {"code": -32001, "message": "agent not found"}}
    with pytest.raises(utils.A2AEnvelopeError) as excinfo:
        utils.parse_task_envelope(body)
    assert "agent not found" in str(excinfo.value)


def test_parse_task_envelope_rejects_a_graphql_error_body():
    body = {"data": None, "errors": [{"message": "unauthorized"}]}
    with pytest.raises(utils.A2AEnvelopeError) as excinfo:
        utils.parse_task_envelope(body)
    assert "unauthorized" in str(excinfo.value)


@pytest.mark.parametrize("body", [
    {"jsonrpc": "2.0", "id": "1"},
    [1, 2, 3],
    "not json",
    {"jsonrpc": "2.0", "id": "1", "result": "not an object"},
])
def test_parse_task_envelope_rejects_malformed_bodies(body):
    with pytest.raises(utils.A2AEnvelopeError):
        utils.parse_task_envelope(body)


def test_parse_task_envelope_requires_a_nonterminal_task():
    body = _rpc(_task(state="TASK_STATE_COMPLETED"))
    with pytest.raises(utils.A2AEnvelopeError):
        utils.parse_task_envelope(body, require_non_terminal=True)
    envelope = utils.parse_task_envelope(body)
    assert envelope.state == "completed"
    assert envelope.is_terminal


def test_parse_task_envelope_rejects_a_message_when_a_task_is_required():
    body = _rpc({"kind": "message", "taskId": "t", "parts": [{"text": "x"}]})
    with pytest.raises(utils.A2AEnvelopeError):
        utils.parse_task_envelope(body, require_non_terminal=True)


def test_decode_jsonrpc_response_raises_on_http_errors():
    response = _response({"error": {"message": "boom"}}, status_code=502)
    with pytest.raises(utils.A2AEnvelopeError) as excinfo:
        utils.decode_jsonrpc_response(response)
    assert "502" in str(excinfo.value)


def test_decode_jsonrpc_response_returns_the_body():
    body = _rpc(_task())
    response = Mock(status_code=200)
    response.json.return_value = body
    assert utils.decode_jsonrpc_response(response) == body


def test_extract_task_output_prefers_artifacts_over_the_status_message():
    result = {
        "id": "t",
        "status": {"state": "TASK_STATE_COMPLETED",
                   "message": {"parts": [{"text": "answer"}]}},
        "artifacts": [{"parts": [{"text": "ans"}]}],
    }
    assert utils.extract_task_output(result) == "ans"


def test_extract_task_output_falls_back_to_the_status_message():
    result = {"id": "t", "status": {"state": "TASK_STATE_COMPLETED",
                                     "message": {"parts": [{"text": "answer"}]}}}
    assert utils.extract_task_output(result) == "answer"


def test_normalize_task_state_and_terminal_detection():
    assert utils.normalize_task_state("TASK_STATE_COMPLETED") == "completed"
    assert utils.normalize_task_state("TASK_STATE_INPUT_REQUIRED") == \
        "input_required"
    assert utils.normalize_task_state("CANCELLED") == "canceled"
    assert utils.normalize_task_state({"state": "TASK_STATE_FAILED"}) == \
        "failed"
    assert utils.is_terminal_state("TASK_STATE_CANCELED")
    assert not utils.is_terminal_state("TASK_STATE_WORKING")


# ---------------------------------------------------------------------------
# 3. Bounded tasks/get polling
# ---------------------------------------------------------------------------
def test_poll_task_returns_the_terminal_state_and_output(monkeypatch):
    clock = _FakeClock()
    responses = [
        _response(_rpc(_task(state="TASK_STATE_WORKING"))),
        _response(_rpc(_task(state="TASK_STATE_COMPLETED", artifacts=[
            {"artifactId": "a1", "parts": [{"text": "final answer"}]}]))),
    ]
    calls = []

    def fake_send(*args, **kwargs):
        calls.append((args, kwargs))
        return responses[len(calls) - 1]

    monkeypatch.setattr(utils, "send_a2a", fake_send)
    result = utils.poll_task("http://gw", "tok", "a2a", "default", "task-1",
                             agent_uuid="agent", timeout=30, interval=0.5,
                             request_timeout=7, sleep=clock.sleep, clock=clock)

    assert result.state == "completed"
    assert result.output == "final answer"
    assert result.ok
    assert not result.timed_out
    assert result.polls == 2
    assert result.context_id == "ctx-1"
    assert calls[0][0][4] == "tasks/get"
    assert calls[0][1]["timeout"] == 7
    assert calls[0][0][5] == {"id": "task-1",
                              "metadata": {"agent_uuid": "agent"}}
    assert clock.sleeps == [0.5]


def test_poll_task_expires_at_the_observation_deadline(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(
        utils, "send_a2a",
        Mock(return_value=_response(_rpc(_task(state="TASK_STATE_WORKING")))))
    result = utils.poll_task("http://gw", "tok", "a2a", "default", "task-1",
                             timeout=2.0, interval=0.5, sleep=clock.sleep,
                             clock=clock)
    assert result.timed_out
    assert not result.ok
    assert result.state == "working"
    assert result.error == ""
    assert clock.now <= 2.0
    assert result.polls <= 5


def test_poll_task_reports_an_http_error(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(
        utils, "send_a2a",
        Mock(return_value=_response({"error": {"message": "Task not found"}},
                                    status_code=404)))
    result = utils.poll_task("http://gw", "tok", "a2a", "default", "task-1",
                             timeout=5, interval=0.5, sleep=clock.sleep,
                             clock=clock)
    assert "404" in result.error
    assert not result.timed_out
    assert not result.ok
    assert result.polls == 1


def test_poll_task_retries_a_connection_loss_until_the_deadline(monkeypatch):
    clock = _FakeClock()

    def boom(*args, **kwargs):
        raise requests.exceptions.ConnectionError("connection reset")

    monkeypatch.setattr(utils, "send_a2a", boom)
    result = utils.poll_task("http://gw", "tok", "a2a", "default", "task-1",
                             timeout=1.0, interval=0.5, sleep=clock.sleep,
                             clock=clock)
    assert result.timed_out
    assert "ConnectionError" in result.error
    assert not result.ok


def test_poll_task_retries_a_server_error_until_the_deadline(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(
        utils, "send_a2a",
        Mock(return_value=_response({"error": "boom"}, status_code=500)))
    result = utils.poll_task("http://gw", "tok", "a2a", "default", "task-1",
                             timeout=1.0, interval=0.5, sleep=clock.sleep,
                             clock=clock)
    assert result.timed_out
    assert "500" in result.error


def test_poll_task_raises_on_a_jsonrpc_error_body(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(
        utils, "send_a2a",
        Mock(return_value=_response({"jsonrpc": "2.0", "id": "1",
                                      "error": {"code": -32001,
                                                "message": "Task not found"}})))
    with pytest.raises(utils.A2AEnvelopeError):
        utils.poll_task("http://gw", "tok", "a2a", "default", "task-1",
                        timeout=5, interval=0.5, sleep=clock.sleep,
                        clock=clock)


def test_poll_task_reads_the_persisted_output_not_the_stream(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(
        utils, "send_a2a",
        Mock(return_value=_response(_rpc(_task(
            state="TASK_STATE_COMPLETED",
            artifacts=[{"parts": [{"text": "persisted"}]}])))))
    result = utils.poll_task("http://gw", "tok", "a2a", "default", "task-1",
                             timeout=5, interval=0.5, sleep=clock.sleep,
                             clock=clock)
    assert result.output == "persisted"
    assert result.task["id"] == "task-1"


# ---------------------------------------------------------------------------
# 4. SSE parsing
# ---------------------------------------------------------------------------
def test_iter_sse_events_parses_one_event():
    events = list(utils.iter_sse_events(
        [b'data: {"type": "task_status", "state": "working"}\n\n']))
    assert len(events) == 1
    assert events[0].type == "task_status"
    assert events[0].payload()["state"] == "working"


def test_iter_sse_events_does_not_assume_one_chunk_per_event():
    events = list(utils.iter_sse_events([
        b'data: {"type": "task_artifact"}\n\ndata: {"type": "task_status"}\n\n']))
    assert [event.type for event in events] == ["task_artifact", "task_status"]


def test_iter_sse_events_splits_an_event_across_chunks():
    events = list(utils.iter_sse_events([
        b'data: {"type": "task_', b'artifact"}\n', b'\n']))
    assert len(events) == 1
    assert events[0].type == "task_artifact"


def test_iter_sse_events_joins_multiline_data():
    events = list(utils.iter_sse_events([b'data: line one\ndata: line two\n\n']))
    assert events[0].data == "line one\nline two"


def test_iter_sse_events_reads_the_optional_id_field():
    events = list(utils.iter_sse_events([b'id: 42\ndata: {}\n\n']))
    assert events[0].id == "42"
    assert utils.last_event_id(events) == "42"


def test_iter_sse_events_works_without_an_id_field():
    events = list(utils.iter_sse_events([b'data: {"type": "task_status"}\n\n']))
    assert events[0].id == ""
    assert utils.last_event_id(events) == ""


def test_poll_task_rejects_a_non_positive_interval(monkeypatch):
    monkeypatch.setattr(utils, "send_a2a", Mock())
    with pytest.raises(ValueError):
        utils.poll_task("http://gw", "tok", "a2a", "default", "task-1",
                        interval=0)


def test_iter_sse_events_reads_the_event_field():
    events = list(utils.iter_sse_events(
        [b'event: task_artifact\ndata: {}\n\n']))
    assert events[0].event == "task_artifact"


def test_iter_sse_events_ignores_comments():
    events = list(utils.iter_sse_events(
        [b': keep-alive\n\ndata: {"type": "task_status"}\n\n']))
    assert len(events) == 1
    assert events[0].type == "task_status"


def test_iter_sse_events_flushes_the_pending_event_at_eof():
    events = list(utils.iter_sse_events([b'data: {"type": "task_status"}']))
    assert len(events) == 1
    assert events[0].type == "task_status"


def test_iter_sse_events_handles_fragmented_utf8():
    payload = json.dumps({"type": "task_artifact",
                          "artifact": {"text": "héllo → wörld"}},
                         ensure_ascii=False).encode("utf-8")
    frame = b"data: " + payload + b"\n\n"
    cut = frame.index("é".encode("utf-8")) + 1  # split inside the character
    chunks = [frame[:cut], frame[cut:cut + 3], frame[cut + 3:]]
    events = list(utils.iter_sse_events(chunks))
    assert len(events) == 1
    assert events[0].payload()["artifact"]["text"] == "héllo → wörld"


def test_iter_sse_events_handles_crlf_line_endings():
    events = list(utils.iter_sse_events(
        [b'data: {"type": "task_status"}\r\n\r\n']))
    assert len(events) == 1
    assert events[0].type == "task_status"


def test_heartbeat_frames_without_data_are_detected():
    events = list(utils.iter_sse_events([
        b'event: heartbeat\n\n',
        b'data: {"type": "heartbeat"}\n\n',
        b'data: {"type": "task_artifact"}\n\n',
    ]))
    assert utils.is_heartbeat_event(events[0])
    assert utils.is_heartbeat_event(events[1])
    assert not utils.is_heartbeat_event(events[2])


def test_duplicate_events_are_suppressed_by_id():
    events = list(utils.iter_sse_events([
        b'id: 1\ndata: {"type": "task_artifact"}\n\n',
        b'id: 1\ndata: {"type": "task_artifact"}\n\n',
        b'id: 2\ndata: {"type": "task_artifact"}\n\n',
    ]))
    deduped = list(utils.dedupe_sse_events(events))
    assert [event.id for event in deduped] == ["1", "2"]


def test_a_frame_without_an_id_does_not_inherit_the_previous_id():
    events = list(utils.iter_sse_events([
        b'id: 1\ndata: {"type": "task_artifact"}\n\n',
        b'data: {"type": "task_artifact"}\n\n',
    ]))
    assert [event.id for event in events] == ["1", ""]
    deduped = list(utils.dedupe_sse_events(events))
    assert len(deduped) == 2
    assert utils.last_event_id(events) == "1"


def test_duplicate_events_are_suppressed_by_the_dedupe_flag():
    events = list(utils.iter_sse_events([
        b'data: {"type": "task_artifact"}\n\n',
        b'data: {"type": "task_artifact"}\n\n',
    ], dedupe=True))
    assert len(events) == 1


def test_summarize_sse_events_counts_frames_and_terminal_state():
    events = list(utils.iter_sse_events([
        b'id: 1\ndata: {"type": "task_status", "task_id": "t1", '
        b'"state": "working"}\n\n',
        b'id: 2\ndata: {"type": "task_artifact", "task_id": "t1"}\n\n',
        b'id: 3\ndata: {"type": "task_status", "task_id": "t1", '
        b'"state": "completed"}\n\n',
        b'event: heartbeat\n\n',
    ]))
    summary = utils.summarize_sse_events(events)
    assert summary["counts"] == {"task_status": 2, "task_artifact": 1,
                                  "heartbeat": 1}
    assert summary["terminal_state"] == "completed"
    assert summary["event_ids"] == ["1", "2", "3"]
    assert summary["task_ids"] == ["t1", "t1", "t1"]
    assert summary["artifacts"] == 1
    assert summary["heartbeats"] == 1


# ---------------------------------------------------------------------------
# 5. Redaction and metrics
# ---------------------------------------------------------------------------
def test_redact_replaces_every_occurrence_of_every_secret():
    text = "Authorization: Bearer s3cret and s3cret again, key=other-key"
    out = utils.redact(text, ["s3cret", "other-key"])
    assert "s3cret" not in out
    assert "other-key" not in out
    assert out.count(utils.REDACTION_PLACEHOLDER) == 3


def test_redact_ignores_empty_and_none_secrets():
    assert utils.redact("hello", [None, ""]) == "hello"


def test_redact_handles_none_text():
    assert utils.redact(None, ["x"]) == ""


def test_redact_structure_walks_nested_structures():
    payload = {"headers": {"Authorization": "Bearer s3cret"},
               "items": ["s3cret", 1, None]}
    out = utils.redact_structure(payload, ["s3cret"])
    assert "s3cret" not in json.dumps(out)
    assert out["items"][1] == 1


def test_build_long_running_metrics_reports_the_timings():
    metrics = utils.build_long_running_metrics(
        submission_latency=0.2,
        submitted_at=100.0, accepted_at=100.5, first_artifact_at=101.0,
        terminal_at=103.0, event_ids=["1", "2"], output="answer text",
        correlation_id="corr-1")
    data = metrics.to_dict()
    assert data["submission_latency_ms"] == 200.0
    assert data["acceptance_latency_ms"] == 500.0
    assert data["first_artifact_ms"] == 1000.0
    assert data["terminal_ms"] == 3000.0
    assert data["event_ids"] == ["1", "2"]
    assert data["output_length"] == len("answer text")
    assert data["correlation_id"] == "corr-1"


def test_build_long_running_metrics_never_records_answer_content():
    metrics = utils.build_long_running_metrics(
        output="the answer is 42", correlation_id="corr-2")
    assert "42" not in json.dumps(metrics.to_dict())
    assert metrics.output_length == len("the answer is 42")


def test_build_long_running_metrics_without_timestamps():
    data = utils.build_long_running_metrics().to_dict()
    assert data["submission_latency_ms"] is None
    assert data["acceptance_latency_ms"] is None
    assert data["first_artifact_ms"] is None
    assert data["terminal_ms"] is None
    assert data["event_ids"] == []
    assert data["output_length"] == 0


def test_extract_correlation_id_finds_the_backend_invocation_id():
    assert utils.extract_correlation_id({"correlation_id": "abc"}) == "abc"
    assert utils.extract_correlation_id({"invocationId": "xyz"}) == "xyz"
    assert utils.extract_correlation_id(
        {"metadata": {"backend_invocation_id": "deep"}}) == "deep"
    assert utils.extract_correlation_id({}) == ""
    assert utils.extract_correlation_id(None) == ""
