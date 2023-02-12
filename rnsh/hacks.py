import asyncio
import threading
import RNS
import logging
module_logger = logging.getLogger(__name__)


class SelfPruningAssociation:
    def __init__(self, ttl: float, loop: asyncio.AbstractEventLoop):
        self._log = module_logger.getChild(self.__class__.__name__)
        self._ttl = ttl
        self._associations = list()
        self._lock = threading.RLock()
        self._loop = loop
        # self._log.debug("__init__")

    def _schedule_prune(self, pair):
        # self._log.debug(f"schedule prune {pair}")

        def schedule_prune_inner(p):
            # self._log.debug(f"schedule inner {p}")
            self._loop.call_later(self._ttl, self._prune, p)

        self._loop.call_soon_threadsafe(schedule_prune_inner, pair)

    def _prune(self, pair: any):
        # self._log.debug(f"prune {pair}")
        with self._lock:
            self._associations.remove(pair)

    def get(self, key: any) -> any:
        # self._log.debug(f"get {key}")
        with self._lock:
            pair = next(filter(lambda x: x[0] == key, self._associations), None)
            if not pair:
                return None
            return pair[1]

    def put(self, key: any, value: any):
        # self._log.debug(f"put {key},{value}")
        with self._lock:
            pair = (key, value)
            self._associations.append(pair)
        self._schedule_prune(pair)


_request_to_link: SelfPruningAssociation = None
_link_handle_request_orig = RNS.Link.handle_request


def _link_handle_request(self, request_id, unpacked_request):
    global _request_to_link
    _request_to_link.put(request_id, self.link_id)
    _link_handle_request_orig(self, request_id, unpacked_request)


def request_request_id_hack(new_api_reponse_generator, loop):
    global _request_to_link
    if _request_to_link is None:
        RNS.Link.handle_request = _link_handle_request
        _request_to_link = SelfPruningAssociation(1.0, loop)

    def listen_request(path, data, request_id, remote_identity, requested_at):
        link_id = _request_to_link.get(request_id)
        return new_api_reponse_generator(path, data, request_id, link_id, remote_identity, requested_at)

    return listen_request
