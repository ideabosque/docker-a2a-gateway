"""Transport regressions for the sibling a2a_protocol_plugin checkout."""
import json
import logging
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from a2a_protocol_plugin.handlers.banyanos_client import (
    BanyanosClient, BanyanosClientError,
)


def client(**kwargs):
    return BanyanosClient(logging.getLogger(__name__),
                         'http://backend.test', 'ws://backend.test', 'banyan',
                         **kwargs)


def test_ws_credentials_and_partition_round_trip():
    instance = client(token='a+b&c=d', part_id='tenant #1&other=2')
    params = parse_qs(urlsplit(instance._build_ws_uri()).query)
    assert params == {'token': ['a+b&c=d'], 'part_id': ['tenant #1&other=2']}


@pytest.mark.parametrize('body', [None, [], 'invalid'])
def test_nonobject_graphql_payload_is_protocol_error(body):
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=json.dumps(body), headers={'Content-Type': 'application/json'}))
    with pytest.raises(BanyanosClientError) as exc:
        client(http_transport=transport)._post_graphql('query { x }', {}, ('x',))
    assert exc.value.category == 'protocol'


def test_http_deadline_has_timeout_category():
    def timeout(request):
        raise httpx.ReadTimeout('deadline', request=request)
    with pytest.raises(BanyanosClientError) as exc:
        client(http_transport=httpx.MockTransport(timeout))._post_graphql(
            'query { x }', {}, ('x',))
    assert exc.value.category == 'timeout'


def test_idle_receive_does_not_end_stream_before_deadline():
    class Socket:
        count = 0
        closed = False

        def recv(self, timeout):
            self.count += 1
            if self.count == 1:
                return {'type': 'connection_ack', 'connection_id': 'test-connection'}
            if self.count == 2:
                raise TimeoutError('idle interval')
            return {'data': {'agentInvokeStream': {
                'eventType': 'completed', 'content': 'final answer'}}}

        def close(self):
            self.closed = True

    socket = Socket()
    instance = client(ws_connect=lambda uri: socket, stream_timeout=60,
                      http_transport=httpx.MockTransport(
                          lambda request: httpx.Response(200, json={'data': {}})))
    result = instance.invoke_agent_stream('agent', 'hello')
    assert result.event_type == 'completed'
    assert result.content == 'final answer'
    assert socket.closed
    assert instance._connection_id is None
