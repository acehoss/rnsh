import logging
import types
import typing
import tempfile

import pytest

import rnsh.rnsh
import asyncio
import rnsh.process
import contextlib
import threading
import os
import pathlib
import tests
import shutil
import random

module_logger = logging.getLogger(__name__)

module_abs_filename = os.path.abspath(tests.__file__)
module_dir = os.path.dirname(module_abs_filename)


class SubprocessReader(contextlib.AbstractContextManager):
    def __init__(self, argv: [str], env: dict = None, name: str = None, stdin_is_pipe: bool = False,
                 stdout_is_pipe: bool = False, stderr_is_pipe: bool = False):
        self._log = module_logger.getChild(self.__class__.__name__ + ("" if name is None else f"({name})"))
        self.name = name or "subproc"
        self.process: rnsh.process.CallbackSubprocess
        self.loop = asyncio.get_running_loop()
        self.env = env or os.environ.copy()
        self.argv = argv
        self._lock = threading.RLock()
        self._stdout = bytearray()
        self._stderr = bytearray()
        self.return_code: int = None
        self.process = rnsh.process.CallbackSubprocess(argv=self.argv,
                                                       env=self.env,
                                                       loop=self.loop,
                                                       stdout_callback=self._stdout_cb,
                                                       terminated_callback=self._terminated_cb,
                                                       stderr_callback=self._stderr_cb,
                                                       stdin_is_pipe=stdin_is_pipe,
                                                       stdout_is_pipe=stdout_is_pipe,
                                                       stderr_is_pipe=stderr_is_pipe)

    def _stdout_cb(self, data):
        self._log.debug(f"_stdout_cb({data})")
        with self._lock:
            self._stdout.extend(data)

    def read(self):
        self._log.debug(f"read()")
        with self._lock:
            data = self._stdout.copy()
            self._stdout.clear()
        self._log.debug(f"read() returns {data}")
        return data

    def _stderr_cb(self, data):
        self._log.debug(f"_stderr_cb({data})")
        with self._lock:
            self._stderr.extend(data)

    def read_err(self):
        self._log.debug(f"read_err()")
        with self._lock:
            data = self._stderr.copy()
            self._stderr.clear()
        self._log.debug(f"read_err() returns {data}")
        return data

    def _terminated_cb(self, rc):
        self._log.debug(f"_terminated_cb({rc})")
        self.return_code = rc

    def start(self):
        self._log.debug(f"start()")
        self.process.start()

    def cleanup(self):
        self._log.debug(f"cleanup()")
        if self.process and self.process.running:
            self.process.terminate(kill_delay=0.1)

    def __exit__(self, __exc_type: typing.Type[BaseException], __exc_value: BaseException,
                 __traceback: types.TracebackType) -> bool:
        self._log.debug(f"__exit__({__exc_type}, {__exc_value}, {__traceback})")
        self.cleanup()
        return False


def replace_text_in_file(filename: str, text: str, replacement: str):
    # Read in the file
    with open(filename, 'r') as file:
        filedata = file.read()

    # Replace the target string
    filedata = filedata.replace(text, replacement)

    # Write the file out again
    with open(filename, 'w') as file:
        file.write(filedata)


class tempdir(object):
    """Sets the cwd within the context

    Args:
        path (Path): The path to the cwd
    """
    def __init__(self, cd: bool = False):
        self.cd = cd
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = self.tempdir.name
        self.origin = pathlib.Path().absolute()
        self.configfile = os.path.join(self.path, "config")

    def setup_files(self):
        shutil.copy(os.path.join(module_dir, "reticulum_test_config"), self.configfile)
        port1 = random.randint(30000, 65000)
        port2 = port1 + 1
        replace_text_in_file(self.configfile, "22222", str(port1))
        replace_text_in_file(self.configfile, "22223", str(port2))


    def __enter__(self):
        self.setup_files()
        if self.cd:
            os.chdir(self.path)

        return self.path

    def __exit__(self, exc, value, tb):
        if self.cd:
            os.chdir(self.origin)
        self.tempdir.__exit__(exc, value, tb)


def test_config_and_cleanup():
    td = None
    with tests.helpers.tempdir() as td:
        assert os.path.isfile(os.path.join(td, "config"))
        with open(os.path.join(td, "config"), 'r') as file:
            filedata = file.read()
        assert filedata.index("acehoss test config") > 0
        with pytest.raises(ValueError):
            filedata.index("22222")
    assert not os.path.exists(os.path.join(td, "config"))