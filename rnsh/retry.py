import asyncio
import threading
import time
import logging as __logging
module_logger = __logging.getLogger(__name__)

class RetryStatus:
    def __init__(self, id: any, try_limit: int, wait_delay: float, retry_callback: callable[any, int], timeout_callback: callable[any], tries: int = 1):
        self.id = id
        self.try_limit = try_limit
        self.tries = tries
        self.wait_delay = wait_delay
        self.retry_callback = retry_callback
        self.timeout_callback = timeout_callback
        self.try_time = time.monotonic()
        self.completed = False

    @property
    def ready(self):
        return self.try_time + self.wait_delay < time.monotonic() and not self.completed

    @property
    def timed_out(self):
        return self.ready and self.tries >= self.try_limit

    def timeout(self):
        self.completed = True
        self.timeout_callback(self.id)

    def retry(self):
        self.tries += 1
        self.retry_callback(self.id, self.tries)

class RetryThread:
    def __init__(self, loop_period: float = 0.25):
        self._log = module_logger.getChild(self.__class__.__name__)
        self._loop_period = loop_period
        self._statuses: list[RetryStatus] = []
        self._id_counter = 0
        self._lock = threading.RLock()
        self._thread = threading.Thread(target=self._thread_run())
        self._run = True
        self._finished: asyncio.Future | None = None
        self._thread.start()

    def close(self, loop: asyncio.AbstractEventLoop | None = None) -> asyncio.Future | None:
        self._log.debug("stopping timer thread")
        if loop is None:
            self._run = False
            self._thread.join()
            return None
        else:
            self._finished = loop.create_future()
            return self._finished
    def _thread_run(self):
        last_run = time.monotonic()
        while self._run and self._finished is None:
            time.sleep(last_run + self._loop_period - time.monotonic())
            last_run = time.monotonic()
            ready: list[RetryStatus] = []
            prune: list[RetryStatus] = []
            with self._lock:
                ready.extend(filter(lambda s: s.ready, self._statuses))
            for retry in ready:
                try:
                    if not retry.completed:
                        if retry.timed_out:
                            self._log.debug(f"timed out {retry.id} after {retry.try_limit} tries")
                            retry.timeout()
                            prune.append(retry)
                        else:
                            self._log.debug(f"retrying {retry.id}, try {retry.tries + 1}/{retry.try_limit}")
                            retry.retry()
                except Exception as e:
                    self._log.error(f"error processing retry id {retry.id}: {e}")
                    prune.append(retry)

            with self._lock:
                for retry in prune:
                    self._log.debug(f"pruned retry {retry.id}, retry count {retry.tries}/{retry.try_limit}")
                    self._statuses.remove(retry)
        if self._finished is not None:
            self._finished.set_result(None)

    def _get_id(self):
        self._id_counter += 1
        return self._id_counter

    def begin(self, try_limit: int, wait_delay: float, try_callback: callable[[any | None, int], any], timeout_callback: callable[any, int], id: int | None = None) -> any:
        self._log.debug(f"running first try")
        id = try_callback(id, 1)
        self._log.debug(f"first try success, got id {id}")
        with self._lock:
            if id is None:
                id = self._get_id()
            self._statuses.append(RetryStatus(id=id,
                                              tries=1,
                                              try_limit=try_limit,
                                              wait_delay=wait_delay,
                                              retry_callback=try_callback,
                                              timeout_callback=timeout_callback))
        self._log.debug(f"added retry timer for {id}")
    def complete(self, id: any):
        assert id is not None
        with self._lock:
            status = next(filter(lambda l: l.id == id, self._statuses))
            assert status is not None
            status.completed = True
            self._statuses.remove(status)
        self._log.debug(f"completed {id}")
    def complete_all(self):
        with self._lock:
            for status in self._statuses:
                status.completed = True
                self._log.debug(f"completed {status.id}")
            self._statuses.clear()
