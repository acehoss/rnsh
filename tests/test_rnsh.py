import logging
logging.getLogger().setLevel(logging.DEBUG)

import tests.helpers
import rnsh.rnsh
import rnsh.process
import shlex
import pytest
import time
import asyncio
import re
import os


def test_version():
    assert rnsh.__version__ != "0.0.0"
    assert rnsh.__version__ != "0.0.1"


@pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_wrapper():
    with tests.helpers.tempdir() as td:
        with tests.helpers.SubprocessReader(argv=shlex.split(f"date")) as wrapper:
            wrapper.start()
            assert wrapper.process is not None
            assert wrapper.process.running
            await asyncio.sleep(1)
            text = wrapper.read().decode("utf-8")
            assert len(text) > 5
            assert not wrapper.process.running



@pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_rnsh_listen_start_stop():
    with tests.helpers.tempdir() as td:
        with tests.helpers.SubprocessReader(argv=shlex.split(f"poetry run rnsh -l --config \"{td}\" -n -C -vvvvvv -- /bin/ls")) as wrapper:
            wrapper.start()
            await asyncio.sleep(0.1)
            assert wrapper.process.running
            # wait for process to start up
            await asyncio.sleep(3)
            # read the output
            text = wrapper.read().decode("utf-8")
            # listener should have printed "listening
            assert text.index("listening") is not None
            # stop process with SIGINT
            wrapper.process.write(rnsh.process.CTRL_C)
            # wait for process to wind down
            start_time = time.time()
            while wrapper.process.running and time.time() - start_time < 5:
                await asyncio.sleep(0.1)
            assert not wrapper.process.running


async def get_listener_id_and_dest(td: str) -> tuple[str, str]:
    with tests.helpers.SubprocessReader(name="getid", argv=shlex.split(f"poetry run -- rnsh -l -c \"{td}\" -p")) as wrapper:
        wrapper.start()
        await asyncio.sleep(0.1)
        assert wrapper.process.running
        # wait for process to start up
        await tests.helpers.wait_for_condition_async(lambda: not wrapper.process.running, 5)
        assert not wrapper.process.running
        await asyncio.sleep(2)
        # read the output
        text = wrapper.read().decode("utf-8").replace("\r", "").replace("\n", "")
        assert text.index("Using service name \"default\"") is not None
        assert text.index("Identity") is not None
        match = re.search(r"<([a-f0-9]{32})>[^<]+<([a-f0-9]{32})>", text)
        assert match is not None
        ih = match.group(1)
        assert len(ih) == 32
        dh = match.group(2)
        assert len(dh) == 32
        await asyncio.sleep(0.1)
        return ih, dh


async def get_initiator_id(td: str) -> str:
    with tests.helpers.SubprocessReader(name="getid", argv=shlex.split(f"poetry run -- rnsh -c \"{td}\" -p")) as wrapper:
        wrapper.start()
        await asyncio.sleep(0.1)
        assert wrapper.process.running
        # wait for process to start up
        await tests.helpers.wait_for_condition_async(lambda: not wrapper.process.running, 5)
        assert not wrapper.process.running
        # read the output
        text = wrapper.read().decode("utf-8").replace("\r", "").replace("\n", "")
        assert text.index("Identity") is not None
        match = re.search(r"<([a-f0-9]{32})>", text)
        assert match is not None
        ih = match.group(1)
        assert len(ih) == 32
        await asyncio.sleep(0.1)
        return ih



@pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_rnsh_get_listener_id_and_dest() -> [int]:
    with tests.helpers.tempdir() as td:
        ih, dh = await get_listener_id_and_dest(td)
        assert len(ih) == 32
        assert len(dh) == 32


@pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_rnsh_get_initiator_id() -> [int]:
    with tests.helpers.tempdir() as td:
        ih = await get_initiator_id(td)
        assert len(ih) == 32


async def do_connected_test(listener_args: str, initiator_args: str, test: callable):
    with tests.helpers.tempdir() as td:
        ih, dh = await get_listener_id_and_dest(td)
        iih = await get_initiator_id(td)
        assert len(ih) == 32
        assert len(dh) == 32
        assert len(iih) == 32
        assert "dh" in initiator_args
        initiator_args = initiator_args.replace("dh", dh)
        listener_args = listener_args.replace("iih", iih)
        with tests.helpers.SubprocessReader(name="listener", argv=shlex.split(f"poetry run -- rnsh -l -c \"{td}\" {listener_args}")) as listener, \
                tests.helpers.SubprocessReader(name="initiator", argv=shlex.split(f"poetry run -- rnsh -q -c \"{td}\" {initiator_args}")) as initiator:
            # listener startup
            listener.start()
            await asyncio.sleep(0.1)
            assert listener.process.running
            # wait for process to start up
            await asyncio.sleep(5)
            # read the output
            text = listener.read().decode("utf-8")
            assert text.index(dh) is not None

            # initiator run
            initiator.start()
            assert initiator.process.running

            await test(td, ih, dh, iih, listener, initiator)

            # expect test to shut down initiator
            assert not initiator.process.running

            # stop process with SIGINT
            listener.process.write(rnsh.process.CTRL_C)
            # wait for process to wind down
            start_time = time.time()
            while listener.process.running and time.time() - start_time < 5:
                await asyncio.sleep(0.1)
            assert not listener.process.running


@pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_rnsh_get_echo_through():
    cwd = os.getcwd()

    async def test(td: str, ih: str, dh: str, iih: str, listener: tests.helpers.SubprocessReader,
                   initiator: tests.helpers.SubprocessReader):
        start_time = time.time()
        while initiator.return_code is None and time.time() - start_time < 3:
            await asyncio.sleep(0.1)
        text = initiator.read().decode("utf-8").replace("\r", "").replace("\n", "")
        assert text == cwd

    await do_connected_test("-n -C -- /bin/pwd", "dh", test)


@pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_rnsh_no_ident():
    cwd = os.getcwd()

    async def test(td: str, ih: str, dh: str, iih: str, listener: tests.helpers.SubprocessReader,
                   initiator: tests.helpers.SubprocessReader):
        start_time = time.time()
        while initiator.return_code is None and time.time() - start_time < 3:
            await asyncio.sleep(0.1)
        text = initiator.read().decode("utf-8").replace("\r", "").replace("\n", "")
        assert text == cwd

    await do_connected_test("-n -C -- /bin/pwd", "-N dh", test)


@pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_rnsh_invalid_ident():
    cwd = os.getcwd()

    async def test(td: str, ih: str, dh: str, iih: str, listener: tests.helpers.SubprocessReader,
                   initiator: tests.helpers.SubprocessReader):
        start_time = time.time()
        while initiator.return_code is None and time.time() - start_time < 3:
            await asyncio.sleep(0.1)
        text = initiator.read().decode("utf-8").replace("\r", "").replace("\n", "")
        assert "not allowed" in text

    await do_connected_test("-a 12345678901234567890123456789012 -C -- /bin/pwd", "dh", test)


@pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_rnsh_valid_ident():
    cwd = os.getcwd()

    async def test(td: str, ih: str, dh: str, iih: str, listener: tests.helpers.SubprocessReader,
                   initiator: tests.helpers.SubprocessReader):
        start_time = time.time()
        while initiator.return_code is None and time.time() - start_time < 3:
            await asyncio.sleep(0.1)
        text = initiator.read().decode("utf-8").replace("\r", "").replace("\n", "")
        assert (text == cwd)

    await do_connected_test("-a iih -C -- /bin/pwd", "dh", test)




