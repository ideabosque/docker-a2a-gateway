"""Shared interactive SSE and permission handling for the chatbot harnesses."""
import json
import queue
import threading
import uuid

import requests

from a2a_test_utils import R, Y, RST, StreamSections, is_reasoning_artifact


class SSEListener:
    def __init__(self, gateway_url, token, endpoint_id, part_id):
        self.gateway_url = gateway_url
        self.token = token
        self.endpoint_id = endpoint_id
        self.part_id = part_id
        self.stop = False
        self.thread = None
        self.current_task_id = None
        self.full_text = ""
        self.sections = StreamSections(response_color=Y)
        self.done = threading.Event()
        self.permissions = queue.Queue()
        self.ready = threading.Event()
        self.connected = False

    def handle_event(self, event):
        if not isinstance(event, dict) or self.current_task_id is None:
            return
        # Status and permission events must be scoped just like text chunks.
        tid = event.get("task_id", event.get("taskId"))
        if tid not in (self.current_task_id, "streaming-task"):
            return
        etype = event.get("type", event.get("event", ""))
        if etype == "task_permission":
            self.permissions.put(event)
        elif etype == "task_artifact":
            artifact = event.get("artifact", {})
            text = artifact.get("text", "") if isinstance(artifact, dict) else ""
            if text:
                if is_reasoning_artifact(artifact):
                    self.sections.reasoning_chunk(text)
                elif self.full_text and text.startswith(self.full_text):
                    delta = text[len(self.full_text):]
                    self.sections.response_chunk(delta)
                    self.full_text = text
                elif not self.full_text or not self.full_text.startswith(text):
                    self.sections.response_chunk(text)
                    self.full_text += text
        elif etype in ("task_status", "status"):
            state = event.get("state", event.get("status", ""))
            if isinstance(state, dict):
                state = state.get("state", "")
            state = str(state).lower().removeprefix("task_state_").replace("-", "_")
            if state in ("input_required", "auth_required", "failed", "canceled",
                         "cancelled", "rejected"):
                self.sections.close()
                print(f"\n{Y}[{state}]{RST}", flush=True)
            if state in ("completed", "failed", "canceled", "cancelled", "rejected"):
                self.done.set()
        elif etype == "error":
            print(f"\n{R}Error: {event.get('error', event.get('message', ''))}{RST}",
                  flush=True)
            self.done.set()

    def start(self):
        def listen():
            try:
                with requests.get(
                    f"{self.gateway_url}/{self.endpoint_id}/a2a_sse",
                    headers={"Authorization": f"Bearer {self.token}",
                             "Part-Id": self.part_id, "Accept": "text/event-stream"},
                    stream=True, timeout=(10, 300),
                ) as response:
                    response.raise_for_status()
                    self.connected = True
                    self.ready.set()
                    # requests' default buffering can hide small permission events.
                    for line in response.iter_lines(chunk_size=1, decode_unicode=True):
                        if self.stop:
                            break
                        if line and line.startswith("data:"):
                            try:
                                event = json.loads(line[5:].strip())
                            except (ValueError, TypeError):
                                continue
                            self.handle_event(event)
            except requests.RequestException as exc:
                if not self.stop:
                    print(f"\n{R}SSE connection lost: {exc}. "
                          f"Pending permissions cannot be answered.{RST}", flush=True)
            finally:
                self.connected = False
                self.ready.set()

        self.thread = threading.Thread(target=listen, daemon=True)
        self.thread.start()
        self.ready.wait(timeout=11)

    def set_task(self, task_id):
        self.current_task_id = task_id
        self.full_text = ""
        self.sections.reset()
        self.done.clear()
        while True:
            try:
                self.permissions.get_nowait()
            except queue.Empty:
                break

    def stop_listening(self):
        self.stop = True


def answer_permission(listener, event, approved):
    """Use the original SSE task id, not the SDK id of this new message."""
    response = requests.post(
        f"{listener.gateway_url}/{listener.endpoint_id}/a2a",
        headers={"Authorization": f"Bearer {listener.token}",
                 "Part-Id": listener.part_id, "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "method": "message/send",
              "id": f"approval-{uuid.uuid4().hex}",
              "params": {
                  "message": {"role": "ROLE_USER", "parts": [
                      {"text": "Approve once" if approved else "Deny"}]},
                  "metadata": {"operation": "approval_response",
                               "approval_task_id": event.get("task_id", event.get("taskId")),
                               "approved": approved}}},
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("error"):
        raise ValueError(f"Approval failed: {body['error']}")
    result = body.get("result", {})
    text = "".join(p.get("text", "") for p in result.get("parts", [])
                   if isinstance(p, dict))
    if "No pending approval" in text or "missing task_id" in text:
        raise ValueError(text)
    print(f"{Y}{text or 'Permission answer sent.'}{RST}", flush=True)


def interactive_request(send, listener, input_fn=None):
    """Keep stdin available for permissions while message/send waits on the agent."""
    if listener is None:
        return send()
    input_fn = input_fn or input
    result = queue.Queue()

    def worker():
        try:
            result.put((send(), None))
        except Exception as exc:
            result.put((None, exc))

    threading.Thread(target=worker, daemon=True).start()
    while True:
        try:
            event = listener.permissions.get(timeout=0.1)
        except queue.Empty:
            event = None
        if event is not None:
            listener.sections.close()
            details = event.get("details", event.get("tool_call", {}))
            if not isinstance(details, str):
                details = json.dumps(details, indent=2, ensure_ascii=False)
            print(f"\n{Y}Permission requested:\n{details}{RST}", flush=True)
            try:
                while True:
                    choice = input_fn("Allow this action once? [y/N] ").strip().lower()
                    if choice in ("y", "yes", "", "n", "no"):
                        break
                    print("Enter y to allow once, or n to deny.")
            except (EOFError, KeyboardInterrupt):
                choice = "n"
            try:
                answer_permission(listener, event, choice in ("y", "yes"))
            except (requests.RequestException, ValueError) as exc:
                print(f"{R}Could not answer permission: {exc}{RST}", flush=True)
        try:
            response, error = result.get_nowait()
        except queue.Empty:
            continue
        if error:
            raise error
        return response
