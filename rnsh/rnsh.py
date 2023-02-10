#!/usr/bin/env python3
import functools
from typing import Callable

import termios

# MIT License
#
# Copyright (c) 2016-2022 Mark Qvist / unsigned.io
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import rnslogging
import RNS
import argparse
import shlex
import time
import sys
import os
import datetime
import base64
import process
import asyncio
import threading
import signal
import retry
from multiprocessing.pool import ThreadPool
from __version import __version__
import logging as __logging

module_logger = __logging.getLogger(__name__)


def _get_logger(name: str):
    global module_logger
    return module_logger.getChild(name)


APP_NAME = "rnsh"
_identity = None
_reticulum = None
_allow_all = False
_allowed_identity_hashes = []
_cmd: str | None = None
DATA_AVAIL_MSG = "data available"
_finished: asyncio.Future | None = None
_retry_timer = retry.RetryThread()
_destination: RNS.Destination | None = None
_pool: ThreadPool = ThreadPool(10)

async def _pump_int(timeout: float = 0):
    try:
        await asyncio.wait_for(_finished, timeout=timeout)
    except asyncio.exceptions.CancelledError:
        pass
    except TimeoutError:
        pass


def _handle_sigint_with_async_int(signal, frame):
    global _finished
    if _finished is not None:
        _finished.get_loop().call_soon_threadsafe(_finished.set_exception, KeyboardInterrupt())
    else:
        raise KeyboardInterrupt()


signal.signal(signal.SIGINT, _handle_sigint_with_async_int)


def _prepare_identity(identity_path):
    global _identity
    log = _get_logger("_prepare_identity")
    if identity_path is None:
        identity_path = RNS.Reticulum.identitypath + "/" + APP_NAME

    if os.path.isfile(identity_path):
        _identity = RNS.Identity.from_file(identity_path)

    if _identity is None:
        log.info("No valid saved identity found, creating new...")
        _identity = RNS.Identity()
        _identity.to_file(identity_path)


def _print_identity(configdir, identitypath, service_name, include_destination: bool):
    _reticulum = RNS.Reticulum(configdir=configdir, loglevel=RNS.LOG_INFO)
    _prepare_identity(identitypath)
    destination = RNS.Destination(_identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, service_name)
    print("Identity     : " + str(_identity))
    if include_destination:
        print("Listening on : " + RNS.prettyhexrep(destination.hash))
    exit(0)


async def _listen(configdir, command, identitypath=None, service_name="default", verbosity=0, quietness=0,
                  allowed=[], disable_auth=None, disable_announce=False):
    global _identity, _allow_all, _allowed_identity_hashes, _reticulum, _cmd, _destination
    log = _get_logger("_listen")
    _cmd = command

    targetloglevel = 3 + verbosity - quietness
    _reticulum = RNS.Reticulum(configdir=configdir, loglevel=targetloglevel)
    _prepare_identity(identitypath)
    _destination = RNS.Destination(_identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, service_name)

    if disable_auth:
        _allow_all = True
    else:
        if allowed is not None:
            for a in allowed:
                try:
                    dest_len = (RNS.Reticulum.TRUNCATED_HASHLENGTH // 8) * 2
                    if len(a) != dest_len:
                        raise ValueError(
                            "Allowed destination length is invalid, must be {hex} hexadecimal characters ({byte} bytes).".format(
                                hex=dest_len, byte=dest_len // 2))
                    try:
                        destination_hash = bytes.fromhex(a)
                        _allowed_identity_hashes.append(destination_hash)
                    except Exception as e:
                        raise ValueError("Invalid destination entered. Check your input.")
                except Exception as e:
                    log.error(str(e))
                    exit(1)

    if len(_allowed_identity_hashes) < 1 and not disable_auth:
        log.warning("Warning: No allowed identities configured, rnsh will not accept any connections!")

    _destination.set_link_established_callback(_listen_link_established)

    if not _allow_all:
        _destination.register_request_handler(
            path="data",
            response_generator=_listen_request,
            allow=RNS.Destination.ALLOW_LIST,
            allowed_list=_allowed_identity_hashes
        )
    else:
        _destination.register_request_handler(
            path="data",
            response_generator=_listen_request,
            allow=RNS.Destination.ALLOW_ALL,
        )

    await _pump_int()

    log.info("rnsh listening for commands on " + RNS.prettyhexrep(_destination.hash))

    if not disable_announce:
        _destination.announce()

    last = time.time()

    try:
        while True:
            if not disable_announce and time.time() - last > 900:  # TODO: make parameter
                last = datetime.datetime.now()
                _destination.announce()
            await _pump_int(1.0)
    except KeyboardInterrupt:
        log.warning("Shutting down")
        for link in list(_destination.links):
            try:
                if link.process is not None and link.process.process.running:
                    link.process.process.terminate()
            except:
                pass
        await asyncio.sleep(1)
        links_still_active = list(filter(lambda l: l.status != RNS.Link.CLOSED, _destination.links))
        for link in links_still_active:
            if link.status != RNS.Link.CLOSED:
                link.teardown()


class ProcessState:
    def __init__(self,
                 cmd: str,
                 mdu: int,
                 data_available_callback: callable,
                 terminated_callback: callable,
                 term: str | None,
                 loop: asyncio.AbstractEventLoop = None):

        self._log = _get_logger(self.__class__.__name__)
        self._mdu = mdu
        self._loop = loop if loop is not None else asyncio.get_running_loop()
        self._process = process.CallbackSubprocess(argv=shlex.split(cmd),
                                                   term=term,
                                                   loop=asyncio.get_running_loop(),
                                                   stdout_callback=self._stdout_data,
                                                   terminated_callback=terminated_callback)
        self._data_buffer = bytearray()
        self._lock = threading.RLock()
        self._data_available_cb = data_available_callback
        self._terminated_cb = terminated_callback
        self._pending_receipt: RNS.PacketReceipt | None = None
        self._process.start()
        self._term_state: [int] = None

    @property
    def mdu(self) -> int:
        return self._mdu

    @mdu.setter
    def mdu(self, val: int):
        self._mdu = val

    def pending_receipt_peek(self) -> RNS.PacketReceipt | None:
        return self._pending_receipt

    def pending_receipt_take(self) -> RNS.PacketReceipt | None:
        with self._lock:
            val = self._pending_receipt
            self._pending_receipt = None
            return val

    def pending_receipt_put(self, receipt: RNS.PacketReceipt | None):
        with self._lock:
            self._pending_receipt = receipt

    @property
    def process(self) -> process.CallbackSubprocess:
        return self._process

    @property
    def return_code(self) -> int | None:
        return self.process.return_code

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def read(self, count: int) -> bytes:
        with self.lock:
            take = self._data_buffer[:count - 1]
            self._data_buffer = self._data_buffer[count:]
            return take

    def _stdout_data(self, data: bytes):
        with self.lock:
            self._data_buffer.extend(data)
            total_available = len(self._data_buffer)
        try:
            self._data_available_cb(total_available)
        except Exception as e:
            self._log.error(f"Error calling ProcessState data_available_callback {e}")

    TERMSTATE_IDX_TERM = 0
    TERMSTATE_IDX_TIOS = 1
    TERMSTATE_IDX_ROWS = 2
    TERMSTATE_IDX_COLS = 3
    TERMSTATE_IDX_HPIX = 4
    TERMSTATE_IDX_VPIX = 5

    def _update_winsz(self):
        self.process.set_winsize(self._term_state[ProcessState.TERMSTATE_IDX_ROWS],
                                 self._term_state[ProcessState.TERMSTATE_IDX_COLS],
                                 self._term_state[ProcessState.TERMSTATE_IDX_HPIX],
                                 self._term_state[ProcessState.TERMSTATE_IDX_VPIX])

    REQUEST_IDX_STDIN = 0
    REQUEST_IDX_TERM = 1
    REQUEST_IDX_TIOS = 2
    REQUEST_IDX_ROWS = 3
    REQUEST_IDX_COLS = 4
    REQUEST_IDX_HPIX = 5
    REQUEST_IDX_VPIX = 6

    @staticmethod
    def default_request(stdin_fd: int | None) -> [any]:
        request = [
            None,  # Stdin
            None,  # TERM variable
            None,  # termios attributes or something
            None,  # terminal rows
            None,  # terminal cols
            None,  # terminal horizontal pixels
            None,  # terminal vertical pixels
        ]
        if stdin_fd is not None:
            request[ProcessState.REQUEST_IDX_TERM] = os.environ.get("TERM", None)
            request[ProcessState.REQUEST_IDX_TIOS] = termios.tcgetattr(stdin_fd)
            request[ProcessState.REQUEST_IDX_ROWS], \
            request[ProcessState.REQUEST_IDX_COLS], \
            request[ProcessState.REQUEST_IDX_HPIX], \
            request[ProcessState.REQUEST_IDX_VPIX] = process.tty_get_winsize(stdin_fd)
        return request

    def process_request(self, data: [any], read_size: int) -> [any]:
        stdin = data[ProcessState.REQUEST_IDX_STDIN]  # Data passed to stdin
        # term = data[ProcessState.REQUEST_IDX_TERM]  # TERM environment variable
        # tios = data[ProcessState.REQUEST_IDX_TIOS]  # termios attr
        # rows = data[ProcessState.REQUEST_IDX_ROWS]  # window rows
        # cols = data[ProcessState.REQUEST_IDX_COLS]  # window cols
        # hpix = data[ProcessState.REQUEST_IDX_HPIX]  # window horizontal pixels
        # vpix = data[ProcessState.REQUEST_IDX_VPIX]  # window vertical pixels
        term_state = data[ProcessState.REQUEST_IDX_ROWS:ProcessState.REQUEST_IDX_VPIX]
        response = ProcessState.default_response()

        response[ProcessState.RESPONSE_IDX_RUNNING] = not self.process.running
        if self.process.running:
            if term_state != self._term_state:
                self._term_state = term_state
                self._update_winsz()
            if stdin is not None and len(stdin) > 0:
                stdin = base64.b64decode(stdin)
                self.process.write(stdin)
        response[ProcessState.RESPONSE_IDX_RETCODE] = self.return_code
        stdout = self.read(read_size)
        with self.lock:
            response[ProcessState.RESPONSE_IDX_RDYBYTE] = len(self._data_buffer)
        response[ProcessState.RESPONSE_IDX_STDOUT] = \
            base64.b64encode(stdout).decode("utf-8") if stdout is not None and len(stdout) > 0 else None
        return response

    RESPONSE_IDX_RUNNING = 0
    RESPONSE_IDX_RETCODE = 1
    RESPONSE_IDX_RDYBYTE = 2
    RESPONSE_IDX_STDOUT = 3
    RESPONSE_IDX_TMSTAMP = 4

    @staticmethod
    def default_response() -> [any]:
        return [
            False,        # 0: Process running
            None,         # 1: Return value
            0,            # 2: Number of outstanding bytes
            None,         # 3: Stdout/Stderr
            time.time(),  # 4: Timestamp
        ]


def _subproc_data_ready(link: RNS.Link, chars_available: int):
    global _retry_timer
    log = _get_logger("_subproc_data_ready")
    process_state: ProcessState = link.process

    def send(timeout: bool, tag: any, tries: int) -> any:
        try:
            pr = process_state.pending_receipt_take()
            if pr is not None and pr.get_status() != RNS.PacketReceipt.SENT and pr.get_status() != RNS.PacketReceipt.DELIVERED:
                if not timeout:
                    _retry_timer.complete(tag)
                log.debug(f"Notification completed with status {pr.status} on link {link}")
                return link.link_id

            if not timeout:
                log.info(
                    f"Notifying client try {tries} (retcode: {process_state.return_code} chars avail: {chars_available})")
                packet = RNS.Packet(link, DATA_AVAIL_MSG.encode("utf-8"))
                packet.send()
                pr = packet.receipt
                process_state.pending_receipt_put(pr)
                return link.link_id
            else:
                log.error(f"Retry count exceeded, terminating link {link}")
                _retry_timer.complete(link.link_id)
                link.teardown()
        except Exception as e:
            log.error("Error notifying client: " + str(e))
        return link.link_id

    with process_state.lock:
        if process_state.pending_receipt_peek() is None:
            _retry_timer.begin(try_limit=15,
                               wait_delay=link.rtt * 3 if link.rtt is not None else 1,
                               try_callback=functools.partial(send, False),
                               timeout_callback=functools.partial(send, True),
                               tag=None)
        else:
            log.debug(f"Notification already pending for link {link}")


def _subproc_terminated(link: RNS.Link, return_code: int):
    log = _get_logger("_subproc_terminated")
    log.info(f"Subprocess terminated ({return_code} for link {link}")
    link.teardown()


def _listen_start_proc(link: RNS.Link, term: str) -> ProcessState | None:
    global _cmd
    log = _get_logger("_listen_start_proc")
    try:
        link.process = ProcessState(cmd=_cmd,
                                    term=term,
                                    data_available_callback=functools.partial(_subproc_data_ready, link),
                                    terminated_callback=functools.partial(_subproc_terminated, link))
        return link.process
    except Exception as e:
        log.error("Failed to launch process: " + str(e))
        link.teardown()
    return None


def _listen_link_established(link):
    global _allow_all
    log = _get_logger("_listen_link_established")
    link.set_remote_identified_callback(_initiator_identified)
    link.set_link_closed_callback(_listen_link_closed)
    log.info("Link " + str(link) + " established")


def _listen_link_closed(link: RNS.Link):
    log = _get_logger("_listen_link_closed")
    # async def cleanup():
    log.info("Link " + str(link) + " closed")
    proc: ProcessState | None = link.proc if hasattr(link, "process") else None
    if proc is None:
        log.warning(f"No process for link {link}")
    try:
        proc.process.terminate()
    except:
        log.error(f"Error closing process for link {link}")
    # asyncio.get_running_loop().call_soon(cleanup)


def _initiator_identified(link, identity):
    global _allow_all, _cmd
    log = _get_logger("_initiator_identified")
    log.info("Initiator of link " + str(link) + " identified as " + RNS.prettyhexrep(identity.hash))
    if not _allow_all and not identity.hash in _allowed_identity_hashes:
        log.warning("Identity " + RNS.prettyhexrep(identity.hash) + " not allowed, tearing down link", RNS.LOG_WARNING)
        link.teardown()


def _listen_request(path, data, request_id, link_id, remote_identity, requested_at):
    global _destination, _retry_timer
    log = _get_logger("_listen_request")
    log.debug(f"listen_execute {path} {request_id} {link_id} {remote_identity}, {requested_at}")
    _retry_timer.complete(link_id)
    link: RNS.Link = next(filter(lambda l: l.link_id == link_id, _destination.links))
    if link is None:
        log.error(f"invalid request {request_id}, no link found with id {link_id}")
        return
    process_state: ProcessState | None = None
    try:
        term = data[ProcessState.REQUEST_IDX_TERM]
        process_state = link.process if hasattr(link, "process") else None
        if process_state is None:
            log.debug(f"process not found for link {link}")
            process_state = _listen_start_proc(link, term)

        # leave significant headroom for metadata and encoding
        result = process_state.process_request(data, link.MDU * 3 // 2)
    except Exception as e:
        result = ProcessState.default_response()
        try:
            if process_state is not None:
                process_state.process.terminate()
                link.teardown()
        except Exception as e:
            log.error(f"Error terminating process for link {link}")

    return result


async def _spin(until: Callable | None = None, timeout: float | None = None) -> bool:
    global _pool
    if timeout is not None:
        timeout += time.time()

    while (timeout is None or time.time() < timeout) and not until():
        await _pump_int(0.01)
    if timeout is not None and time.time() > timeout:
        return False
    else:
        return True

_link: RNS.Link | None = None
_remote_exec_grace = 2.0
_new_data: asyncio.Event | None = None
_tr = process.TtyRestorer(sys.stdin.fileno())


def _client_packet_handler(message, packet):
    global _new_data
    log = _get_logger("_client_packet_handler")
    if message is not None and message.decode("utf-8") == DATA_AVAIL_MSG and _new_data is not None:
        _new_data.set()
    else:
        log.error(f"received unhandled packet")


async def _execute(configdir, identitypath=None, verbosity=0, quietness=0, noid=False, destination=None,
                   service_name="default", stdin=None, timeout=RNS.Transport.PATH_REQUEST_TIMEOUT):
    global _identity, _reticulum, _link, _destination, _remote_exec_grace, _tr
    log = _get_logger("_execute")

    dest_len = (RNS.Reticulum.TRUNCATED_HASHLENGTH // 8) * 2
    if len(destination) != dest_len:
        raise ValueError(
            "Allowed destination length is invalid, must be {hex} hexadecimal characters ({byte} bytes).".format(
                hex=dest_len, byte=dest_len // 2))
    try:
        destination_hash = bytes.fromhex(destination)
    except Exception as e:
        raise ValueError("Invalid destination entered. Check your input.")

    if _reticulum is None:
        targetloglevel = 2 + verbosity - quietness
        _reticulum = RNS.Reticulum(configdir=configdir, loglevel=targetloglevel)

    if _identity is None:
        _prepare_identity(identitypath)

    if not RNS.Transport.has_path(destination_hash):
        RNS.Transport.request_path(destination_hash)
        log.info(f"Requesting path...")
        if not await _spin(until=lambda: RNS.Transport.has_path(destination_hash), timeout=timeout):
            raise Exception("Path not found")

    if _destination is None:
        listener_identity = RNS.Identity.recall(destination_hash)
        _destination = RNS.Destination(
            listener_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            APP_NAME,
            service_name
        )

    if _link is None or _link.status == RNS.Link.PENDING:
        _link = RNS.Link(_destination)
        _link.did_identify = False

    log.info(f"Establishing link...")
    if not await _spin(until=lambda: _link.status == RNS.Link.ACTIVE, timeout=timeout):
        raise Exception("Could not establish link with " + RNS.prettyhexrep(destination_hash))

    if not noid and not _link.did_identify:
        _link.identify(_identity)
        _link.did_identify = True

    _link.set_packet_callback(_client_packet_handler)

    request = ProcessState.default_request(sys.stdin.fileno())
    request[ProcessState.REQUEST_IDX_STDIN] = (base64.b64encode(stdin) if stdin is not None else None)

    # TODO: Tune
    timeout = timeout + _link.rtt * 4 + _remote_exec_grace

    request_receipt = _link.request(
        path="data",
        data=request,
        timeout=timeout
    )
    timeout += 0.5

    await _spin(
        until=lambda: _link.status == RNS.Link.CLOSED or (
                request_receipt.status != RNS.RequestReceipt.FAILED and request_receipt.status != RNS.RequestReceipt.SENT),
        timeout=timeout
    )

    if _link.status == RNS.Link.CLOSED:
        raise Exception("Could not request remote execution, link was closed")

    if request_receipt.status == RNS.RequestReceipt.FAILED:
        raise Exception("Could not request remote execution")

    await _spin(
        until=lambda: request_receipt.status != RNS.RequestReceipt.DELIVERED,
        timeout=timeout
    )

    if request_receipt.status == RNS.RequestReceipt.FAILED:
        raise Exception("No result was received")

    if request_receipt.status == RNS.RequestReceipt.FAILED:
        raise Exception("Receiving result failed")

    if request_receipt.response is not None:
        try:
            running     = request_receipt.response[ProcessState.RESPONSE_IDX_RUNNING]
            return_code = request_receipt.response[ProcessState.RESPONSE_IDX_RETCODE]
            ready_bytes = request_receipt.response[ProcessState.RESPONSE_IDX_RDYBYTE]
            stdout      = request_receipt.response[ProcessState.RESPONSE_IDX_STDOUT]
            timestamp   = request_receipt.response[ProcessState.RESPONSE_IDX_TMSTAMP]
            # log.debug("data: " + (stdout.decode("utf-8") if stdout is not None else ""))
        except Exception as e:
            raise Exception(f"Received invalid response") from e

        _tr.raw()
        if stdout is not None:
            stdout = base64.b64decode(stdout)
            # log.debug(f"stdout: {stdout}")
            os.write(sys.stdout.fileno(), stdout)

        sys.stdout.flush()
        sys.stderr.flush()

        if not running and return_code is not None:
            return return_code

        return None


async def _initiate(configdir: str, identitypath: str, verbosity: int, quietness: int, noid: bool, destination: str,
                    service_name: str, timeout: float):
    global _new_data, _finished, _tr
    log = _get_logger("_initiate")
    loop = asyncio.get_event_loop()
    _new_data = asyncio.Event()

    def stdout(data: bytes):
        # log.debug(f"stdout {data}")
        os.write(sys.stdout.fileno(), data)

    def terminated(rc: int):
        # log.debug(f"terminated {rc}")
        return_code.set_result(rc)

    data_buffer = bytearray()

    def sigint_handler(signal, frame):
        log.debug("KeyboardInterrupt")
        data_buffer.extend("\x03".encode("utf-8"))

    def sigwinch_handler(signal, frame):
        # log.debug("WindowChanged")
        if _new_data is not None:
            _new_data.set()

    def stdin():
        data = process.tty_read(sys.stdin.fileno())
        # log.debug(f"stdin {data}")
        if data is not None:
                data_buffer.extend(data)

    process.tty_add_reader_callback(sys.stdin.fileno(), stdin)

    await _pump_int()
    signal.signal(signal.SIGWINCH, sigwinch_handler)
    while True:
        stdin = data_buffer.copy()
        data_buffer.clear()
        _new_data.clear()

        return_code = await _execute(
            configdir=configdir,
            identitypath=identitypath,
            verbosity=verbosity,
            quietness=quietness,
            noid=noid,
            destination=destination,
            service_name=service_name,
            stdin=stdin,
            timeout=timeout,
        )
        signal.signal(signal.SIGINT, sigint_handler)
        if return_code is not None:
            _link.teardown()
            return return_code

        await process.event_wait(_new_data, 5)


# def _print_help():
#     # retrieve subparsers from parser
#     subparsers_actions = [
#         action for action in parser._actions
#         if isinstance(action, argparse._SubParsersAction)]
#     # there will probably only be one subparser_action,
#     # but better safe than sorry
#     for subparsers_action in subparsers_actions:
#         # get all subparsers and print help
#         for choice, subparser in subparsers_action.choices.items():
#             print("Subparser '{}'".format(choice))
#             print(subparser.format_help())
#     return 0

import docopt
import json


async def main():
    global _tr, _finished
    log = _get_logger("main")
    _finished = asyncio.get_running_loop().create_future()
    usage = '''
Usage:
    rnsh [--config <configfile>] [-i <identityfile>] [-s <service_name>] [-l] -p
    rnsh -l [--config <configfile>] [-i <identityfile>] [-s <service_name>] [-v...] [-q...] [-b] 
         (-n | -a <identity_hash> [-a <identity_hash>]...) <program> [<arg>...]
    rnsh [--config <configfile>] [-i <identityfile>] [-s <service_name>] [-v...] [-q...] [-N] [-m]
         [-w <timeout>] <destination_hash>
    rnsh -h
    rnsh --version

Options:
    --config FILE            Alternate Reticulum config file to use
    -i FILE --identity FILE  Specific identity file to use
    -s NAME --service NAME   Listen on/connect to specific service name if not default
    -p --print-identity      Print identity information and exit
    -l --listen              Listen (server) mode
    -b --no-announce         Do not announce service
    -a HASH --allowed HASH   Specify identities allowed to connect
    -n --no-auth             Disable authentication
    -N --no-id               Disable identify on connect
    -m --mirror              Client returns with code of remote process
    -w TIME --timeout TIME   Specify client connect and request timeout in seconds
    -v --verbose             Increase verbosity
    -q --quiet               Increase quietness
    --version                Show version
    -h --help                Show this help
    '''
    args = docopt.docopt(usage, version=f"rnsh {__version__}")
    # json.dump(args, sys.stdout)

    args_service_name = args.get("--service", None) or "default"
    args_listen = args.get("--listen", None) or False
    args_identity = args.get("--identity", None)
    args_config = args.get("--config", None)
    args_print_identity = args.get("--print-identity", None) or False
    args_verbose = args.get("--verbose", None) or 0
    args_quiet = args.get("--quiet", None) or 0
    args_no_announce = args.get("--no-announce", None) or False
    args_no_auth = args.get("--no-auth", None) or False
    args_allowed = args.get("--allowed", None) or []
    args_program = args.get("<program>", None)
    args_program_args = args.get("<arg>", None) or []
    args_no_id = args.get("--no-id", None) or False
    args_mirror = args.get("--mirror", None) or False
    args_timeout = args.get("--timeout", None) or RNS.Transport.PATH_REQUEST_TIMEOUT
    args_destination = args.get("<destination_hash>", None)
    args_help = args.get("--help", None) or False

    if args_help:
        return 0

    if args_print_identity:
        _print_identity(args_config, args_identity, args_service_name, args_listen)
        return 0

    if args_listen:
        # log.info("command " + args.command)
        await _listen(
            configdir=args_config,
            command=[args_program].extend(args_program_args),
            identitypath=args_identity,
            service_name=args_service_name,
            verbosity=args_verbose,
            quietness=args_quiet,
            allowed=args_allowed,
            disable_auth=args_no_auth,
            disable_announce=args_no_announce,
        )

    if args_destination is not None and args_service_name is not None:
        try:
            return_code = await _initiate(
                configdir=args_config,
                identitypath=args_identity,
                verbosity=args_verbose,
                quietness=args_quiet,
                noid=args_no_id,
                destination=args_destination,
                service_name=args_service_name,
                timeout=args_timeout,
            )
            return return_code if args_mirror else 0
        except:
            _tr.restore()
            raise
    else:
        print("")
        print(args)
        print("")


if __name__ == "__main__":
    return_code = 1
    try:
        return_code = asyncio.run(main())
    finally:
        try:
            process.tty_unset_reader_callbacks(sys.stdin.fileno())
        except:
            pass
        _tr.restore()
        _pool.close()
        _retry_timer.close()
    sys.exit(return_code)
