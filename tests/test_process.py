import tests.helpers
import time
import pytest
import rnsh.process
import asyncio
import logging
import multiprocessing.pool
logging.getLogger().setLevel(logging.DEBUG)


# @pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_echo():
    """
    Echoing some text through cat.
    """
    with tests.helpers.SubprocessReader(argv=["/bin/cat"]) as state:
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


# @pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_echo_live():
    """
    Check for immediate echo
    """
    with tests.helpers.SubprocessReader(argv=["/bin/cat"]) as state:
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


# @pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_echo_live_pipe_in():
    """
    Check for immediate echo
    """
    with tests.helpers.SubprocessReader(argv=["/bin/cat"], stdin_is_pipe=True) as state:
        state.start()
        assert state.process is not None
        assert state.process.running
        message = "t"
        state.process.write(message.encode("utf-8"))
        await asyncio.sleep(0.1)
        data = state.read()
        state.process.close_stdin()
        await asyncio.sleep(0.1)
        assert len(data) > 0
        decoded = data.decode("utf-8")
        assert decoded == message
        assert not state.process.running


# @pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_echo_live_pipe_out():
    """
    Check for immediate echo
    """
    with tests.helpers.SubprocessReader(argv=["/bin/cat"], stdout_is_pipe=True) as state:
        state.start()
        assert state.process is not None
        assert state.process.running
        message = "t"
        state.process.write(message.encode("utf-8"))
        state.process.write(rnsh.process.CTRL_D)
        await asyncio.sleep(0.1)
        data = state.read()
        assert len(data) > 0
        decoded = data.decode("utf-8")
        assert decoded == message
        data = state.read_err()
        assert len(data) > 0
        state.process.close_stdin()
        await asyncio.sleep(0.1)
        assert not state.process.running


# @pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_echo_live_pipe_err():
    """
    Check for immediate echo
    """
    with tests.helpers.SubprocessReader(argv=["/bin/cat"], stderr_is_pipe=True) as state:
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


# @pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_echo_live_pipe_out_err():
    """
    Check for immediate echo
    """
    with tests.helpers.SubprocessReader(argv=["/bin/cat"], stdout_is_pipe=True, stderr_is_pipe=True) as state:
        state.start()
        assert state.process is not None
        assert state.process.running
        message = "t"
        state.process.write(message.encode("utf-8"))
        state.process.write(rnsh.process.CTRL_D)
        await asyncio.sleep(0.1)
        data = state.read()
        assert len(data) > 0
        decoded = data.decode("utf-8")
        assert decoded == message
        data = state.read_err()
        assert len(data) == 0
        state.process.close_stdin()
        await asyncio.sleep(0.1)
        assert not state.process.running



# @pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_echo_live_pipe_all():
    """
    Check for immediate echo
    """
    with tests.helpers.SubprocessReader(argv=["/bin/cat"], stdout_is_pipe=True, stderr_is_pipe=True,
                                        stdin_is_pipe=True) as state:
        state.start()
        assert state.process is not None
        assert state.process.running
        message = "t"
        state.process.write(message.encode("utf-8"))
        await asyncio.sleep(0.1)
        data = state.read()
        state.process.close_stdin()
        await asyncio.sleep(0.1)
        assert len(data) > 0
        decoded = data.decode("utf-8")
        assert decoded == message
        assert not state.process.running


# @pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_double_echo_live():
    """
    Check for immediate echo
    """
    with tests.helpers.SubprocessReader(name="state", argv=["/bin/cat"]) as state:
        with tests.helpers.SubprocessReader(name="state2", argv=["/bin/cat"]) as state2:
            state.start()
            state2.start()
            assert state.process is not None
            assert state.process.running
            assert state2.process is not None
            assert state2.process.running
            message = "t"
            state.process.write(message.encode("utf-8"))
            state2.process.write(message.encode("utf-8"))
            await asyncio.sleep(0.1)
            data = state.read()
            data2 = state2.read()
            state.process.write(rnsh.process.CTRL_C)
            state2.process.write(rnsh.process.CTRL_C)
            await asyncio.sleep(0.1)
            assert len(data) > 0
            assert len(data2) > 0
            decoded = data.decode("utf-8")
            decoded2 = data.decode("utf-8")
            assert decoded == message
            assert decoded2 == message
            assert not state.process.running
            assert not state2.process.running


@pytest.mark.asyncio
async def test_event_wait_any():
    delay = 0.5
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
