import asyncio
import errno
import functools
import signal
import struct
import threading
import time
import tty
import pty
import io
import os
import asyncio
import subprocess
import typing
import sys

import logging as __logging

import fcntl
import select
import termios

module_logger = __logging.getLogger(__name__)

# _tattrs: dict[int, list] = {}
# # _fl: dict[int, int] = {}
#
# def tty_set_now(fd: int):
#     global _tattrs
#     # fl = fcntl.fcntl(fd, fcntl.F_GETFL)
#     # module_logger.debug(f"fl {fd} {fl:032b} {fl}")
#     _tattrs[fd] = termios.tcgetattr(fd)
#     # _fl[fd] = fl
#     # termios.tcsetattr(fd, termios.TCSANOW, termios.)
#     # tty.setcbreak(fd, termios.TCSANOW)
#     # tty.setraw(fd)
#     # fcntl.fcntl(fd, fcntl.F_SETFL, fl & ~(termios.TCSADRAIN | termios.TCSAFLUSH))
#     # module_logger.debug(f"fl {fd} {fl:032b} {fl}")
#
# def tty_reset(fd: int):
#     global _tattrs
#     tattr = _tattrs.get(fd)
#     if tattr is not None:
#         termios.tcsetattr(fd, termios.TCSANOW, tattr)
#
#     # fl = _fl.get(fd)
#     # if fl is not None:
#     #     fcntl.fcntl(fd, fcntl.F_SETFL, fl)

def tty_set_callback(fd: int, callback: callable, loop: asyncio.AbstractEventLoop | None = None):
    if loop is None:
        loop = asyncio.get_running_loop()
    loop.add_reader(fd, callback)

def tty_read(fd: int) -> bytes | None:
    if fd_is_closed(fd):
        return None

    run = True
    result = bytearray()
    while run and not fd_is_closed(fd):
        ready, _, _ = select.select([fd], [], [], 0)
        if len(ready) == 0:
            break
        for f in ready:
            try:
                data = os.read(fd, 512)
            except OSError as e:
                if e.errno != errno.EIO and e.errno != errno.EWOULDBLOCK:
                    raise
            else:
                if not data:  # EOF
                    run = False
                if data is not None and len(data) > 0:
                    result.extend(data)
    return result

def fd_is_closed(fd: int) -> bool:
    try:
        fcntl.fcntl(fd, fcntl.F_GETFL) < 0
    except OSError as ose:
        return ose.errno == errno.EBADF

def tty_unset_callbacks(fd: int, loop: asyncio.AbstractEventLoop | None = None):
    try:
        if loop is None:
            loop = asyncio.get_running_loop()
        loop.remove_reader(fd)
    except:
        pass

def tty_get_size(fd: int) -> [int, int, int ,int]:
    packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack('HHHH', 0, 0, 0, 0))
    rows, cols, h_pixels, v_pixels = struct.unpack('HHHH', packed)
    return rows, cols, h_pixels, v_pixels

def tty_set_size(fd: int, rows: int, cols: int, h_pixels: int, v_pixels: int):
    packed = struct.pack('HHHH', rows, cols, h_pixels, v_pixels)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)

def process_exists(pid):
    """ Check For the existence of a unix pid. """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True

class TtyRestorer:
    def __init__(self, fd: int):
        self._fd = fd
        self._tattr = termios.tcgetattr(self._fd)

    def raw(self):
        tty.setraw(self._fd, termios.TCSADRAIN)

    def restore(self):
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._tattr)

class CallbackSubprocess:

    PROCESS_POLL_TIME: float = 0.1

    def __init__(self, command: [str], term: str, loop: asyncio.AbstractEventLoop, stdout_callback: callable,
                terminated_callback: callable):

        assert loop is not None, "loop should not be None"
        assert stdout_callback is not None, "stdout_callback should not be None"
        assert terminated_callback is not None, "terminated_callback should not be None"

        self.log = module_logger.getChild(self.__class__.__name__)
        self.log.debug(f"__init__({command},{term},...")
        self._command = command
        self._term = term
        self._loop = loop
        self._stdout_cb = stdout_callback
        self._terminated_cb = terminated_callback
        self._pid: int | None = None

    def terminate(self):
        self.log.debug("terminate()")
        try:
            os.kill(self._pid, signal.SIGTERM)
        except:
            pass

        def kill():
            self.log.debug("kill()")
            try:
                os.kill(self._pid, signal.SIGHUP)
                os.kill(self._pid, signal.SIGKILL)
            except:
                pass

        self._loop.call_later(1, kill)

        def wait():
            self.log.debug("wait()")
            os.waitpid(self._pid, 0)
            self.log.debug("wait() finish")

        threading.Thread(target=wait).start()

    @property
    def started(self) -> bool:
        return self._pid is not None

    @property
    def running(self) -> bool:
        return self._pid is not None and process_exists(self._pid)

    def write(self, data: bytes):
        self.log.debug(f"write({data})")
        os.write(self._si, data)

    def set_winsize(self, r: int, c: int, h: int, w: int):
        self.log.debug(f"set_winsize({r},{c},{h},{w}")
        tty_set_size(self._si, r, c, h, w)

    def copy_winsize(self, fromfd:int):
        r,c,h,w = tty_get_size(fromfd)
        self.set_winsize(r,c,w,h)

    # def tcsetattr(self, val: list[int | list[int | bytes]]):
    #     termios.tcsetattr(self._si, termios.TCSANOW, val)

    def start(self):
        self.log.debug("start()")
        parentenv = os.environ.copy()
        env = {"HOME": parentenv["HOME"],
               "TERM": self._term if self._term is not None else parentenv.get("TERM", "xterm"),
               "LANG": parentenv.get("LANG"),
               "SHELL": self._command[0]}

        self._pid, self._si = pty.fork()

        if self._pid == 0:
            try:
                os.setpgrp()
                os.execvpe(self._command[0], self._command, env)
            except Exception as err:
                print(f"Child process error {err}")
            sys.exit(0)

        def poll():
            # self.log.debug("poll")
            try:
                pid, rc = os.waitpid(self._pid, os.WNOHANG)
                if rc is not None and not process_exists(self._pid):
                    self.log.debug(f"polled return code {rc}")
                    self._terminated_cb(rc)
                self._loop.call_later(CallbackSubprocess.PROCESS_POLL_TIME, poll)
            except Exception as e:
                self.log.debug(f"Error in process poll: {e}")
        self._loop.call_later(CallbackSubprocess.PROCESS_POLL_TIME, poll)

        def reader(fd: int, callback: callable):
            result = bytearray()
            try:
                c = tty_read(fd)
                if c is not None and len(c) > 0:
                    callback(c)
            except:
                pass

        tty_set_callback(self._si, functools.partial(reader, self._si, self._stdout_cb), self._loop)

async def main():
    import testlogging

    log = module_logger.getChild("main")
    if len(sys.argv) <= 1:
        print("no cmd")
        exit(1)

    loop = asyncio.get_event_loop()
    # asyncio.set_event_loop(loop)
    retcode = loop.create_future()

    def stdout(data: bytes):
        # log.debug("stdout")
        os.write(sys.stdout.fileno(), data)
        # sys.stdout.flush()

    def terminated(rc: int):
        # log.debug(f"terminated {rc}")
        retcode.set_result(rc)

    process = CallbackSubprocess(sys.argv[1:], os.environ.get("TERM", "xterm"), loop, stdout, terminated)

    def sigint_handler(signal, frame):
        # log.debug("KeyboardInterrupt")
        if process is None or process.started and not process.running:
            tr.restore()
            raise KeyboardInterrupt
        elif process.running:
            process.write("\x03".encode("utf-8"))

    def sigwinch_handler(signal, frame):
        # log.debug("WindowChanged")
        process.copy_winsize(sys.stdin.fileno())

    signal.signal(signal.SIGINT, sigint_handler)
    signal.signal(signal.SIGWINCH, sigwinch_handler)

    def stdin():
        data = tty_read(sys.stdin.fileno())
        # log.debug(f"stdin {data}")
        if data is not None:
            process.write(data)
            # sys.stdout.buffer.write(data)

    tty_set_callback(sys.stdin.fileno(), stdin)
    process.start()
    # process.tcsetattr(termios.tcgetattr(sys.stdin))

    val = await retcode
    log.debug(f"got retcode {val}")
    return val

if __name__ == "__main__":
    tr = TtyRestorer(sys.stdin.fileno())
    try:
        tr.raw()
        asyncio.run(main())
    finally:
        tty_unset_callbacks(sys.stdin.fileno())
        tr.restore()