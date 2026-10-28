"""Offline checks for meaningful BanyanOS live-test assertions."""
import importlib.util
import json
import struct
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import a2a_test_utils as utils


@pytest.mark.parametrize('body, expected', [
    ({'data': {'__typename': 'Query'}}, True),
    ({'errors': [{'message': 'unauthorized'}]}, False),
    ({'data': None}, False),
    ({'data': {'__typename': 'Query'}, 'errors': [{'message': 'denied'}]}, False),
    ([], False),
])
def test_graphql_probe_requires_valid_data(monkeypatch, body, expected):
    response = Mock(status_code=200)
    response.json.return_value = body
    monkeypatch.setattr(utils.requests, 'post', Mock(return_value=response))
    assert utils.banyanos_graphql_ok('http://test', 'banyan', 'token') is expected


def test_websocket_query_preserves_special_characters():
    conn = utils.BanyanosRawWebSocket('ws://test?feature=yes', 'banyan',
                                     'a+b&c=d', 'tenant #1')
    assert parse_qs(urlsplit(conn.path).query) == {
        'feature': ['yes'], 'token': ['a+b&c=d'], 'part_id': ['tenant #1']}


def test_empty_close_frame_finishes_without_another_read():
    conn = utils.BanyanosRawWebSocket('ws://test', 'banyan', 'token')
    conn.sock = Mock()
    conn._buf = b'\x88\x00'
    assert conn.recv_json() is None
    conn.sock.recv.assert_not_called()


def test_split_extended_length_frame_and_ping():
    conn = utils.BanyanosRawWebSocket('ws://test', 'banyan', 'token')
    payload = json.dumps({'content': 'x' * 160}).encode()
    frame = b'\x81\x7e' + struct.pack('>H', len(payload)) + payload
    conn._buf = b'\x89\x00' + frame[:2]
    conn.sock = Mock()
    conn.sock.recv.side_effect = [frame[2:3], frame[3:]]
    assert conn.recv_json()['content'] == 'x' * 160
    pong = conn.sock.sendall.call_args.args[0]
    assert pong[0] == 0x8A
    assert pong[1] & 0x80  # client control frames must be masked


def test_host_probes_remap_docker_host_alias():
    cfg = utils.resolve_config({'BANYANOS_WS_URL': 'ws://host.docker.internal:18000'})
    assert cfg['banyanos_ws_url'] == 'ws://127.0.0.1:18000'


def test_missing_persisted_task_is_failure(monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'banyanos/test_gateway_live.py'
    spec = importlib.util.spec_from_file_location('banyanos_gateway_cli', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'send_a2a', Mock())
    monkeypatch.setattr(module, 'unwrap_response', lambda response: {
        'error': {'code': -32001, 'message': 'Task not found'}})
    cfg = dict(gateway_url='http://test', token='token', endpoint_id='a2a',
               part_id='default', agent_uuid='agent')
    module.test_08_tasks_get(cfg, 'missing')
    assert module._results == ['fail']
