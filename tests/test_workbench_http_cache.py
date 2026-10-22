"""Conditional state polling preserves updates, errors and rollback visibility."""
import json
import os
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from battery_insight.workbench_server import make_server
from test_workbench_server import workspace_controller


def test_unchanged_state_reuses_bytes_and_sends_empty_304(workspace_controller):
    controller, *_ = workspace_controller
    status, encoded, etag = controller.http_state()
    assert status == 200 and json.loads(encoded) == controller.state()
    assert controller.http_state()[1] is encoded
    assert controller.http_state(etag) == (304, b"", etag)
    assert controller.http_state('"other", W/' + etag) == (304, b"", etag)
    assert controller.http_state('"stale"')[0] == 200


def test_compare_profile_error_and_rollback_invalidate_cached_state(workspace_controller, monkeypatch):
    controller, *_ = workspace_controller
    first = controller.http_state()[2]
    options = dict(controller.options, metric_ids=[], design_field_ids=[])
    controller.compare(options)
    compared = controller.http_state(first)
    assert compared[0] == 200
    assert json.loads(compared[1])['analysis']['graphs']['electrochem']['edges'] == []
    controller.save_profile('selected', options, ['network'])
    saved = controller.http_state(compared[2])
    assert saved[0] == 200 and json.loads(saved[1])['profiles'][0]['name'] == 'selected'
    controller.last_error = 'new input rejected'
    failed = controller.http_state(saved[2])
    assert failed[0] == 200 and json.loads(failed[1])['last_error'] == controller.last_error
    controller.last_error = None
    recovered = controller.http_state(failed[2])
    assert recovered[0] == 200 and json.loads(recovered[1])['last_error'] is None
    from battery_insight import workbench_report
    monkeypatch.setattr(workbench_report, 'write_workbench_report', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        controller.compare(dict(options, life_max=options['life_max'] + 1))
    assert controller.http_state(recovered[2])[0] == 304


def test_external_profile_edit_with_same_timestamp_is_not_stale(workspace_controller):
    controller, *_ = workspace_controller
    path = controller.output / 'comparison_profiles.json'
    path.write_text('[{"name":"A"}]', encoding='utf-8')
    before = path.stat()
    original = controller.http_state()[2]
    path.write_text('[{"name":"B"}]', encoding='utf-8')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    status, encoded, _ = controller.http_state(original)
    assert status == 200 and json.loads(encoded)['profiles'] == [{'name': 'B'}]


def test_http_validator_route_preserves_local_access_checks(workspace_controller):
    controller, *_ = workspace_controller
    server = make_server(controller, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(address + '/api/state') as response:
            etag = response.headers['ETag']
            assert json.load(response)['capabilities']['server']
        request = Request(address + '/api/state', headers={'If-None-Match': etag})
        with pytest.raises(HTTPError) as unchanged:
            urlopen(request)
        assert unchanged.value.code == 304 and unchanged.value.read() == b''
        assert unchanged.value.headers['ETag'] == etag
        request = Request(address + '/api/state', headers={'If-None-Match': etag, 'Origin': 'https://example.com'})
        with pytest.raises(HTTPError) as foreign:
            urlopen(request)
        assert foreign.value.code == 403
        controller.last_error = 'visible refresh failure'
        with urlopen(Request(address + '/api/state', headers={'If-None-Match': etag})) as response:
            assert response.status == 200 and json.load(response)['last_error'] == controller.last_error
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
