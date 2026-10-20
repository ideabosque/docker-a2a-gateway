"""Offline regressions for permission handling while message/send is blocked."""
import contextlib
import io
import json
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from chatbot_stream import SSEListener, answer_permission, interactive_request


class ChatbotStreamTests(unittest.TestCase):
    def setUp(self):
        self.listener = SSEListener("http://gateway", "token", "a2a", "tenant")
        self.listener.set_task("original-task")
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def test_permission_is_answered_before_http_can_return(self):
        for choice, approved in (("y", True), ("n", False), ("", False)):
            with self.subTest(choice=choice):
                resolved = threading.Event()
                response = Mock()

                def send():
                    self.listener.handle_event({"type": "task_permission",
                        "task_id": "original-task", "details": '{"title":"run command"}'})
                    self.listener.handle_event({"type": "task_status",
                        "task_id": "original-task", "state": "INPUT_REQUIRED"})
                    if not resolved.wait(2):
                        raise AssertionError("HTTP stayed blocked waiting for approval")
                    return response

                def answer(listener, event, decision):
                    self.assertIs(listener, self.listener)
                    self.assertEqual(event["task_id"], "original-task")
                    self.assertEqual(decision, approved)
                    resolved.set()

                with patch("chatbot_stream.answer_permission", side_effect=answer):
                    actual = interactive_request(send, self.listener, lambda _: choice)
                self.assertIs(actual, response)
        self.assertIn("run command", self.output.getvalue())

    def test_multiple_permissions_and_eof_denial(self):
        resolved = threading.Event()
        count = 0

        def send():
            for title in ("first", "second"):
                self.listener.handle_event({"type": "task_permission",
                    "task_id": "original-task", "details": title})
            self.assertTrue(resolved.wait(2))
            return "done"

        def answer(listener, event, decision):
            nonlocal count
            self.assertFalse(decision)
            count += 1
            if count == 2:
                resolved.set()

        with patch("chatbot_stream.answer_permission", side_effect=answer):
            self.assertEqual(interactive_request(send, self.listener,
                Mock(side_effect=EOFError)), "done")
        self.assertEqual(count, 2)

    def test_other_tasks_cannot_prompt_or_end_current_turn(self):
        for etype in ("task_permission", "task_status", "error"):
            self.listener.handle_event({"type": etype, "task_id": "other-task",
                "state": "completed", "error": "unrelated"})
        self.assertTrue(self.listener.permissions.empty())
        self.assertFalse(self.listener.done.is_set())
        self.assertEqual(self.output.getvalue(), "")

    def test_approval_wire_format_uses_original_task_and_boolean(self):
        response = Mock()
        response.json.return_value = {"result": {"parts": [{"text": "Approval granted"}]}}
        with patch("chatbot_stream.requests.post", return_value=response) as post:
            answer_permission(self.listener, {"task_id": "original-task"}, False)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["method"], "message/send")
        self.assertEqual(payload["params"]["metadata"], {
            "operation": "approval_response", "approval_task_id": "original-task",
            "approved": False})
        self.assertEqual(post.call_args.kwargs["headers"]["Part-Id"], "tenant")

    def test_small_permission_sse_event_is_not_buffered(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_lines.return_value = ["data:" + json.dumps({
            "type": "task_permission", "task_id": "original-task", "details": "small"})]
        with patch("chatbot_stream.requests.get", return_value=response):
            self.listener.start()
            self.listener.thread.join(2)
        response.iter_lines.assert_called_once_with(chunk_size=1, decode_unicode=True)
        self.assertEqual(self.listener.permissions.get_nowait()["details"], "small")

    def test_http_failure_propagates_without_extra_wait(self):
        def send():
            raise requests.Timeout("timed out")
        with self.assertRaises(requests.Timeout):
            interactive_request(send, self.listener)

    def test_terminal_states_and_reasoning_are_handled(self):
        self.listener.handle_event({"type": "task_artifact", "task_id": "original-task",
            "artifact": {"text": "thinking", "metadata": {"data_format": "reasoning"}}})
        self.listener.handle_event({"type": "task_artifact", "task_id": "original-task",
            "artifact": {"text": "answer"}})
        self.assertEqual(self.listener.full_text, "answer")
        self.listener.handle_event({"type": "task_status", "task_id": "original-task",
            "status": {"state": "TASK_STATE_FAILED"}})
        self.assertTrue(self.listener.done.is_set())


if __name__ == "__main__":
    unittest.main()
