import uuid
import time
import pytest
import rnsh.process
import contextlib
import asyncio
import logging
import os
import threading
import types
import typing
import multiprocessing.pool
logging.getLogger().setLevel(logging.DEBUG)


class State(contextlib.AbstractContextManager):
    def __init__(self, argv: [str], loop: asyncio.AbstractEventLoop, env: dict = None):
        self.process: rnsh.process.CallbackSubprocess
        self.loop = loop
        self.env = env or os.environ.copy()
        self.argv = argv
        self._lock = threading.RLock()
        self._stdout = bytearray()
        self.return_code: int = None
        self.process = rnsh.process.CallbackSubprocess(argv=self.argv,
                                                       env=self.env,
                                                       loop=self.loop,
                                                       stdout_callback=self._stdout_cb,
                                                       terminated_callback=self._terminated_cb)

    def _stdout_cb(self, data):
        with self._lock:
            self._stdout.extend(data)

    def read(self):
        with self._lock:
            data = self._stdout.copy()
            self._stdout.clear()
            return data

    def _terminated_cb(self, rc):
        self.return_code = rc

    def start(self):
        self.process.start()

    def cleanup(self):
        if self.process and self.process.running:
            self.process.terminate(kill_delay=0.1)

    def __exit__(self, __exc_type: typing.Type[BaseException], __exc_value: BaseException,
                 __traceback: types.TracebackType) -> bool:
        self.cleanup()
        return False


@pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_echo():
    """
    Echoing some text through cat.
    """
    loop = asyncio.get_running_loop()
    with State(argv=["/bin/cat"],
               loop=loop) as state:
        state.start()
        assert state.process is not None
        assert state.process.running
        message = "test\n"
        state.process.write(message.encode("utf-8"))
        await asyncio.sleep(0.1)
        data = state.read()
        state.process.write(rnsh.process.CTRL_D)
        await asyncio.sleep(0.1)
        assert len(data) > 0
        decoded = data.decode("utf-8")
        assert decoded == message.replace("\n", "\r\n") * 2
        assert not state.process.running


@pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_echo_live():
    """
    Check for immediate echo
    """
    loop = asyncio.get_running_loop()
    with State(argv=["/bin/cat"],
               loop=loop) as state:
        state.start()
        assert state.process is not None
        assert state.process.running
        message = "t"
        state.process.write(message.encode("utf-8"))
        await asyncio.sleep(0.1)
        data = state.read()
        state.process.write(rnsh.process.CTRL_C)
        await asyncio.sleep(0.1)
        assert len(data) > 0
        decoded = data.decode("utf-8")
        assert decoded == message
        assert not state.process.running


@pytest.mark.asyncio
async def test_event_wait_any():
    delay = 0.1
    with multiprocessing.pool.ThreadPool() as pool:
        loop = asyncio.get_running_loop()
        evt1 = asyncio.Event()
        evt2 = asyncio.Event()

        def assert_between(min, max, val):
            assert min <= val <= max

        # test 1: both timeout
        ts = time.time()
        finished = await rnsh.process.event_wait_any([evt1, evt2], timeout=delay*2)
        assert_between(delay*2, delay*2.1, time.time() - ts)
        assert finished is None
        assert not evt1.is_set()
        assert not evt2.is_set()

        #test 2: evt1 set, evt2 not set
        hits = 0

        def test2_bg():
            nonlocal hits
            hits += 1
            time.sleep(delay)
            evt1.set()

        ts = time.time()
        pool.apply_async(test2_bg)
        finished = await rnsh.process.event_wait_any([evt1, evt2], timeout=delay * 2)
        assert_between(delay * 0.5, delay * 1.5, time.time() - ts)
        assert hits == 1
        assert evt1.is_set()
        assert not evt2.is_set()
        assert finished == evt1
