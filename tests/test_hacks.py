import asyncio

import rnsh.hacks as hacks
import pytest
import time
import logging
logging.getLogger().setLevel(logging.DEBUG)


class FakeLink:
    def __init__(self, link_id):
        self.link_id = link_id


@pytest.mark.asyncio
async def test_pruning():
    def listen_request(path, data, request_id, link_id, remote_identity, requested_at):
        assert path == 1
        assert data == 2
        assert request_id == 3
        assert remote_identity == 4
        assert requested_at == 5
        assert link_id == 6
        return 7

    lhr_called = 0
    link = FakeLink(6)

    def link_handle_request(self, request_id, unpacked_request):
        nonlocal lhr_called
        lhr_called += 1

    old_func = hacks.request_request_id_hack(listen_request, asyncio.get_running_loop())
    hacks._link_handle_request_orig = link_handle_request
    hacks._link_handle_request(link, 3, None)
    link_id = hacks._request_to_link.get(3)
    assert link_id == link.link_id
    result = old_func(1, 2, 3, 4, 5)
    assert result == 7
    link_id = hacks._request_to_link.get(3)
    assert link_id == 6
    await asyncio.sleep(1.5)
    link_id = hacks._request_to_link.get(3)
    assert link_id is None
