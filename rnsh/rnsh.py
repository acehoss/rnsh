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
import rnsh.loop
import rnsh.listener as listener
import rnsh.initiator as initiator

module_logger = __logging.getLogger(__name__)


def _get_logger(name: str):
    global module_logger
    return module_logger.getChild(name)


APP_NAME = "rnsh"
loop: asyncio.AbstractEventLoop | None = None


def _sanitize_service_name(service_name:str) -> str:
    return re.sub(r'\W+', '', service_name)


def prepare_identity(identity_path, service_name: str = None) -> tuple[RNS.Identity]:
    log = _get_logger("_prepare_identity")
    service_name = _sanitize_service_name(service_name or "")
    if identity_path is None:
        identity_path = RNS.Reticulum.identitypath + "/" + APP_NAME + \
                        (f".{service_name}" if service_name and len(service_name) > 0 else "")

    identity = None
    if os.path.isfile(identity_path):
        identity = RNS.Identity.from_file(identity_path)

    if identity is None:
        log.info("No valid saved identity found, creating new...")
        identity = RNS.Identity()
        identity.to_file(identity_path)
    return identity


def print_identity(configdir, identitypath, service_name, include_destination: bool):
    reticulum = RNS.Reticulum(configdir=configdir, loglevel=RNS.LOG_INFO)
    if service_name and len(service_name) > 0:
        print(f"Using service name \"{service_name}\"")
    identity = prepare_identity(identitypath, service_name)
    destination = RNS.Destination(identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME)
    print("Identity     : " + str(identity))
    if include_destination:
        print("Listening on : " + RNS.prettyhexrep(destination.hash))
    exit(0)


verbose_set = False


async def _rnsh_cli_main():
    global verbose_set
    log = _get_logger("main")
    _loop = asyncio.get_running_loop()
    rnslogging.set_main_loop(_loop)
    args = rnsh.args.Args(sys.argv)
    verbose_set = args.verbose > 0

    if args.print_identity:
        print_identity(args.config, args.identity, args.service_name, args.listen)
        return 0

    if args.listen:
        allowed_file = None
        dest_len = (RNS.Reticulum.TRUNCATED_HASHLENGTH//8)*2
        if os.path.isfile(os.path.expanduser("~/.config/rnsh/allowed_identities")):
            allowed_file = os.path.expanduser("~/.config/rnsh/allowed_identities")
        elif os.path.isfile(os.path.expanduser("~/.rnsh/allowed_identities")):
            allowed_file = os.path.expanduser("~/.rnsh/allowed_identities")

        await listener.listen(configdir=args.config,
                              command=args.command_line,
                              identitypath=args.identity,
                              service_name=args.service_name,
                              verbosity=args.verbose,
                              quietness=args.quiet,
                              allowed=args.allowed,
                              allowed_file=allowed_file,
                              disable_auth=args.no_auth,
                              announce_period=args.announce,
                              no_remote_command=args.no_remote_cmd,
                              remote_cmd_as_args=args.remote_cmd_as_args)
        return 0

    if args.destination is not None:
        return_code = await initiator.initiate(configdir=args.config,
                                               identitypath=args.identity,
                                               verbosity=args.verbose,
                                               quietness=args.quiet,
                                               noid=args.no_id,
                                               destination=args.destination,
                                               timeout=args.timeout,
                                               command=args.command_line
        )
        return return_code if args.mirror else 0
    else:
        print("")
        print(rnsh.args.usage)
        print("")
        return 1


def rnsh_cli():
    global verbose_set
    return_code = 1
    exc = None
    try:
        return_code = asyncio.run(_rnsh_cli_main())
    except SystemExit:
        pass
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        print(f"Unhandled exception: {ex}")
        exc = ex
    process.tty_unset_reader_callbacks(0)
    if verbose_set and exc:
        raise exc
    sys.exit(return_code if return_code is not None else 255)


if __name__ == "__main__":
    rnsh_cli()
