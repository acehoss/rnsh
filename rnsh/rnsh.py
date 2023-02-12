#!/usr/bin/env python3

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

from __future__ import annotations

import asyncio
import base64
import functools
import logging as __logging
import os
import signal
import sys
import termios
import threading
import time
from typing import Callable, TypeVar
import RNS
import rnsh.exception as exception
import rnsh.process as process
import rnsh.retry as retry
import rnsh.rnslogging as rnslogging
import rnsh.hacks as hacks
from rnsh.__version import __version__

module_logger = __logging.getLogger(__name__)


def _get_logger(name: str):
    global module_logger
    return module_logger.getChild(name)


APP_NAME = "rnsh"
_identity = None
_reticulum = None
_allow_all = False
_allowed_identity_hashes = []
_cmd: [str] = None
DATA_AVAIL_MSG = "data available"
_finished: asyncio.Event | None = None
_retry_timer: retry.RetryThread | None = None
_destination: RNS.Destination | None = None
_loop: asyncio.AbstractEventLoop | None = None


async def _check_finished(timeout: float = 0):
    await process.event_wait(_finished, timeout=timeout)


def _sigint_handler(sig, frame):
    global _finished
    log = _get_logger("_sigint_handler")
    log.debug("SIGINT")
    if _finished is not None:
        _finished.set()
    else:
        raise KeyboardInterrupt()


signal.signal(signal.SIGINT, _sigint_handler)


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
    global _reticulum
    _reticulum = RNS.Reticulum(configdir=configdir, loglevel=RNS.LOG_INFO)
    _prepare_identity(identitypath)
    destination = RNS.Destination(_identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, service_name)
    print("Identity     : " + str(_identity))
    if include_destination:
        print("Listening on : " + RNS.prettyhexrep(destination.hash))
    exit(0)


# hack_goes_here

async def _listen(configdir, command, identitypath=None, service_name="default", verbosity=0, quietness=0,
                  allowed=None, disable_auth=None, announce_period=900):
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
                            "Allowed destination length is invalid, must be {hex} hexadecimal " +
                            "characters ({byte} bytes).".format(
                                hex=dest_len, byte=dest_len // 2))
                    try:
                        destination_hash = bytes.fromhex(a)
                        _allowed_identity_hashes.append(destination_hash)
                    except Exception:
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
            response_generator=hacks.request_request_id_hack(_listen_request, asyncio.get_running_loop()),
            allow=RNS.Destination.ALLOW_LIST,
            allowed_list=_allowed_identity_hashes
        )
    else:
        _destination.register_request_handler(
            path="data",
            response_generator=hacks.request_request_id_hack(_listen_request, asyncio.get_running_loop()),
            allow=RNS.Destination.ALLOW_ALL,
        )

    await _check_finished()

    log.info("rnsh listening for commands on " + RNS.prettyhexrep(_destination.hash))

    if announce_period is not None:
        _destination.announce()

    last = time.time()

    try:
        while True:
            if announce_period and 0 < announce_period < time.time() - last:
                last = time.time()
                _destination.announce()
            await _check_finished(1.0)
    except KeyboardInterrupt:
        log.warning("Shutting down")
        for link in list(_destination.links):
            with exception.permit(SystemExit):
                proc = Session.get_for_tag(link.link_id)
                if proc is not None and proc.process.running:
                    proc.process.terminate()
        await asyncio.sleep(1)
        links_still_active = list(filter(lambda l: l.status != RNS.Link.CLOSED, _destination.links))
        for link in links_still_active:
            if link.status != RNS.Link.CLOSED:
                link.teardown()


_PROTOCOL_MAGIC = 0xdeadbeef


def _protocol_make_version(version: int):
    return (_PROTOCOL_MAGIC << 32) & 0xffffffff00000000 | (0xffffffff & version)


_PROTOCOL_VERSION_0 = _protocol_make_version(0)


def _protocol_split_version(version: int):
    return (version >> 32) & 0xffffffff, version & 0xffffffff


def _protocol_check_magic(value: int):
    return _protocol_split_version(value)[0] == _PROTOCOL_MAGIC


class Session:
    _processes: [(any, Session)] = []
    _lock = threading.RLock()

    @classmethod
    def get_for_tag(cls, tag: any) -> Session | None:
        with cls._lock:
            return next(map(lambda p: p[1], filter(lambda p: p[0] == tag, cls._processes)), None)

    @classmethod
    def put_for_tag(cls, tag: any, ps: Session):
        with cls._lock:
            cls.clear_tag(tag)
            cls._processes.append((tag, ps))

    @classmethod
    def clear_tag(cls, tag: any):
        with cls._lock:
            with exception.permit(SystemExit):
                cls._processes.remove(tag)

    def __init__(self,
                 tag: any,
                 cmd: [str],
                 mdu: int,
                 data_available_callback: callable,
                 terminated_callback: callable,
                 term: str | None,
                 remote_identity: str | None,
                 loop: asyncio.AbstractEventLoop = None):

        self._log = _get_logger(self.__class__.__name__)
        self._mdu = mdu
        self._loop = loop if loop is not None else asyncio.get_running_loop()
        self._process = process.CallbackSubprocess(argv=cmd,
                                                   env={"TERM": term or os.environ.get("TERM", None),
                                                        "RNS_REMOTE_IDENTITY": remote_identity or ""},
                                                   loop=loop,
                                                   stdout_callback=self._stdout_data,
                                                   terminated_callback=terminated_callback)
        self._data_buffer = bytearray()
        self._lock = threading.RLock()
        self._data_available_cb = data_available_callback
        self._terminated_cb = terminated_callback
        self._pending_receipt: RNS.PacketReceipt | None = None
        self._process.start()
        self._term_state: [int] = None
        Session.put_for_tag(tag, self)

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
            initial_len = len(self._data_buffer)
            take = self._data_buffer[:count]
            self._data_buffer = self._data_buffer[count:].copy()
            self._log.debug(f"read {len(take)} bytes of {initial_len}, {len(self._data_buffer)} remaining")
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
        try:
            self.process.set_winsize(self._term_state[1],
                                     self._term_state[2],
                                     self._term_state[3],
                                     self._term_state[4])
        except Exception as e:
            self._log.debug(f"failed to update winsz: {e}")

    REQUEST_IDX_VERS = 0
    REQUEST_IDX_STDIN = 1
    REQUEST_IDX_TERM = 2
    REQUEST_IDX_TIOS = 3
    REQUEST_IDX_ROWS = 4
    REQUEST_IDX_COLS = 5
    REQUEST_IDX_HPIX = 6
    REQUEST_IDX_VPIX = 7

    @staticmethod
    def default_request(stdin_fd: int | None) -> [any]:
        global _PROTOCOL_VERSION_0
        request: list[any] = [
            _PROTOCOL_VERSION_0,  # 0 Protocol Version
            None,                 # 1 Stdin
            None,                 # 2 TERM variable
            None,                 # 3 termios attributes or something
            None,                 # 4 terminal rows
            None,                 # 5 terminal cols
            None,                 # 6 terminal horizontal pixels
            None,                 # 7 terminal vertical pixels
        ].copy()

        if stdin_fd is not None:
            request[Session.REQUEST_IDX_TERM] = os.environ.get("TERM", None)
            request[Session.REQUEST_IDX_TIOS] = termios.tcgetattr(stdin_fd)
            request[Session.REQUEST_IDX_ROWS], \
                request[Session.REQUEST_IDX_COLS], \
                request[Session.REQUEST_IDX_HPIX], \
                request[Session.REQUEST_IDX_VPIX] = process.tty_get_winsize(stdin_fd)
        return request

    def process_request(self, data: [any], read_size: int) -> [any]:
        stdin = data[Session.REQUEST_IDX_STDIN]  # Data passed to stdin
        # term = data[ProcessState.REQUEST_IDX_TERM]  # TERM environment variable
        # tios = data[ProcessState.REQUEST_IDX_TIOS]  # termios attr
        # rows = data[ProcessState.REQUEST_IDX_ROWS]  # window rows
        # cols = data[ProcessState.REQUEST_IDX_COLS]  # window cols
        # hpix = data[ProcessState.REQUEST_IDX_HPIX]  # window horizontal pixels
        # vpix = data[ProcessState.REQUEST_IDX_VPIX]  # window vertical pixels
        # term_state = data[ProcessState.REQUEST_IDX_ROWS:ProcessState.REQUEST_IDX_VPIX+1]
        response = Session.default_response()
        term_state = data[Session.REQUEST_IDX_TIOS:Session.REQUEST_IDX_VPIX + 1]

        response[Session.RESPONSE_IDX_RUNNING] = self.process.running
        if self.process.running:
            if term_state != self._term_state:
                self._term_state = term_state
                self._update_winsz()
            if stdin is not None and len(stdin) > 0:
                stdin = base64.b64decode(stdin)
                self.process.write(stdin)
        response[Session.RESPONSE_IDX_RETCODE] = None if self.process.running else self.return_code

        with self.lock:
            stdout = self.read(read_size)
            response[Session.RESPONSE_IDX_RDYBYTE] = len(self._data_buffer)

        if stdout is not None and len(stdout) > 0:
            response[Session.RESPONSE_IDX_STDOUT] = base64.b64encode(stdout).decode("utf-8")
        return response

    RESPONSE_IDX_VERSION = 0
    RESPONSE_IDX_RUNNING = 1
    RESPONSE_IDX_RETCODE = 2
    RESPONSE_IDX_RDYBYTE = 3
    RESPONSE_IDX_STDOUT = 4
    RESPONSE_IDX_TMSTAMP = 5

    @staticmethod
    def default_response() -> [any]:
        global _PROTOCOL_VERSION_0
        response: list[any] = [
            _PROTOCOL_VERSION_0,  # 0: Protocol version
            False,                # 1: Process running
            None,                 # 2: Return value
            0,                    # 3: Number of outstanding bytes
            None,                 # 4: Stdout/Stderr
            None,                 # 5: Timestamp
        ].copy()
        response[Session.RESPONSE_IDX_TMSTAMP] = time.time()
        return response


def _subproc_data_ready(link: RNS.Link, chars_available: int):
    global _retry_timer
    log = _get_logger("_subproc_data_ready")
    session: Session = Session.get_for_tag(link.link_id)

    def send(timeout: bool, tag: any, tries: int) -> any:
        # log.debug("send")
        def inner():
            # log.debug("inner")
            try:
                if link.status != RNS.Link.ACTIVE:
                    _retry_timer.complete(link.link_id)
                    session.pending_receipt_take()
                    return

                pr = session.pending_receipt_take()
                log.debug(f"send inner pr: {pr}")
                if pr is not None and pr.status == RNS.PacketReceipt.DELIVERED:
                    if not timeout:
                        _retry_timer.complete(tag)
                    log.debug(f"Notification completed with status {pr.status} on link {link}")
                    return
                else:
                    if not timeout:
                        log.info(
                            f"Notifying client try {tries} (retcode: {session.return_code} " +
                            f"chars avail: {chars_available})")
                        packet = RNS.Packet(link, DATA_AVAIL_MSG.encode("utf-8"))
                        packet.send()
                        pr = packet.receipt
                        session.pending_receipt_put(pr)
                    else:
                        log.error(f"Retry count exceeded, terminating link {link}")
                        _retry_timer.complete(link.link_id)
                        link.teardown()
            except Exception as e:
                log.error("Error notifying client: " + str(e))

        _loop.call_soon_threadsafe(inner)
        return link.link_id

    with session.lock:
        if not _retry_timer.has_tag(link.link_id):
            _retry_timer.begin(try_limit=15,
                               wait_delay=max(link.rtt * 5 if link.rtt is not None else 1, 1),
                               try_callback=functools.partial(send, False),
                               timeout_callback=functools.partial(send, True),
                               tag=None)
        else:
            log.debug(f"Notification already pending for link {link}")


def _subproc_terminated(link: RNS.Link, return_code: int):
    global _loop
    log = _get_logger("_subproc_terminated")
    log.info(f"Subprocess returned {return_code} for link {link}")
    proc = Session.get_for_tag(link.link_id)
    if proc is None:
        log.debug(f"no proc for link {link}")
        return

    def cleanup():
        def inner():
            log.debug(f"cleanup culled link {link}")
            if link and link.status != RNS.Link.CLOSED:
                with exception.permit(SystemExit):
                    try:
                        link.teardown()
                    finally:
                        Session.clear_tag(link.link_id)

        _loop.call_later(300, inner)
        _loop.call_soon(_subproc_data_ready, link, 0)

    _loop.call_soon_threadsafe(cleanup)


def _listen_start_proc(link: RNS.Link, remote_identity: str | None, term: str,
                       loop: asyncio.AbstractEventLoop) -> Session | None:
    global _cmd
    log = _get_logger("_listen_start_proc")
    try:
        return Session(tag=link.link_id,
                       cmd=_cmd,
                       term=term,
                       remote_identity=remote_identity,
                       mdu=link.MDU,
                       loop=loop,
                       data_available_callback=functools.partial(_subproc_data_ready, link),
                       terminated_callback=functools.partial(_subproc_terminated, link))
    except Exception as e:
        log.error("Failed to launch process: " + str(e))
        _subproc_terminated(link, 255)
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
    proc: Session | None = Session.get_for_tag(link.link_id)
    if proc is None:
        log.warning(f"No process for link {link}")
    else:
        try:
            proc.process.terminate()
            _retry_timer.complete(link.link_id)
        except Exception as e:
            log.error(f"Error closing process for link {link}: {e}")
    Session.clear_tag(link.link_id)


def _initiator_identified(link, identity):
    global _allow_all, _cmd, _loop
    log = _get_logger("_initiator_identified")
    log.info("Initiator of link " + str(link) + " identified as " + RNS.prettyhexrep(identity.hash))
    if not _allow_all and identity.hash not in _allowed_identity_hashes:
        log.warning("Identity " + RNS.prettyhexrep(identity.hash) + " not allowed, tearing down link", RNS.LOG_WARNING)
        link.teardown()


def _listen_request(path, data, request_id, link_id, remote_identity, requested_at):
    global _destination, _retry_timer, _loop
    log = _get_logger("_listen_request")
    log.debug(f"listen_execute {path} {request_id} {link_id} {remote_identity}, {requested_at}")
    if not hasattr(data, "__len__") or len(data) < 1:
        raise Exception("Request data invalid")
    _retry_timer.complete(link_id)
    link: RNS.Link = next(filter(lambda l: l.link_id == link_id, _destination.links), None)
    if link is None:
        raise Exception(f"Invalid request {request_id}, no link found with id {link_id}")

    remote_version = data[Session.REQUEST_IDX_VERS]
    if not _protocol_check_magic(remote_version):
        raise Exception("Request magic incorrect")

    if not remote_version == _PROTOCOL_VERSION_0:
        response = Session.default_response()
        response[Session.RESPONSE_IDX_STDOUT] = base64.b64encode("Listener<->initiator version mismatch\r\n".encode("utf-8"))
        response[Session.RESPONSE_IDX_RETCODE] = 255
        response[Session.RESPONSE_IDX_RDYBYTE] = 0
        return response

    session: Session | None = None
    try:
        term = data[Session.REQUEST_IDX_TERM]
        session = Session.get_for_tag(link.link_id)
        if session is None:
            log.debug(f"Process not found for link {link}")
            session = _listen_start_proc(link=link,
                                               term=term,
                                               remote_identity=RNS.hexrep(remote_identity.hash).replace(":", ""),
                                               loop=_loop)

        # leave significant headroom for metadata and encoding
        result = session.process_request(data, link.MDU * 4 // 3)
        return result
        # return ProcessState.default_response()
    except Exception as e:
        log.error(f"Error procesing request for link {link}: {e}")
        try:
            if session is not None and session.process.running:
                session.process.terminate()
        except Exception as ee:
            log.debug(f"Error terminating process for link {link}: {ee}")

    return Session.default_response()


async def _spin(until: callable = None, timeout: float | None = None) -> bool:
    if timeout is not None:
        timeout += time.time()

    while (timeout is None or time.time() < timeout) and not until():
        await _check_finished(0.01)
    if timeout is not None and time.time() > timeout:
        return False
    else:
        return True


_link: RNS.Link | None = None
_remote_exec_grace = 2.0
_new_data: asyncio.Event | None = None
_tr: process.TTYRestorer | None = None


def _client_packet_handler(message, packet):
    global _new_data
    log = _get_logger("_client_packet_handler")
    if message is not None and message.decode("utf-8") == DATA_AVAIL_MSG and _new_data is not None:
        log.debug("data available")
        _new_data.set()
    else:
        log.error(f"received unhandled packet")


class RemoteExecutionError(Exception):
    def __init__(self, msg):
        self.msg = msg


def _response_handler(request_receipt: RNS.RequestReceipt):
    pass


async def _execute(configdir, identitypath=None, verbosity=0, quietness=0, noid=False, destination=None,
                   service_name="default", stdin=None, timeout=RNS.Transport.PATH_REQUEST_TIMEOUT):
    global _identity, _reticulum, _link, _destination, _remote_exec_grace, _tr, _new_data
    log = _get_logger("_execute")

    dest_len = (RNS.Reticulum.TRUNCATED_HASHLENGTH // 8) * 2
    if len(destination) != dest_len:
        raise RemoteExecutionError(
            "Allowed destination length is invalid, must be {hex} hexadecimal characters ({byte} bytes).".format(
                hex=dest_len, byte=dest_len // 2))
    try:
        destination_hash = bytes.fromhex(destination)
    except Exception as e:
        raise RemoteExecutionError("Invalid destination entered. Check your input.")

    if _reticulum is None:
        targetloglevel = 2 + verbosity - quietness
        _reticulum = RNS.Reticulum(configdir=configdir, loglevel=targetloglevel)

    if _identity is None:
        _prepare_identity(identitypath)

    if not RNS.Transport.has_path(destination_hash):
        RNS.Transport.request_path(destination_hash)
        log.info(f"Requesting path...")
        if not await _spin(until=lambda: RNS.Transport.has_path(destination_hash), timeout=timeout):
            raise RemoteExecutionError("Path not found")

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
        raise RemoteExecutionError("Could not establish link with " + RNS.prettyhexrep(destination_hash))

    if not noid and not _link.did_identify:
        _link.identify(_identity)
        _link.did_identify = True

    _link.set_packet_callback(_client_packet_handler)

    request = Session.default_request(sys.stdin.fileno())
    request[Session.REQUEST_IDX_STDIN] = (base64.b64encode(stdin) if stdin is not None else None)

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
                request_receipt.status != RNS.RequestReceipt.FAILED and
                request_receipt.status != RNS.RequestReceipt.SENT),
        timeout=timeout
    )

    if _link.status == RNS.Link.CLOSED:
        raise RemoteExecutionError("Could not request remote execution, link was closed")

    if request_receipt.status == RNS.RequestReceipt.FAILED:
        raise RemoteExecutionError("Could not request remote execution")

    await _spin(
        until=lambda: request_receipt.status != RNS.RequestReceipt.DELIVERED,
        timeout=timeout
    )

    if request_receipt.status == RNS.RequestReceipt.FAILED:
        raise RemoteExecutionError("No result was received")

    if request_receipt.status == RNS.RequestReceipt.FAILED:
        raise RemoteExecutionError("Receiving result failed")

    if request_receipt.response is not None:
        try:
            version = request_receipt.response[Session.RESPONSE_IDX_VERSION] or 0
            if not _protocol_check_magic(version):
                raise RemoteExecutionError("Protocol error")
            elif version != _PROTOCOL_VERSION_0:
                raise RemoteExecutionError("Protocol version mismatch")

            running = request_receipt.response[Session.RESPONSE_IDX_RUNNING] or True
            return_code = request_receipt.response[Session.RESPONSE_IDX_RETCODE]
            ready_bytes = request_receipt.response[Session.RESPONSE_IDX_RDYBYTE] or 0
            stdout = request_receipt.response[Session.RESPONSE_IDX_STDOUT]
            if stdout is not None:
                stdout = base64.b64decode(stdout)
            timestamp = request_receipt.response[Session.RESPONSE_IDX_TMSTAMP]
            # log.debug("data: " + (stdout.decode("utf-8") if stdout is not None else ""))
        except RemoteExecutionError:
            raise
        except Exception as e:
            raise RemoteExecutionError(f"Received invalid response") from e

        _tr.raw()
        if stdout is not None:
            # log.debug(f"stdout: {stdout}")
            os.write(sys.stdout.fileno(), stdout)

        sys.stdout.flush()
        sys.stderr.flush()

        log.debug(f"{ready_bytes} bytes ready on server, return code {return_code}")

        if ready_bytes > 0:
            _new_data.set()

        if (not running or return_code is not None) and (ready_bytes == 0):
            log.debug(f"returning running: {running}, return_code: {return_code}")
            return return_code or 255

        return None


async def _initiate(configdir: str, identitypath: str, verbosity: int, quietness: int, noid: bool, destination: str,
                    service_name: str, timeout: float):
    global _new_data, _finished, _tr
    log = _get_logger("_initiate")
    loop = asyncio.get_running_loop()
    _new_data = asyncio.Event()

    data_buffer = bytearray()

    def sigint_handler():
        log.debug("KeyboardInterrupt")
        data_buffer.extend("\x03".encode("utf-8"))
        _new_data.set()

    def sigwinch_handler():
        # log.debug("WindowChanged")
        if _new_data is not None:
            _new_data.set()

    def stdin():
        data = process.tty_read(sys.stdin.fileno())
        # log.debug(f"stdin {data}")
        if data is not None:
            data_buffer.extend(data)
            _new_data.set()

    process.tty_add_reader_callback(sys.stdin.fileno(), stdin)

    await _check_finished()
    loop.add_signal_handler(signal.SIGWINCH, sigwinch_handler)
    # leave a lot of overhead
    mdu = 64
    first_loop = True
    while True:
        try:
            log.debug("top of client loop")
            stdin = data_buffer[:mdu]
            data_buffer = data_buffer[mdu:]
            _new_data.clear()
            log.debug("before _execute")
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

            if first_loop:
                first_loop = False
                mdu = _link.MDU * 4 // 3
                loop.remove_signal_handler(signal.SIGINT)
                loop.add_signal_handler(signal.SIGINT, sigint_handler)
                _new_data.set()

            if return_code is not None:
                log.debug(f"received return code {return_code}, exiting")
                with exception.permit(SystemExit):
                    _link.teardown()

                return return_code
        except RemoteExecutionError as e:
            print(e.msg)
            return 255

        await process.event_wait(_new_data, 5)


_T = TypeVar("_T")


def _split_array_at(arr: [_T], at: _T) -> ([_T], [_T]):
    try:
        idx = arr.index(at)
        return arr[:idx], arr[idx + 1:]
    except ValueError:
        return arr, []


async def _rnsh_cli_main():
    global _tr, _finished, _loop
    import docopt
    log = _get_logger("main")
    _loop = asyncio.get_running_loop()
    rnslogging.set_main_loop(_loop)
    _finished = asyncio.Event()
    _loop.remove_signal_handler(signal.SIGINT)
    _loop.add_signal_handler(signal.SIGINT, functools.partial(_sigint_handler, signal.SIGINT, None))
    usage = '''
Usage:
    rnsh [--config <configdir>] [-i <identityfile>] [-s <service_name>] [-l] -p
    rnsh -l [--config <configfile>] [-i <identityfile>] [-s <service_name>] 
         [-v...] [-q...] [-b <period>] (-n | -a <identity_hash> [-a <identity_hash>] ...) 
         [--] <program> [<arg> ...]
    rnsh [--config <configfile>] [-i <identityfile>] [-s <service_name>] 
         [-v...] [-q...] [-N] [-m] [-w <timeout>] <destination_hash>
    rnsh -h
    rnsh --version

Options:
    --config DIR             Alternate Reticulum config directory to use
    -i FILE --identity FILE  Specific identity file to use
    -s NAME --service NAME   Listen on/connect to specific service name if not default
    -p --print-identity      Print identity information and exit
    -l --listen              Listen (server) mode
    -b --announce PERIOD     Announce on startup and every PERIOD seconds
                             Specify 0 for PERIOD to announce on startup only.
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

    argv, program_args = _split_array_at(sys.argv, "--")
    if len(program_args) > 0:
        argv.append(program_args[0])
        program_args = program_args[1:]

    args = docopt.docopt(usage, argv=argv[1:], version=f"rnsh {__version__}")
    # json.dump(args, sys.stdout)

    args_service_name = args.get("--service", None) or "default"
    args_listen = args.get("--listen", None) or False
    args_identity = args.get("--identity", None)
    args_config = args.get("--config", None)
    args_print_identity = args.get("--print-identity", None) or False
    args_verbose = args.get("--verbose", None) or 0
    args_quiet = args.get("--quiet", None) or 0
    args_announce = args.get("--announce", None)
    try:
        if args_announce:
            args_announce = int(args_announce)
    except ValueError:
        print("Invalid value for --announce")
        return 1
    args_no_auth = args.get("--no-auth", None) or False
    args_allowed = args.get("--allowed", None) or []
    args_program = args.get("<program>", None)
    args_program_args = args.get("<arg>", None) or []
    args_program_args.insert(0, args_program)
    args_program_args.extend(program_args)
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
            command=args_program_args,
            identitypath=args_identity,
            service_name=args_service_name,
            verbosity=args_verbose,
            quietness=args_quiet,
            allowed=args_allowed,
            disable_auth=args_no_auth,
            announce_period=args_announce,
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
        finally:
            _tr.restore()
    else:
        print("")
        print(args)
        print("")


def rnsh_cli():
    global _tr, _retry_timer
    with process.TTYRestorer(sys.stdin.fileno()) as _tr:
        with retry.RetryThread() as _retry_timer:
            return_code = asyncio.run(_rnsh_cli_main())

    with exception.permit(SystemExit):
        process.tty_unset_reader_callbacks(sys.stdin.fileno())

    sys.exit(return_code or 255)


if __name__ == "__main__":
    rnsh_cli()
