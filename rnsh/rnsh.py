#!/usr/bin/env python3
import functools

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
import RNS.vendor.umsgpack as umsgpack
import process
import asyncio
import threading
import signal
import retry

import logging as __logging
module_logger = __logging.getLogger(__name__)
def _getLogger(name: str):
    global module_logger
    return module_logger.getChild(name)

from RNS._version import __version__

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

def _handle_sigint_with_async_int():
    if _finished is not None:
        _finished.set_exception(KeyboardInterrupt())
    else:
        raise KeyboardInterrupt()

signal.signal(signal.SIGINT, _handle_sigint_with_async_int)

def _prepare_identity(identity_path):
    global _identity
    log = _getLogger("_prepare_identity")
    if identity_path == None:
        identity_path = RNS.Reticulum.identitypath+"/"+APP_NAME

    if os.path.isfile(identity_path):
        _identity = RNS.Identity.from_file(identity_path)

    if _identity == None:
        log.info("No valid saved identity found, creating new...")
        _identity = RNS.Identity()
        _identity.to_file(identity_path)

async def _listen(configdir, command, identitypath = None, service_name ="default", verbosity = 0, quietness = 0,
                  allowed = [], print_identity = False, disable_auth = None, disable_announce=False):
    global _identity, _allow_all, _allowed_identity_hashes, _reticulum, _cmd, _destination
    log = _getLogger("_listen")
    _cmd = command

    targetloglevel = 3+verbosity-quietness
    _reticulum = RNS.Reticulum(configdir=configdir, loglevel=targetloglevel)
    
    _prepare_identity(identitypath)
    _destination = RNS.Destination(_identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, service_name)

    if print_identity:
        log.info("Identity     : " + str(_identity))
        log.info("Listening on : " + RNS.prettyhexrep(_destination.hash))
        exit(0)

    if disable_auth:
        _allow_all = True
    else:
        if allowed != None:
            for a in allowed:
                try:
                    dest_len = (RNS.Reticulum.TRUNCATED_HASHLENGTH//8)*2
                    if len(a) != dest_len:
                        raise ValueError("Allowed destination length is invalid, must be {hex} hexadecimal characters ({byte} bytes).".format(hex=dest_len, byte=dest_len//2))
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
            path = service_name,
            response_generator = _listen_request,
            allow = RNS.Destination.ALLOW_LIST,
            allowed_list = _allowed_identity_hashes
        )
    else:
        _destination.register_request_handler(
            path = service_name,
            response_generator = _listen_request,
            allow = RNS.Destination.ALLOW_ALL,
        )

    log.info("rnsh listening for commands on " + RNS.prettyhexrep(_destination.hash))

    if not disable_announce:
        _destination.announce()

    last = time.monotonic()

    try:
        while True:
            if not disable_announce and time.monotonic() - last > 900:  # TODO: make parameter
                last = datetime.datetime.now()
                _destination.announce()
            try:
                await asyncio.wait_for(_finished, timeout=1.0)
            except TimeoutError:
                pass
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

        self._log = _getLogger(self.__class__.__name__)
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
        self._term_state: [int] | None = None

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
            take = self._data_buffer[:count-1]
            self._data_buffer = self._data_buffer[count:]
            return take

    def _stdout_data(self, data: bytes):
        total_available = 0
        with self.lock:
            self._data_buffer.extend(data)
            total_available = len(self._data_buffer)
        try:
            self._data_available_cb(total_available)
        except Exception as e:
            self._log.error(f"Error calling ProcessState data_available_callback {e}")

    def _update_winsz(self):
        self.process.set_winsize(self._term_state[3],
                                 self._term_state[4],
                                 self._term_state[5],
                                 self._term_state[6])


    REQUEST_IDX_STDIN = 0
    REQUEST_IDX_TERM  = 1
    REQUEST_IDX_TIOS  = 2
    REQUEST_IDX_ROWS  = 3
    REQUEST_IDX_COLS  = 4
    REQUEST_IDX_HPIX  = 5
    REQUEST_IDX_VPIX  = 6
    def process_request(self, data: [any], read_size: int) -> [any]:
        stdin = data[ProcessState.REQUEST_IDX_STDIN]  # Data passed to stdin
        term  = data[ProcessState.REQUEST_IDX_TERM]   # TERM environment variable
        tios  = data[ProcessState.REQUEST_IDX_TIOS]   # termios attr
        rows  = data[ProcessState.REQUEST_IDX_ROWS]   # window rows
        cols  = data[ProcessState.REQUEST_IDX_COLS]   # window cols
        hpix  = data[ProcessState.REQUEST_IDX_HPIX]   # window horizontal pixels
        vpix  = data[ProcessState.REQUEST_IDX_VPIX]   # window vertical pixels
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
    RESPONSE_IDX_STDOUT  = 3
    RESPONSE_IDX_TMSTAMP = 4
    @staticmethod
    def default_response() -> [any]:
        return [
            False,                         # 0: Process running
            None,                          # 1: Return value
            0,                             # 2: Number of outstanding bytes
            None,                          # 3: Stdout/Stderr
            time.time(),                   # 4: Timestamp
        ]


def _subproc_data_ready(link: RNS.Link, chars_available: int):
    global _retry_timer
    log = _getLogger("_subproc_data_ready")
    process_state: ProcessState = link.process

    def send(timeout: bool, id: any, tries: int) -> any:
        try:
            pr = process_state.pending_receipt_take()
            if pr is not None and pr.get_status() != RNS.PacketReceipt.SENT and pr.get_status() != RNS.PacketReceipt.DELIVERED:
                if not timeout:
                    _retry_timer.complete(id)
                log.debug(f"Packet {id} completed with status {pr.status} on link {link}")
                return link.link_id

            if not timeout:
                log.info(f"Notifying client try {tries} (retcode: {process_state.return_code} chars avail: {chars_available})")
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
                               id=None)
        else:
            log.debug(f"Notification already pending for link {link}")

def _subproc_terminated(link: RNS.Link, return_code: int):
    log = _getLogger("_subproc_terminated")
    log.info(f"Subprocess terminated ({return_code} for link {link}")
    link.teardown()

def _listen_start_proc(link: RNS.Link, term: str) -> ProcessState | None:
    global _cmd
    log = _getLogger("_listen_start_proc")
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
    log = _getLogger("_listen_link_established")
    link.set_remote_identified_callback(_initiator_identified)
    link.set_link_closed_callback(_listen_link_closed)
    log.info("Link "+str(link)+" established")

def _listen_link_closed(link: RNS.Link):
    log = _getLogger("_listen_link_closed")
    # async def cleanup():
    log.info("Link "+str(link)+" closed")
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
    log = _getLogger("_initiator_identified")
    log.info("Initiator of link "+str(link)+" identified as "+RNS.prettyhexrep(identity.hash))
    if not _allow_all and not identity.hash in _allowed_identity_hashes:
        log.warning("Identity "+RNS.prettyhexrep(identity.hash)+" not allowed, tearing down link", RNS.LOG_WARNING)
        link.teardown()

def _listen_request(path, data, request_id, link_id, remote_identity, requested_at):
    global _destination, _retry_timer
    log = _getLogger("_listen_request")
    log.debug(f"listen_execute {path} {request_id} {link_id} {remote_identity}, {requested_at}")
    _retry_timer.complete(link_id)
    link: RNS.Link = next(filter(lambda l: l.link_id == link_id, _destination.links))
    if link is None:
        log.error(f"invalid request {request_id}, no link found with id {link_id}")
        return
    process_state: ProcessState | None = None
    try:
        term = data[1]
        process_state = link.process if hasattr(link, "process") else None
        if process_state is None:
            log.debug(f"process not found for link {link}")
            process_state = _listen_start_proc(link, term)


        # leave significant overhead for metadata and encoding
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

def spin(until=None, msg=None, timeout=None):
    i = 0
    syms = "⢄⢂⢁⡁⡈⡐⡠"
    if timeout != None:
        timeout = time.time()+timeout

    # print(msg+"  ", end=" ")
    while (timeout == None or time.time()<timeout) and not until():
        time.sleep(0.1)
        # print(("\b\b"+syms[i]+" "), end="")
        sys.stdout.flush()
        i = (i+1)%len(syms)

    # print("\r"+" "*len(msg)+"  \r", end="")

    if timeout != None and time.time() > timeout:
        return False
    else:
        return True

current_progress = 0.0
stats = []
speed = 0.0
def spin_stat(until=None, timeout=None):
    global current_progress, response_transfer_size, speed
    i = 0
    syms = "⢄⢂⢁⡁⡈⡐⡠"
    if timeout != None:
        timeout = time.time()+timeout

    while (timeout == None or time.time()<timeout) and not until():
        time.sleep(0.1)
        prg = current_progress
        percent = round(prg * 100.0, 1)
        stat_str = str(percent)+"% - " + size_str(int(prg*response_transfer_size)) + " of " + size_str(response_transfer_size) + " - " +size_str(speed, "b")+"ps"
        # print("\r                                                                                  \rReceiving result "+syms[i]+" "+stat_str, end=" ")
        #
        # sys.stdout.flush()
        i = (i+1)%len(syms)

    # print("\r                                                                                  \r", end="")

    if timeout != None and time.time() > timeout:
        return False
    else:
        return True

def remote_execution_done(request_receipt):
    pass

def remote_execution_progress(request_receipt):
    stats_max = 32
    global current_progress, response_transfer_size, speed
    current_progress = request_receipt.progress
    response_transfer_size = request_receipt.response_transfer_size
    now = time.time()
    got = current_progress*response_transfer_size
    entry = [now, got]
    stats.append(entry)
    while len(stats) > stats_max:
        stats.pop(0)

    span = now - stats[0][0]
    if span == 0:
        speed = 0
    else:
        diff = got - stats[0][1]
        speed = diff/span

link = None
listener_destination = None
remote_exec_grace = 2.0
new_data = False

def client_packet_handler(message, packet):
    global new_data
    if message is not None and message.decode("utf-8") == DATA_AVAIL_MSG:
            new_data = True
def execute(configdir, identitypath = None, verbosity = 0, quietness = 0, noid = False, destination = None, service_name = "default", stdin = None, timeout = RNS.Transport.PATH_REQUEST_TIMEOUT):
    global _identity, _reticulum, link, listener_destination, remote_exec_grace

    try:
        dest_len = (RNS.Reticulum.TRUNCATED_HASHLENGTH//8)*2
        if len(destination) != dest_len:
            raise ValueError("Allowed destination length is invalid, must be {hex} hexadecimal characters ({byte} bytes).".format(hex=dest_len, byte=dest_len//2))
        try:
            destination_hash = bytes.fromhex(destination)
        except Exception as e:
            raise ValueError("Invalid destination entered. Check your input.")
    except Exception as e:
        print(str(e))
        return 241

    if _reticulum == None:
        targetloglevel = 3+verbosity-quietness
        _reticulum = RNS.Reticulum(configdir=configdir, loglevel=targetloglevel)

    if _identity == None:
        _prepare_identity(identitypath)

    if not RNS.Transport.has_path(destination_hash):
        RNS.Transport.request_path(destination_hash)
        if not spin(until=lambda: RNS.Transport.has_path(destination_hash), msg="Path to "+RNS.prettyhexrep(destination_hash)+" requested", timeout=timeout):
            print("Path not found")
            return 242

    if listener_destination == None:
        listener_identity = RNS.Identity.recall(destination_hash)
        listener_destination = RNS.Destination(
            listener_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            APP_NAME,
            service_name
        )

    if link == None or link.status == RNS.Link.PENDING:
        link = RNS.Link(listener_destination)
        link.did_identify = False

    if not spin(until=lambda: link.status == RNS.Link.ACTIVE, msg="Establishing link with "+RNS.prettyhexrep(destination_hash), timeout=timeout):
        print("Could not establish link with "+RNS.prettyhexrep(destination_hash))
        return 243

    if not noid and not link.did_identify:
        link.identify(_identity)
        link.did_identify = True

    link.set_packet_callback(client_packet_handler)

    # if stdin != None:
    #     stdin = stdin.encode("utf-8")

    request_data = [
        (base64.b64encode(stdin) if stdin is not None else None),                    # Data passed to stdin
    ]

    # TODO: Tune
    rexec_timeout = timeout+link.rtt*4+remote_exec_grace

    request_receipt = link.request(
        path=service_name,
        data=request_data,
        response_callback=remote_execution_done,
        failed_callback=remote_execution_done,
        progress_callback=remote_execution_progress,
        timeout=rexec_timeout
    )

    spin(
        until=lambda:link.status == RNS.Link.CLOSED or (request_receipt.status != RNS.RequestReceipt.FAILED and request_receipt.status != RNS.RequestReceipt.SENT),
        msg="Sending execution request",
        timeout=rexec_timeout+0.5
    )

    if link.status == RNS.Link.CLOSED:
        print("Could not request remote execution, link was closed")
        return 244

    if request_receipt.status == RNS.RequestReceipt.FAILED:
        print("Could not request remote execution")
        return 245

    spin(
        until=lambda:request_receipt.status != RNS.RequestReceipt.DELIVERED,
        msg="Command delivered, awaiting result",
        timeout=timeout
    )

    if request_receipt.status == RNS.RequestReceipt.FAILED:
        print("No result was received")
        return 246

    # spin_stat(
    #     until=lambda:request_receipt.status != RNS.RequestReceipt.RECEIVING,
    #     timeout=result_timeout
    # )

    if request_receipt.status == RNS.RequestReceipt.FAILED:
        print("Receiving result failed")
        return 247

    if request_receipt.response != None:
        try:
            running = request_receipt.response[0]
            retval = request_receipt.response[1]
            stdout = request_receipt.response[2]
            stderr = request_receipt.response[3]
            timestamp = request_receipt.response[4]
            # print("data: " + (stdout.decode("utf-8") if stdout is not None else ""))
        except Exception as e:
            print("Received invalid result: " + str(e))
            return 248

        if stdout is not None:
            stdout = base64.b64decode(stdout)
            # print(f"stdout: {stdout}")
            os.write(sys.stdout.buffer.fileno(), stdout)
            # print(stdout.decode("utf-8"), end="")
        if stderr is not None:
            stderr = base64.b64decode(stderr)
            # print(f"stderr: {stderr}")
            os.write(sys.stderr.buffer.fileno(), stderr)
            # print(stderr.decode("utf-8"), file=sys.stderr, end="")

        sys.stdout.buffer.flush()
        sys.stdout.flush()
        sys.stderr.buffer.flush()
        sys.stderr.flush()

        if not running and retval is not None:
            return retval

        return None

def main():
    global new_data
    parser = argparse.ArgumentParser(description="Reticulum Remote Execution Utility")
    parser.add_argument("destination", nargs="?", default=None, help="hexadecimal hash of the listener", type=str)
    parser.add_argument("-c", "--command", nargs="?", default="/bin/zsh", help="command to be execute", type=str)
    parser.add_argument("--config", metavar="path", action="store", default=None, help="path to alternative Reticulum config directory", type=str)
    parser.add_argument("-s", "--service-name", action="store", default="default", help="service name for connection")
    parser.add_argument('-v', '--verbose', action='count', default=0, help="increase verbosity")
    parser.add_argument('-q', '--quiet', action='count', default=0, help="decrease verbosity")
    parser.add_argument('-p', '--print-identity', action='store_true', default=False, help="print identity and destination info and exit")
    parser.add_argument("-l", '--listen', action='store_true', default=False, help="listen for incoming commands")
    parser.add_argument('-i', metavar="identity", action='store', dest="identity", default=None, help="path to identity to use", type=str)
    parser.add_argument("-x", '--interactive', action='store_true', default=False, help="enter interactive mode")
    parser.add_argument("-b", '--no-announce', action='store_true', default=False, help="don't announce at program start")
    parser.add_argument('-a', metavar="allowed_hash", dest="allowed", action='append', help="accept from this identity", type=str)
    parser.add_argument('-n', '--noauth', action='store_true', default=False, help="accept commands from anyone")
    parser.add_argument('-N', '--noid', action='store_true', default=False, help="don't identify to listener")
    parser.add_argument("-d", '--detailed', action='store_true', default=False, help="show detailed result output")
    parser.add_argument("-m", action='store_true', dest="mirror", default=False, help="mirror exit code of remote command")
    parser.add_argument("-w", action="store", metavar="seconds", type=float, help="connect and request timeout before giving up", default=RNS.Transport.PATH_REQUEST_TIMEOUT)
    parser.add_argument("-W", action="store", metavar="seconds", type=float, help="max result download time", default=None)
    parser.add_argument("--stdin", action='store', default=None, help="pass input to stdin", type=str)
    parser.add_argument("--stdout", action='store', default=None, help="max size in bytes of returned stdout", type=int)
    parser.add_argument("--stderr", action='store', default=None, help="max size in bytes of returned stderr", type=int)
    parser.add_argument("--version", action="version", version="rnx {version}".format(version=__version__))

    args = parser.parse_args()

    if args.listen or args.print_identity:
        RNS.log("command " + args.command)
        _listen(
            configdir=args.config,
            command=args.command,
            identitypath=args.identity,
            service_name=args.service_name,
            verbosity=args.verbose,
            quietness=args.quiet,
            allowed=args.allowed,
            print_identity=args.print_identity,
            disable_auth=args.noauth,
            disable_announce=args.no_announce,
        )

    if args.destination is not None and args.service_name is not None:
        # command_history_max = 5000
        # command_history = []
        # command_current = ""
        # history_idx = 0
        # tty.setcbreak(sys.stdin.fileno())

        fr = execute(
            configdir=args.config,
            identitypath=args.identity,
            verbosity=args.verbose,
            quietness=args.quiet,
            noid=args.noid,
            destination=args.destination,
            service_name=args.service_name,
            stdin=os.environ["TERM"].encode("utf-8"),
            timeout=args.w,
        )

        if fr is not None:
            print(f"Remote returned result {fr}")
            exit(1)

        last = datetime.datetime.now()
        #reader = NonBlockingStreamReader(sys.stdin.fileno())
        while True: # reader.is_open() and (link is None or link.status != RNS.Link.CLOSED):
            stdin = bytearray()
            # try:
            #     try:
            #         # while True:
            #         #     got = reader.read()
            #         #     if got is None:
            #         #         break
            #         #     stdin.extend(got.encode("utf-8"))
            #
            #     except:
            #         pass
            #
            # except KeyboardInterrupt:
            #     stdin.extend("\x03".encode("utf-8"))
            # except EOFError:
            #     stdin.extend("\x04".encode("utf-8"))

            if new_data or (datetime.datetime.now() - last).total_seconds() > 5 or link is None or (stdin is not None and len(stdin) > 0):
                last = datetime.datetime.now()
                new_data = False
                result = execute(
                    configdir=args.config,
                    identitypath=args.identity,
                    verbosity=args.verbose,
                    quietness=args.quiet,
                    noid=args.noid,
                    destination=args.destination,
                    service_name=args.service_name,
                    stdin=stdin,
                    timeout=args.w,
                )
                # print("|", end="")
                if result is not None:
                    break
            time.sleep(0.010)
        if link is not None:
            link.teardown()

    else:
        print("")
        parser.print_help()
        print("")

    # except KeyboardInterrupt:
    #     pass
        # # tty.setnocbreak(sys.stdin.fileno())
        # print("")
        # if link != None:
        #     link.teardown()
        # exit()

def size_str(num, suffix='B'):
    units = ['','K','M','G','T','P','E','Z']
    last_unit = 'Y'

    if suffix == 'b':
        num *= 8
        units = ['','K','M','G','T','P','E','Z']
        last_unit = 'Y'

    for unit in units:
        if abs(num) < 1000.0:
            if unit == "":
                return "%.0f %s%s" % (num, unit, suffix)
            else:
                return "%.2f %s%s" % (num, unit, suffix)
        num /= 1000.0

    return "%.2f%s%s" % (num, last_unit, suffix)

def pretty_time(time, verbose=False):
    days = int(time // (24 * 3600))
    time = time % (24 * 3600)
    hours = int(time // 3600)
    time %= 3600
    minutes = int(time // 60)
    time %= 60
    seconds = round(time, 2)

    ss = "" if seconds == 1 else "s"
    sm = "" if minutes == 1 else "s"
    sh = "" if hours == 1 else "s"
    sd = "" if days == 1 else "s"

    components = []
    if days > 0:
        components.append(str(days)+" day"+sd if verbose else str(days)+"d")

    if hours > 0:
        components.append(str(hours)+" hour"+sh if verbose else str(hours)+"h")

    if minutes > 0:
        components.append(str(minutes)+" minute"+sm if verbose else str(minutes)+"m")

    if seconds > 0:
        components.append(str(seconds)+" second"+ss if verbose else str(seconds)+"s")

    i = 0
    tstr = ""
    for c in components:
        i += 1
        if i == 1:
            pass
        elif i < len(components):
            tstr += ", "
        elif i == len(components):
            tstr += " and "

        tstr += c

    return tstr

if __name__ == "__main__":
    main()
