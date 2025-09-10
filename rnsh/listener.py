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
import rnsh.rnsh

module_logger = __logging.getLogger(__name__)


def _get_logger(name: str):
    global module_logger
    return module_logger.getChild(name)


_identity = None
_reticulum = None
_allow_all = False
_allowed_file = None
_allowed_identity_hashes = []
_allowed_file_identity_hashes = []
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


def _sigint_handler(sig, loop):
    global _finished
    log = _get_logger("_sigint_handler")
    log.debug(signal.Signals(sig).name)
    if _finished is not None:
        _finished.set()
    else:
        raise KeyboardInterrupt()

def _reload_allowed_file():
    global _allowed_file, _allowed_file_identity_hashes
    log = _get_logger("_listen")
    if _allowed_file != None:
        try:
            with open(_allowed_file, "r") as file:
                dest_len = (RNS.Reticulum.TRUNCATED_HASHLENGTH // 8) * 2
                added = 0
                line = 0
                _allowed_file_identity_hashes = []
                for allow in file.read().replace("\r", "").split("\n"):
                    line += 1
                    if len(allow) == dest_len:
                        try:
                            destination_hash = bytes.fromhex(allow)
                            _allowed_file_identity_hashes.append(destination_hash)
                            added += 1
                        except Exception:
                            log.debug(f"Discarded invalid Identity hash in {_allowed_file} at line {line}")

                ms = "y" if added == 1 else "ies"
                log.debug(f"Loaded {added} allowed identit{ms} from "+str(_allowed_file))
        except Exception as e:
            log.error(f"Error while reloading allowed indetities file: {e}")


async def listen(configdir, command, identitypath=None, service_name=None, verbosity=0, quietness=0, allowed=None,
                 allowed_file=None, disable_auth=None, announce_period=900, no_remote_command=True, remote_cmd_as_args=False,
                 loop: asyncio.AbstractEventLoop = None):
    global _identity, _allow_all, _allowed_identity_hashes, _allowed_file, _allowed_file_identity_hashes
    global _reticulum, _cmd, _destination, _no_remote_command, _remote_cmd_as_args, _finished
    log = _get_logger("_listen")
    if not loop:
        loop = asyncio.get_running_loop()
    if service_name is None or len(service_name) == 0:
        service_name = "default"

    log.info(f"Using service name {service_name}")

    # Emit an immediate readiness hint before heavy initialization so tests can detect startup promptly
    try:
        print("rnsh listening...", flush=True)
    except Exception:
        pass


    # More -v should LOWER the threshold (more chatty); more -q should RAISE it (quieter)
    try:
        targetloglevel = max(RNS.LOG_DEBUG, RNS.LOG_INFO - int(verbosity) + int(quietness))
    except Exception:
        targetloglevel = RNS.LOG_INFO
    _reticulum = RNS.Reticulum(configdir=configdir, loglevel=targetloglevel)
    rnslogging.RnsHandler.set_log_level_with_rns_level(targetloglevel)
    _identity = rnsh.rnsh.prepare_identity(identitypath, service_name)
    _destination = RNS.Destination(_identity, RNS.Destination.IN, RNS.Destination.SINGLE, rnsh.rnsh.APP_NAME)
    # Log early to ensure visibility for readiness checks in tests
    log.info("rnsh listening for commands on " + RNS.prettyhexrep(_destination.hash))
    try:
        # Also print directly to stdout with flush to guarantee capture by PTY/pipe
        print("rnsh listening for commands on " + RNS.prettyhexrep(_destination.hash), flush=True)
    except Exception:
        pass

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
        if allowed_file is not None:
            _allowed_file = allowed_file
            _reload_allowed_file()

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

    if (len(_allowed_identity_hashes) < 1 and len(_allowed_file_identity_hashes) < 1) and not disable_auth:
        log.warning("Warning: No allowed identities configured, rnsh will not accept any connections!")

    def link_established(lnk: RNS.Link):
        _reload_allowed_file()
        session.ListenerSession.allowed_file_identity_hashes = _allowed_file_identity_hashes
        session.ListenerSession(session.RNSOutlet.get_outlet(lnk), lnk.get_channel(), loop)
    _destination.set_link_established_callback(link_established)

    _finished = asyncio.Event()
    signal.signal(signal.SIGINT, _sigint_handler)

    # Log again after full setup
    log.info("rnsh listening for commands on " + RNS.prettyhexrep(_destination.hash))
    try:
        print("rnsh listening for commands on " + RNS.prettyhexrep(_destination.hash), flush=True)
    except Exception:
        pass

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
                # no sleep if there's work to do
                if not await session.ListenerSession.pump_all():
                    await sleeper.sleep_async()
            else:
                await asyncio.sleep(0.25)
    finally:
        log.warning("Shutting down")
        await session.ListenerSession.terminate_all("Shutting down")
        await asyncio.sleep(1)
        links_still_active = list(filter(lambda l: l.status != RNS.Link.CLOSED, _destination.links))
        for link in links_still_active:
            if link.status not in [RNS.Link.CLOSED]:
                link.teardown()
                await asyncio.sleep(0.01)