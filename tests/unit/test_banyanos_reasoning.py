"""BanyanOS reasoning defaults, overrides and backend request propagation."""
import logging
import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from a2a_protocol_plugin.handlers.a2a_ai_agent_utility import resolve_show_reasoning
from a2a_protocol_plugin.handlers.banyanos_agent_handler import BanyanosAgentHandler
from a2a_protocol_plugin.handlers.banyanos_client import BanyanosClient, BanyanosStreamEvent
from a2a_protocol_plugin.handlers.config import Config


AGENT = {'metadata': {'agent_type': 'banyanos_agent'}}


@pytest.mark.parametrize('default, request_value, agent_value, expected', [
    (True, None, None, True), (False, None, None, False),
    (False, True, None, True), (True, False, None, False),
    (False, None, True, True), (True, None, False, False),
    (True, 'false', True, False), (False, 'true', False, True),
])
def test_reasoning_precedence(monkeypatch, default, request_value, agent_value, expected):
    monkeypatch.setattr(Config, 'banyanos_show_reasoning', default)
    agent = {'metadata': dict(AGENT['metadata'])}
    if agent_value is not None:
        agent['metadata']['show_reasoning'] = agent_value
    assert resolve_show_reasoning(agent, request_value) is expected


def test_banyanos_default_does_not_change_other_agents(monkeypatch):
    monkeypatch.setattr(Config, 'banyanos_show_reasoning', False)
    monkeypatch.setattr(Config, 'a2a_show_reasoning', True)
    assert resolve_show_reasoning({'metadata': {'agent_type': 'pi'}}) is True


def test_unset_banyanos_flag_inherits_global(monkeypatch):
    monkeypatch.setattr(Config, 'banyanos_show_reasoning', None)
    monkeypatch.setattr(Config, 'a2a_show_reasoning', False)
    assert resolve_show_reasoning(AGENT) is False


@pytest.mark.parametrize('value, expected', [('true', True), ('false', False), ('0', False)])
def test_config_reads_env_boolean(monkeypatch, value, expected):
    # Use a subclass so configuration resets cannot leak into other tests.
    class TestConfig(Config):
        pass
    monkeypatch.setenv('BANYANOS_SHOW_REASONING', value)
    monkeypatch.setattr(TestConfig, 'logger', logging.getLogger(__name__))
    TestConfig._set_parameters({})
    assert TestConfig.banyanos_show_reasoning is expected


@pytest.mark.parametrize('enabled', [True, False])
def test_handler_requests_and_gates_reasoning(monkeypatch, enabled):
    monkeypatch.setattr(Config, 'banyanos_show_reasoning', enabled)
    handler = BanyanosAgentHandler(logging.getLogger(__name__), AGENT)
    client = Mock()
    def invoke(**kwargs):
        assert kwargs['include_reasoning'] is enabled
        # Even an incorrectly configured backend must not leak disabled events.
        kwargs['on_event'](BanyanosStreamEvent('reasoning', content='provider reasoning'))
        kwargs['on_event'](BanyanosStreamEvent('chunk', content='answer'))
        return BanyanosStreamEvent('completed', content='answer')
    client.invoke_agent_stream.side_effect = invoke
    output = queue.Queue()
    result = handler._ask_streaming(client, SimpleNamespace(banyanos_agent_id='agent'),
        {'idempotency_key': 'test'}, 'hello', None, {}, output, None)
    chunks = list(output.queue)
    reasoning = [chunk['value'] for chunk in chunks
                 if chunk.get('meta', {}).get('data_format') == 'reasoning']
    assert reasoning == (['provider reasoning'] if enabled else [])
    assert result['content'] == 'answer'


@pytest.mark.parametrize('enabled', [True, False])
def test_client_sends_graphql_reasoning_variable(enabled):
    client = BanyanosClient(logging.getLogger(__name__), 'http://test', 'ws://test', 'banyan')
    client._stream = Mock()
    client.invoke_agent_stream('agent', 'hello', include_reasoning=enabled)
    assert client._stream.call_args.kwargs['variables']['includeReasoning'] is enabled


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [True, False])
async def test_bridge_forwards_resolved_flag_and_separates_reasoning(monkeypatch, enabled):
    from a2a_protocol_plugin.handlers import a2a_ai_agent_utility as bridge
    monkeypatch.setattr(Config, 'banyanos_show_reasoning', enabled)
    monkeypatch.setattr(bridge, 'resolve_agent', AsyncMock(return_value=AGENT))
    monkeypatch.setattr(bridge, 'build_input_messages', AsyncMock(return_value=[
        {'role': 'user', 'content': 'hello'}]))
    for name in ('_persist_user_message', '_persist_thread_run_message',
                 '_broadcast_to_gateway_sse', '_emit_to_sdk'):
        monkeypatch.setattr(bridge, name, AsyncMock())
    def ask_model(**kwargs):
        assert kwargs['context']['show_reasoning'] is enabled
        kwargs['stream_queue'].put({'name': 'token', 'value': 'provider reasoning',
                                   'meta': {'data_format': 'reasoning'}})
        kwargs['stream_queue'].put({'name': 'token', 'value': 'answer'})
        kwargs['stream_event'].set()
        return {'content': 'answer', 'role': 'agent'}
    monkeypatch.setattr(bridge, 'load_agent_handler', Mock(return_value=SimpleNamespace(
        ask_model=ask_model)))
    result = await bridge.execute_ai_agent_streaming(
        partition_key='a2a#test', agent_uuid='banyanos-agent', user_query='hello',
        event_queue=AsyncMock(), context_id='context', task_id='test', stream_timeout=5)
    assert result.content == 'answer'
    assert result.metadata.get('reasoning') == ('provider reasoning' if enabled else None)
