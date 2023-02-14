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
import importlib.metadata
import logging as __logging
import os
import shlex
import signal
import sys
import termios
import threading
import time
import tty
from typing import Callable, TypeVar
import RNS
import rnsh.exception as exception
import rnsh.process as process
import rnsh.retry as retry
import rnsh.rnslogging as rnslogging
import rnsh.hacks as hacks
import re
import contextlib
import rnsh.args
import pwd

module_logger = __logging.getLogger(__name__)


def _get_logger(name: str):
    global module_logger
    return module_logger.getChild(name)


APP_NAME = "rnsh"
_identity = None
_reticulum = None
_allow_all = False
_allowed_identity_hashes = []
_cmd: [str] | None = None
DATA_AVAIL_MSG = "data available"
_finished: asyncio.Event = None
_retry_timer: retry.RetryThread | None = None
_destination: RNS.Destination | None = None
_loop: asyncio.AbstractEventLoop | None = None
_no_remote_command = True


async def _check_finished(timeout: float = 0):
    return await process.event_wait(_finished, timeout=timeout)


def _sigint_handler(sig, frame):
    global _finished
    log = _get_logger("_sigint_handler")
    log.debug(signal.Signals(sig).name)
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


async def _listen(configdir, command, identitypath=None, service_name="default", verbosity=0, quietness=0, allowed=None,
                  disable_auth=None, announce_period=900, no_remote_command=True):
    global _identity, _allow_all, _allowed_identity_hashes, _reticulum, _cmd, _destination, _no_remote_command
    log = _get_logger("_listen")


    targetloglevel = RNS.LOG_INFO + verbosity - quietness
    _reticulum = RNS.Reticulum(configdir=configdir, loglevel=targetloglevel)
    rnslogging.RnsHandler.set_log_level_with_rns_level(targetloglevel)
    _prepare_identity(identitypath)
    _destination = RNS.Destination(_identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, service_name)

    _cmd = command
    if _cmd is None or len(_cmd) == 0:
        shell = pwd.getpwuid(os.getuid()).pw_shell
        log.info(f"Using {shell} for default command.")
        _cmd = [shell]
    else:
        log.info(f"Using command {shlex.join(_cmd)}")

    _no_remote_command = no_remote_command
    if _cmd is None and _no_remote_command:
        raise Exception(f"Unable to look up shell for {os.getlogin}, cannot proceed with -C and no <program>.")

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
            # response_generator=_listen_request,
            allow=RNS.Destination.ALLOW_LIST,
            allowed_list=_allowed_identity_hashes
        )
    else:
        _destination.register_request_handler(
            path="data",
            response_generator=hacks.request_request_id_hack(_listen_request, asyncio.get_running_loop()),
            # response_generator=_listen_request,
            allow=RNS.Destination.ALLOW_ALL,
        )

    if await _check_finished():
        return

    log.info("rnsh listening for commands on " + RNS.prettyhexrep(_destination.hash))

    if announce_period is not None:
        _destination.announce()

    last = time.time()

    try:
        while not await _check_finished(1.0):
            if announce_period and 0 < announce_period < time.time() - last:
                last = time.time()
                _destination.announce()
    finally:
        log.warning("Shutting down")
        for link in list(_destination.links):
            with exception.permit(SystemExit, KeyboardInterrupt):
                proc = Session.get_for_tag(link.link_id)
                if proc is not None and proc.process.running:
                    proc.process.terminate()
        await asyncio.sleep(0)
        links_still_active = list(filter(lambda l: l.status != RNS.Link.CLOSED, _destination.links))
        for link in links_still_active:
            if link.status != RNS.Link.CLOSED:
                link.teardown()
                await asyncio.sleep(0)


_PROTOCOL_MAGIC = 0xdeadbeef


def _protocol_make_version(version: int):
    return (_PROTOCOL_MAGIC << 32) & 0xffffffff00000000 | (0xffffffff & version)


_PROTOCOL_VERSION_0 = _protocol_make_version(0)
_PROTOCOL_VERSION_1 = _protocol_make_version(1)


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
        self._log.debug(f"Starting {cmd}")
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
    REQUEST_IDX_CMD = 8

    @staticmethod
    def default_request(stdin_fd: int | None) -> [any]:
        global _tr
        global _PROTOCOL_VERSION_1
        request: list[any] = [
            _PROTOCOL_VERSION_1,  # 0 Protocol Version
            None,  # 1 Stdin
            None,  # 2 TERM variable
            None,  # 3 termios attributes or something
            None,  # 4 terminal rows
            None,  # 5 terminal cols
            None,  # 6 terminal horizontal pixels
            None,  # 7 terminal vertical pixels
            None,  # 8 Command to run
        ].copy()

        if stdin_fd is not None:
            request[Session.REQUEST_IDX_TERM] = os.environ.get("TERM", None)
            request[Session.REQUEST_IDX_TIOS] = _tr.original_attr() if _tr else None
            with contextlib.suppress(OSError):
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

        first_term_state = self._term_state is None
        term_state = data[Session.REQUEST_IDX_TIOS:Session.REQUEST_IDX_VPIX + 1]

        response[Session.RESPONSE_IDX_RUNNING] = self.process.running
        if self.process.running:
            if term_state != self._term_state:
                self._term_state = term_state
                if term_state is not None:
                    self._update_winsz()
                    if first_term_state is not None:
                        # TODO: use a more specific error
                        with contextlib.suppress(Exception):
                            self.process.tcsetattr(termios.TCSANOW, term_state[0])
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
    def default_response(version: int = _PROTOCOL_VERSION_1) -> [any]:
        response: list[any] = [
            version,  # 0: Protocol version
            False,  # 1: Process running
            None,  # 2: Return value
            0,  # 3: Number of outstanding bytes
            None,  # 4: Stdout/Stderr
            None,  # 5: Timestamp
        ].copy()
        response[Session.RESPONSE_IDX_TMSTAMP] = time.time()
        return response

    @classmethod
    def error_response(cls, msg: str, version: int = _PROTOCOL_VERSION_1) -> [any]:
        response = cls.default_response(version)
        response[Session.RESPONSE_IDX_STDOUT] = base64.b64encode( f"{msg}\r\n".encode("utf-8"))
        response[Session.RESPONSE_IDX_RETCODE] = 255
        response[Session.RESPONSE_IDX_RDYBYTE] = 0
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


def _listen_start_proc(link: RNS.Link, remote_identity: str | None, term: str, cmd: str | None,
                       loop: asyncio.AbstractEventLoop) -> Session | None:
    log = _get_logger("_listen_start_proc")
    try:
        return Session(tag=link.link_id,
                       cmd=cmd,
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
    global _destination, _retry_timer, _loop, _cmd, _no_remote_command
    log = _get_logger("_listen_request")
    log.debug(
        f"listen_execute {path} {RNS.prettyhexrep(request_id)} {RNS.prettyhexrep(link_id)} {remote_identity}, {requested_at}")
    if not hasattr(data, "__len__") or len(data) < 1:
        raise Exception("Request data invalid")
    _retry_timer.complete(link_id)
    link: RNS.Link = next(filter(lambda l: l.link_id == link_id, _destination.links), None)
    if link is None:
        raise Exception(f"Invalid request {request_id}, no link found with id {link_id}")

    remote_version = data[Session.REQUEST_IDX_VERS]
    if not _protocol_check_magic(remote_version):
        raise Exception("Request magic incorrect")

    if not remote_version == _PROTOCOL_VERSION_0 and not remote_version == _PROTOCOL_VERSION_1:
        return Session.error_response("Listener<->initiator version mismatch")

    cmd = _cmd
    if remote_version == _PROTOCOL_VERSION_1:
        remote_command = data[Session.REQUEST_IDX_CMD]
        if remote_command is not None and len(remote_command) > 0:
            if _no_remote_command:
                return Session.error_response("Listener does not permit initiator to provide command.")
            cmd = remote_command

    if not _no_remote_command and (cmd is None or len(cmd) == 0):
        return Session.error_response("No command supplied and no default command available.")

    session: Session | None = None
    try:
        term = data[Session.REQUEST_IDX_TERM]
        # sanitize
        if term is not None:
            term = re.sub('[^A-Za-z-0-9\-\_]','', term)
        session = Session.get_for_tag(link.link_id)
        if session is None:
            log.debug(f"Process not found for link {link}")
            session = _listen_start_proc(link=link,
                                         term=term,
                                         cmd=cmd,
                                         remote_identity=RNS.hexrep(remote_identity.hash).replace(":", ""),
                                         loop=_loop)

        # leave significant headroom for metadata and encoding
        result = session.process_request(data, link.MDU * 1 // 2)
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
        if await _check_finished(0.001):
            raise asyncio.CancelledError()
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
                   service_name="default", stdin=None, timeout=RNS.Transport.PATH_REQUEST_TIMEOUT,
                   cmd: [str] | None = None):
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
        targetloglevel = RNS.LOG_ERROR + verbosity - quietness
        _reticulum = RNS.Reticulum(configdir=configdir, loglevel=targetloglevel)
        rnslogging.RnsHandler.set_log_level_with_rns_level(targetloglevel)

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
        log.debug("No link")
        _link = RNS.Link(_destination)
        _link.did_identify = False

    log.info(f"Establishing link...")
    if not await _spin(until=lambda: _link.status == RNS.Link.ACTIVE, timeout=timeout):
        raise RemoteExecutionError("Could not establish link with " + RNS.prettyhexrep(destination_hash))

    log.debug("Have link")
    if not noid and not _link.did_identify:
        _link.identify(_identity)
        _link.did_identify = True

    _link.set_packet_callback(_client_packet_handler)

    request = Session.default_request(sys.stdin.fileno())
    log.debug(f"Sending {len(stdin) or 0} bytes to listener")
    # log.debug(f"Sending {stdin} to listener")
    request[Session.REQUEST_IDX_STDIN] = (base64.b64encode(stdin) if stdin is not None else None)
    request[Session.REQUEST_IDX_CMD] = cmd

    # TODO: Tune
    timeout = timeout + _link.rtt * 4 + _remote_exec_grace

    log.debug("Sending request")
    request_receipt = _link.request(
        path="data",
        data=request,
        timeout=timeout
    )
    timeout += 0.5

    log.debug("Waiting for delivery")
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
            elif version != _PROTOCOL_VERSION_0 and version != _PROTOCOL_VERSION_1:
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

        if stdout is not None:
            _tr.raw()
            log.debug(f"stdout: {stdout}")
            os.write(sys.stdout.fileno(), stdout)
            sys.stdout.flush()

        got_bytes = len(stdout) if stdout is not None else 0
        log.debug(f"{got_bytes} chars received, {ready_bytes} bytes ready on server, return code {return_code}")

        if ready_bytes > 0:
            _new_data.set()

        if (not running or return_code is not None) and (ready_bytes == 0):
            log.debug(f"returning running: {running}, return_code: {return_code}")
            return return_code or 255

        return None

_pre_input = bytearray()


async def _initiate(configdir: str, identitypath: str, verbosity: int, quietness: int, noid: bool, destination: str,
                    service_name: str, timeout: float, command: [str] | None = None):
    global _new_data, _finished, _tr, _cmd, _pre_input
    log = _get_logger("_initiate")
    loop = asyncio.get_running_loop()
    _new_data = asyncio.Event()
    command = command
    if command is not None and len(command) == 1:
        command = shlex.split(command[0])

    data_buffer = bytearray(sys.stdin.buffer.read()) if not os.isatty(sys.stdin.fileno()) else bytearray()

    def sigwinch_handler():
        # log.debug("WindowChanged")
        if _new_data is not None:
            _new_data.set()

    def stdin():
        try:
            data = process.tty_read(sys.stdin.fileno())
            log.debug(f"stdin {data}")
            if data is not None:
                data_buffer.extend(data)
                _new_data.set()
        except EOFError:
            data_buffer.extend(process.CTRL_D)
            process.tty_unset_reader_callbacks(sys.stdin.fileno())

    process.tty_add_reader_callback(sys.stdin.fileno(), stdin)

    await _check_finished()
    loop.add_signal_handler(signal.SIGWINCH, sigwinch_handler)

    # leave a lot of overhead
    mdu = 64
    rtt = 5
    first_loop = True
    while not await _check_finished():
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
                cmd=command,
            )

            if first_loop:
                first_loop = False
                mdu = _link.MDU // 2
                _new_data.set()

            if _link:
                rtt = _link.rtt

            if return_code is not None:
                log.debug(f"received return code {return_code}, exiting")
                with exception.permit(SystemExit, KeyboardInterrupt):
                    _link.teardown()

                return return_code
        except asyncio.CancelledError:
            if _link and _link.status != RNS.Link.CLOSED:
                _link.teardown()
                return 0
        except RemoteExecutionError as e:
            print(e.msg)
            return 255

        await process.event_wait_any([_new_data, _finished], timeout=min(max(rtt * 50, 5), 120))


def _loop_set_signal(sig, loop):
    loop.remove_signal_handler(sig)
    loop.add_signal_handler(sig, functools.partial(_sigint_handler, sig, None))


async def _rnsh_cli_main():
    global _tr, _finished, _loop
    import docopt
    log = _get_logger("main")
    _loop = asyncio.get_running_loop()
    rnslogging.set_main_loop(_loop)
    _finished = asyncio.Event()
    _loop_set_signal(signal.SIGINT, _loop)
    _loop_set_signal(signal.SIGTERM, _loop)

    args = rnsh.args.Args(sys.argv)

    if args.print_identity:
        _print_identity(args.config, args.identity, args.service_name, args.listen)
        return 0

    if args.listen:
        # log.info("command " + args.command)
        await _listen(configdir=args.config,
                      command=args.program_args,
                      identitypath=args.identity,
                      service_name=args.service_name,
                      verbosity=args.verbose,
                      quietness=args.quiet,
                      allowed=args.allowed,
                      disable_auth=args.no_auth,
                      announce_period=args.announce,
                      no_remote_command=args.no_remote_cmd)
        return 0

    if args.destination is not None and args.service_name is not None:
        return_code = await _initiate(
            configdir=args.config,
            identitypath=args.identity,
            verbosity=args.verbose,
            quietness=args.quiet,
            noid=args.no_id,
            destination=args.destination,
            service_name=args.service_name,
            timeout=args.timeout,
            command=args.program_args
        )
        return return_code if args.mirror else 0
    else:
        print("")
        print(args.usage)
        print("")
        return 1


def rnsh_cli():
    global _tr, _retry_timer, _pre_input
    with contextlib.suppress(Exception):
        if not os.isatty(sys.stdin.fileno()):
            time.sleep(0.1)  # attempting to deal with an issue with missing input
            tty.setraw(sys.stdin.fileno(), termios.TCSANOW)

    with process.TTYRestorer(sys.stdin.fileno()) as _tr, retry.RetryThread() as _retry_timer:
        return_code = asyncio.run(_rnsh_cli_main())

    process.tty_unset_reader_callbacks(sys.stdin.fileno())
    sys.exit(return_code if return_code is not None else 255)


if __name__ == "__main__":
    rnsh_cli()
