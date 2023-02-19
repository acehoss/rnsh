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
import enum
import functools
import importlib.metadata
import logging as __logging
import os
import queue
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
import rnsh.session as session
import re
import contextlib
import rnsh.args
import pwd
import rnsh.protocol as protocol
import rnsh.helpers as helpers

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
_remote_cmd_as_args = False


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
                  disable_auth=None, announce_period=900, no_remote_command=True, remote_cmd_as_args=False):
    global _identity, _allow_all, _allowed_identity_hashes, _reticulum, _cmd, _destination, _no_remote_command
    global _remote_cmd_as_args
    log = _get_logger("_listen")


    targetloglevel = RNS.LOG_INFO + verbosity - quietness
    _reticulum = RNS.Reticulum(configdir=configdir, loglevel=targetloglevel)
    rnslogging.RnsHandler.set_log_level_with_rns_level(targetloglevel)
    _prepare_identity(identitypath)
    _destination = RNS.Destination(_identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, service_name)

    _cmd = command
    if _cmd is None or len(_cmd) == 0:
        shell = None
        try:
            shell = pwd.getpwuid(os.getuid()).pw_shell
        except Exception as e:
            log.error(f"Error looking up shell: {e}")
        log.info(f"Using {shell} for default command.")
        _cmd = [shell] if shell else None
    else:
        log.info(f"Using command {shlex.join(_cmd)}")

    _no_remote_command = no_remote_command
    session.ListenerSession.allow_remote_command = not no_remote_command
    _remote_cmd_as_args = remote_cmd_as_args
    if (_cmd is None or len(_cmd) == 0 or _cmd[0] is None or len(_cmd[0]) == 0) \
            and (_no_remote_command or _remote_cmd_as_args):
        raise Exception(f"Unable to look up shell for {os.getlogin}, cannot proceed with -A or -C and no <program>.")

    session.ListenerSession.default_command = _cmd
    session.ListenerSession.remote_cmd_as_args = _remote_cmd_as_args

    if disable_auth:
        _allow_all = True
        session.ListenerSession.allow_all = True
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
                        session.ListenerSession.allowed_identity_hashes.append(destination_hash)
                    except Exception:
                        raise ValueError("Invalid destination entered. Check your input.")
                except Exception as e:
                    log.error(str(e))
                    exit(1)

    if len(_allowed_identity_hashes) < 1 and not disable_auth:
        log.warning("Warning: No allowed identities configured, rnsh will not accept any connections!")

    def link_established(lnk: RNS.Link):
        session.ListenerSession(session.RNSOutlet.get_outlet(lnk), _loop)
    _destination.set_link_established_callback(link_established)

    if await _check_finished():
        return

    log.info("rnsh listening for commands on " + RNS.prettyhexrep(_destination.hash))

    if announce_period is not None:
        _destination.announce()

    last_announce = time.time()
    sleeper = helpers.SleepRate(0.01)

    try:
        while not await _check_finished():
            if announce_period and 0 < announce_period < time.time() - last_announce:
                last_announce = time.time()
                _destination.announce()
            if len(session.ListenerSession.sessions) > 0:
                await session.ListenerSession.pump_all()
                await sleeper.sleep_async()
            else:
                await asyncio.sleep(0.25)
    finally:
        log.warning("Shutting down")
        await session.ListenerSession.terminate_all("Shutting down")
        await asyncio.sleep(1)
        session.ListenerSession.messenger.shutdown()
        links_still_active = list(filter(lambda l: l.status != RNS.Link.CLOSED, _destination.links))
        for link in links_still_active:
            if link.status not in [RNS.Link.CLOSED]:
                link.teardown()
                await asyncio.sleep(0.01)


async def _spin(until: callable = None, timeout: float | None = None) -> bool:
    if timeout is not None:
        timeout += time.time()

    while (timeout is None or time.time() < timeout) and not until():
        if await _check_finished(0.01):
            raise asyncio.CancelledError()
    if timeout is not None and time.time() > timeout:
        return False
    else:
        return True


_link: RNS.Link | None = None
_remote_exec_grace = 2.0
_new_data: asyncio.Event | None = None
_tr: process.TTYRestorer | None = None

_pq = queue.Queue()

class InitiatorState(enum.IntEnum):
    IS_INITIAL      = 0
    IS_LINKED     = 1
    IS_WAIT_VERS  = 2
    IS_RUNNING    = 3
    IS_TERMINATE  = 4
    IS_TEARDOWN   = 5


def _client_link_closed(link):
    log = _get_logger("_client_link_closed")
    _finished.set()


def _client_packet_handler(message, packet):
    global _new_data
    log = _get_logger("_client_packet_handler")
    packet.prove()
    _pq.put(message)



class RemoteExecutionError(Exception):
    def __init__(self, msg):
        self.msg = msg


async def _initiate_link(configdir, identitypath=None, verbosity=0, quietness=0, noid=False, destination=None,
                         service_name="default", timeout=RNS.Transport.PATH_REQUEST_TIMEOUT):
    global _identity, _reticulum, _link, _destination, _remote_exec_grace, _tr, _new_data
    log = _get_logger("_initiate_link")

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

        _link.set_link_closed_callback(_client_link_closed)

    log.info(f"Establishing link...")
    if not await _spin(until=lambda: _link.status == RNS.Link.ACTIVE, timeout=timeout):
        raise RemoteExecutionError("Could not establish link with " + RNS.prettyhexrep(destination_hash))

    log.debug("Have link")
    if not noid and not _link.did_identify:
        _link.identify(_identity)
        _link.did_identify = True

    _link.set_packet_callback(_client_packet_handler)


async def _initiate(configdir: str, identitypath: str, verbosity: int, quietness: int, noid: bool, destination: str,
                    service_name: str, timeout: float, command: [str] | None = None):
    global _new_data, _finished, _tr, _cmd, _pre_input
    log = _get_logger("_initiate")
    loop = asyncio.get_running_loop()
    _new_data = asyncio.Event()
    state = InitiatorState.IS_INITIAL
    data_buffer = bytearray(sys.stdin.buffer.read()) if not os.isatty(sys.stdin.fileno()) else bytearray()

    await _initiate_link(
        configdir=configdir,
        identitypath=identitypath,
        verbosity=verbosity,
        quietness=quietness,
        noid=noid,
        destination=destination,
        service_name=service_name,
        timeout=timeout,
    )

    if not _link or _link.status != RNS.Link.ACTIVE:
        _finished.set()
        return 255

    state = InitiatorState.IS_LINKED
    outlet = session.RNSOutlet(_link)
    with protocol.Messenger(retry_delay_min=5) as messenger:

        # Next step after linking and identifying: send version
        # if not await _spin(lambda: messenger.is_outlet_ready(outlet), timeout=5):
        #     print("Error bringing up link")
        #     return 253

        messenger.send(outlet, protocol.VersionInfoMessage())
        try:
            vp = _pq.get(timeout=max(outlet.rtt * 20, 5))
            vm = messenger.receive(vp)
            if not isinstance(vm, protocol.VersionInfoMessage):
                raise Exception("Invalid message received")
            log.debug(f"Server version info: sw {vm.sw_version} prot {vm.protocol_version}")
            state = InitiatorState.IS_RUNNING
        except queue.Empty:
            print("Protocol error")
            return 254

        winch = False
        def sigwinch_handler():
            nonlocal winch
            # log.debug("WindowChanged")
            winch = True
            # if _new_data is not None:
            #     _new_data.set()

        stdin_eof = False
        def stdin():
            nonlocal stdin_eof
            try:
                data = process.tty_read(sys.stdin.fileno())
                log.debug(f"stdin {data}")
                if data is not None:
                    data_buffer.extend(data)
                    _new_data.set()
            except EOFError:
                if os.isatty(0):
                    data_buffer.extend(process.CTRL_D)
                stdin_eof = True
                process.tty_unset_reader_callbacks(sys.stdin.fileno())

        process.tty_add_reader_callback(sys.stdin.fileno(), stdin)

        await _check_finished()

        tcattr = None
        rows, cols, hpix, vpix = (None, None, None, None)
        try:
            tcattr = termios.tcgetattr(0)
            rows, cols, hpix, vpix = process.tty_get_winsize(0)
        except:
            try:
                tcattr = termios.tcgetattr(1)
                rows, cols, hpix, vpix = process.tty_get_winsize(1)
            except:
                try:
                    tcattr = termios.tcgetattr(2)
                    rows, cols, hpix, vpix = process.tty_get_winsize(2)
                except:
                    pass

        messenger.send(outlet, protocol.ExecuteCommandMesssage(cmdline=command,
                                                               pipe_stdin=not os.isatty(0),
                                                               pipe_stdout=not os.isatty(1),
                                                               pipe_stderr=not os.isatty(2),
                                                               tcflags=tcattr,
                                                               term=os.environ.get("TERM", None),
                                                               rows=rows,
                                                               cols=cols,
                                                               hpix=hpix,
                                                               vpix=vpix))

        loop.add_signal_handler(signal.SIGWINCH, sigwinch_handler)
        mdu = _link.MDU - 16
        sent_eof = False
        last_winch = time.time()
        sleeper = helpers.SleepRate(0.01)
        while not await _check_finished() and state in [InitiatorState.IS_RUNNING]:
            try:
                try:
                    packet = _pq.get(timeout=sleeper.next_sleep_time())
                    message = messenger.receive(packet)

                    if isinstance(message, protocol.StreamDataMessage):
                        if message.stream_id == protocol.StreamDataMessage.STREAM_ID_STDOUT:
                            if message.data and len(message.data) > 0:
                                _tr.raw()
                                log.debug(f"stdout: {message.data}")
                                os.write(1, message.data)
                                sys.stdout.flush()
                            if message.eof:
                                os.close(1)
                        if message.stream_id == protocol.StreamDataMessage.STREAM_ID_STDERR:
                            if message.data and len(message.data) > 0:
                                _tr.raw()
                                log.debug(f"stdout: {message.data}")
                                os.write(2, message.data)
                                sys.stderr.flush()
                            if message.eof:
                                os.close(2)
                    elif isinstance(message, protocol.CommandExitedMessage):
                        log.debug(f"received return code {message.return_code}, exiting")
                        with exception.permit(SystemExit, KeyboardInterrupt):
                            _link.teardown()
                            return message.return_code
                    elif isinstance(message, protocol.ErrorMessage):
                        log.error(message.data)
                        if message.fatal:
                            _link.teardown()
                            return 200

                except queue.Empty:
                    pass

                if messenger.is_outlet_ready(outlet):
                    stdin = data_buffer[:mdu]
                    data_buffer = data_buffer[mdu:]
                    eof = not sent_eof and stdin_eof and len(stdin) == 0
                    if len(stdin) > 0 or eof:
                        messenger.send(outlet, protocol.StreamDataMessage(protocol.StreamDataMessage.STREAM_ID_STDIN,
                                                                          stdin, eof))
                        sent_eof = eof

                # send window change, but rate limited
                if winch and time.time() - last_winch > _link.rtt * 25:
                    last_winch = time.time()
                    winch = False
                    with contextlib.suppress(Exception):
                        r, c, h, v = process.tty_get_winsize(0)
                        messenger.send(outlet, protocol.WindowSizeMessage(r, c, h, v))
            except RemoteExecutionError as e:
                print(e.msg)
                return 255
            except Exception as ex:
                print(f"Client exception: {ex}")
                if _link and _link.status != RNS.Link.CLOSED:
                    _link.teardown()
                    return 127

            # await process.event_wait_any([_new_data, _finished], timeout=min(max(rtt * 50, 5), 120))
            # await sleeper.sleep_async()
        log.debug("after main loop")
        return 0


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
                      command=args.command_line,
                      identitypath=args.identity,
                      service_name=args.service_name,
                      verbosity=args.verbose,
                      quietness=args.quiet,
                      allowed=args.allowed,
                      disable_auth=args.no_auth,
                      announce_period=args.announce,
                      no_remote_command=args.no_remote_cmd,
                      remote_cmd_as_args=args.remote_cmd_as_args)
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
            command=args.command_line
        )
        return return_code if args.mirror else 0
    else:
        print("")
        print(args.usage)
        print("")
        return 1


def rnsh_cli():
    global _tr, _retry_timer, _pre_input
    with process.TTYRestorer(sys.stdin.fileno()) as _tr, retry.RetryThread() as _retry_timer:
        return_code = asyncio.run(_rnsh_cli_main())

    process.tty_unset_reader_callbacks(sys.stdin.fileno())
    sys.exit(return_code if return_code is not None else 255)


if __name__ == "__main__":
    rnsh_cli()
