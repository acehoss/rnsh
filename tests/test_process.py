import uuid
import time
from types import TracebackType
from typing import Type
import pytest
import rnsh.process
import contextlib
import asyncio
import logging
import os
import threading
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

    def __exit__(self, __exc_type: Type[BaseException], __exc_value: BaseException,
                 __traceback: TracebackType) -> bool:
        self.cleanup()
        return False


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
