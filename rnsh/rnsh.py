#!/usr/bin/env python3
import pty
import threading

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

import RNS
import subprocess
import argparse
import shlex
import time
import sys
import tty
import os
import datetime
import select
import base64
import fcntl
import termios
import queue
import signal
import errno
import RNS.vendor.umsgpack as umsgpack

from RNS._version import __version__

APP_NAME = "rnsh"
identity = None
reticulum = None
allow_all = False
allowed_identity_hashes = []
cmd = None
processes = []
processes_lock = threading.Lock()
DATA_AVAIL_MSG = "data available"


def fd_set_non_blocking(fd):
    old_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    if fd.isatty():
        tty.setraw(fd)
    fcntl.fcntl(fd, fcntl.F_SETFL, old_flags | os.O_NONBLOCK)
def fd_non_blocking_read(fd):
    # from https://stackoverflow.com/questions/26263636/how-to-check-potentially-empty-stdin-without-waiting-for-input
    # TODO: Windows is probably different

    #return fd.read()
    try:
        old_settings = None
        # try:
        #     old_settings = termios.tcgetattr(fd)
        # except:
        #     pass
        old_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        try:
            # try:
            #     tty.setraw(fd)
            # except:
            #     pass
            fcntl.fcntl(fd, fcntl.F_SETFL, old_flags | os.O_NONBLOCK)
            return os.read(fd.fileno(), 1024)
        except OSError as ose:
            if ose.errno != 35:
                raise ose
        except Exception as e:
            RNS.log(f"Raw read error {e}")
        finally:
            fcntl.fcntl(fd, fcntl.F_SETFL, old_flags)
            # if old_settings is not None:
                # termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except:
        pass

class NonBlockingStreamReader:

    def __init__(self, stream, callback = None):
        '''
        stream: the stream to read from.
                Usually a process' stdout or stderr.
        '''

        self._s = stream
        self._q = queue.Queue()
        self._callback = callback
        self._stop_time = None

        def _populateQueue(stream, queue):
            '''
            Collect lines from 'stream' and put them in 'quque'.
            '''
            # fd_set_non_blocking(stream)
            run = True
            while run and not (self._stop_time is not None and (datetime.datetime.now() - self._stop_time).total_seconds() > 0.05):
                    # stream.flush()
                    # line = stream.read(1)  #fd_non_blocking_read(stream)
                    timeout = 0.01
                    ready, _, _ = select.select([stream], [], [], timeout)
                    for fd in ready:
                        try:
                            data = os.read(fd, 512)
                        except OSError as e:
                            if e.errno != errno.EIO:
                                raise
                            # EIO means EOF on some systems
                            run = False
                        else:
                            if not data:  # EOF
                                run = False
                            if data is not None and len(data) > 0:
                                if self._callback is not None:
                                    self._callback(data)
                                else:
                                    queue.put(data)
            RNS.log("NonBlockingStreamReader exiting", RNS.LOG_DEBUG)
            os.close(stream)

        self._t = threading.Thread(target = _populateQueue,
                args = (self._s, self._q))
        self._t.daemon = True
        self._t.start() #start collecting lines from the stream

    def read(self, timeout = None):
        try:
            result = self._q.get_nowait() if timeout is None else self._q.get(block = timeout is not None,
                    timeout = timeout)
            return result
        except TimeoutError:
            return None

    def is_open(self):
        return self._t.is_alive()

    def stop(self):
        if self._stop_time is None:
            self._stop_time = datetime.datetime.now()

class UnexpectedEndOfStream(Exception): pass

def processes_get():
    processes_lock.acquire()
    try:
        return processes.copy()
    finally:
        processes_lock.release()

def processes_add(process):
    processes_lock.acquire()
    try:
        processes.append(process)
    finally:
        processes_lock.release()

def processes_remove(process):
    if process.link.status == RNS.Link.ACTIVE:
        return

    processes_lock.acquire()
    try:
        if next(filter(lambda p: p == process, processes)) is not None:
            processes.remove(process)
    finally:
        processes_lock.release()

#### Link Overrides ####
_link_handle_request_orig = RNS.Link.handle_request
def link_handle_request(self, request_id, unpacked_request):
    for process in processes_get():
        if process.link.link_id == self.link_id:
            RNS.log("Associating packet to link", RNS.LOG_DEBUG)
            process.request_id = request_id
    self.last_request_id = request_id
    _link_handle_request_orig(self, request_id, unpacked_request)

RNS.Link.handle_request = link_handle_request

class ProcessState:
    def __init__(self, command, link, remote_identity, term):
        self.lock = threading.RLock()
        self.link = link
        self.remote_identity = remote_identity
        self.term = term
        self.command = command
        self._stderrbuf = bytearray()
        self._stdoutbuf = bytearray()
        RNS.log("Launching " + self.command) # + " for client " + (RNS.prettyhexrep(self.remote_identity) if self.remote_identity else "unknown"), RNS.LOG_DEBUG)
        env = os.environ.copy()
        # env["PYTHONUNBUFFERED"] = "1"
        # env["PS1"] ="\\u:\\h "
        env["TERM"] = self.term
        self.mo, so = pty.openpty()
        self.me, se = pty.openpty()
        self.mi, si = pty.openpty()
        self.process = subprocess.Popen(shlex.split(self.command), bufsize=512, stdin=si, stdout=so, stderr=se, preexec_fn=os.setsid, shell=False, env=env)
        for fd in [so, se, si]:
            os.close(fd)
        # tty.setcbreak(self.mo)
        self.stdout_reader = NonBlockingStreamReader(self.mo, self._stdout_cb)
        self.stderr_reader = NonBlockingStreamReader(self.me, self._stderr_cb)
        self.last_update = datetime.datetime.now()
        self.request_id = None
        self.notify_tried = 0
        self.return_code = None

    def _fd_callback(self, fdbuf, data):
        with self.lock:
            fdbuf.extend(data)

    def _stdout_cb(self, data):
        self._fd_callback(self._stdoutbuf, data)

    def _stderr_cb(self, data):
        self._fd_callback(self._stderrbuf, data)

    def notify_client_data_available(self, chars_available):
        if (datetime.datetime.now() - self.last_update).total_seconds() < 1:
            return

        self.last_update = datetime.datetime.now()
        if self.notify_tried > 15:
            processes_remove(self)
            RNS.log(f"Try count exceeded, terminating connection", RNS.LOG_ERROR)
            self.link.teardown()
            return

        try:
            RNS.log(f"Notifying client; try {self.notify_tried} retcode: {self.return_code} chars avail: {chars_available}")
            RNS.Packet(self.link, DATA_AVAIL_MSG.encode("utf-8")).send()
            self.notify_tried += 1
        except Exception as e:
            RNS.log("Error notifying client: " + str(e), RNS.LOG_ERROR)

    def poll(self, should_notify):
        self.return_code, chars_available = self.process.poll(), len(self._stdoutbuf) + len(self._stderrbuf)

        if should_notify and self.return_code is not None or chars_available > 0:
            self.notify_client_data_available(chars_available)

        if self.return_code is not None:
            self.stdout_reader.stop()
            self.stderr_reader.stop()

        return self.return_code, chars_available

    def is_finished(self):
        with self.lock:
            return self.return_code is not None and not self.stdout_reader.is_open() # and not self.stderr_reader.is_open()

    def read(self): #TODO: limit take sizes?
        with self.lock:
            self.notify_tried = 0
            self.last_update = datetime.datetime.now()
            stdout = self._stdoutbuf
            self._stdoutbuf = bytearray()
            stderr = self._stderrbuf.copy()
            self._stderrbuf = bytearray()
            self.return_code = self.process.poll()
            if self.return_code is not None and len(stdout) == 0 and len(stderr) == 0:
                self.final_checkin = True
            return self.process.poll(), stdout, stderr

    def write(self, bytes):
        os.write(self.mi, bytes)
        os.fsync(self.mi)

    def terminate(self):
        chars_available = 0
        with self.lock:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            for fd in [self.mo, self.me, self.mi]:
                os.close(fd)
            self.process.terminate()
            self.process.wait()
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                self._stdoutbuf += stdout
                self._stderrbuf += stderr
        return len(self._stdoutbuf) + len(self._stderrbuf)

def prepare_identity(identity_path):
    global identity
    if identity_path == None:
        identity_path = RNS.Reticulum.identitypath+"/"+APP_NAME

    if os.path.isfile(identity_path):
        identity = RNS.Identity.from_file(identity_path)                

    if identity == None:
        RNS.log("No valid saved identity found, creating new...", RNS.LOG_INFO)
        identity = RNS.Identity()
        identity.to_file(identity_path)

def listen(configdir, command, identitypath = None, service_name ="default", verbosity = 0, quietness = 0, allowed = [], print_identity = False, disable_auth = None, disable_announce=False):
    global identity, allow_all, allowed_identity_hashes, reticulum, cmd

    cmd = command

    targetloglevel = 3+verbosity-quietness
    reticulum = RNS.Reticulum(configdir=configdir, loglevel=targetloglevel)
    
    prepare_identity(identitypath)
    destination = RNS.Destination(identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, service_name)

    if print_identity:
        print("Identity     : "+str(identity))
        print("Listening on : "+RNS.prettyhexrep(destination.hash))
        exit(0)

    if disable_auth:
        allow_all = True
    else:
        if allowed != None:
            for a in allowed:
                try:
                    dest_len = (RNS.Reticulum.TRUNCATED_HASHLENGTH//8)*2
                    if len(a) != dest_len:
                        raise ValueError("Allowed destination length is invalid, must be {hex} hexadecimal characters ({byte} bytes).".format(hex=dest_len, byte=dest_len//2))
                    try:
                        destination_hash = bytes.fromhex(a)
                        allowed_identity_hashes.append(destination_hash)
                    except Exception as e:
                        raise ValueError("Invalid destination entered. Check your input.")
                except Exception as e:
                    print(str(e))
                    exit(1)

    if len(allowed_identity_hashes) < 1 and not disable_auth:
        print("Warning: No allowed identities configured, rncx will not accept any commands!")

    destination.set_link_established_callback(command_link_established)

    if not allow_all:
        destination.register_request_handler(
            path = service_name,
            response_generator = execute_received_command,
            allow = RNS.Destination.ALLOW_LIST,
            allowed_list = allowed_identity_hashes
        )
    else:
        destination.register_request_handler(
            path = service_name,
            response_generator = execute_received_command,
            allow = RNS.Destination.ALLOW_ALL,
        )

    RNS.log("rnsh listening for commands on "+RNS.prettyhexrep(destination.hash))

    if not disable_announce:
        destination.announce()

    last = datetime.datetime.now()
    
    while True:
        if not disable_announce and (datetime.datetime.now() - last).total_seconds() > 900:  # TODO: make parameter
            last = datetime.datetime.now()
            destination.announce()

        time.sleep(0.005)
        for proc in processes_get():
            try:
                if proc.link.status == RNS.Link.CLOSED:
                    RNS.log("Link closed, terminating")
                    proc.terminate()
                proc.poll(should_notify=True)
            except:
                RNS.log("Error polling process for link " + proc.link.link_id, RNS.LOG_ERROR)

            if proc.link.status == RNS.Link.CLOSED:
                processes_remove(proc)



def command_link_start_process(link, identity, term) -> ProcessState:
    try:
        process = ProcessState(cmd, link, identity, term)
        processes_add(process)
        return process
    except Exception as e:
        RNS.log("Failed to launch process: " + str(e), RNS.LOG_ERROR)
        link.teardown()
def command_link_established(link):
    global allow_all
    link.set_remote_identified_callback(initiator_identified)
    link.set_link_closed_callback(command_link_closed)
    RNS.log("Shell link "+str(link)+" established")
    if allow_all:
        command_link_start_process(link, None)

def command_link_closed(link):
    RNS.log("Shell link "+str(link)+" closed")
    matches = list(filter(lambda p: p.link == link, processes_get()))
    if len(matches) == 0:
        return
    proc = matches[0]
    try:
        proc.terminate()
    except:
        RNS.log("Error closing process for link " + RNS.prettyhexrep(link.link_id), RNS.LOG_ERROR)
    finally:
        processes_remove(proc)

def initiator_identified(link, identity):
    global allow_all, cmd
    RNS.log("Initiator of link "+str(link)+" identified as "+RNS.prettyhexrep(identity.hash))
    if not allow_all and not identity.hash in allowed_identity_hashes:
        RNS.log("Identity "+RNS.prettyhexrep(identity.hash)+" not allowed, tearing down link", RNS.LOG_WARNING)
        link.teardown()

def execute_received_command(path, data, request_id, remote_identity, requested_at):
    RNS.log("execute_received_command", RNS.LOG_DEBUG)
    process = None
    for proc in processes_get():
        RNS.log("checking a proc", RNS.LOG_DEBUG)
        if proc.request_id == request_id:
                process = proc
                RNS.log("execute_received_command matched request", RNS.LOG_DEBUG)

    stdin   = data[0]                  # Data passed to stdin
    if process is None:
        link = next(filter(lambda l: hasattr(l, "last_request_id") and l.last_request_id == request_id, RNS.Transport.active_links))
        if link is not None:
            process = command_link_start_process(link, identity, base64.b64decode(stdin).decode("utf-8") if stdin is not None else "")
            time.sleep(0.1)

    # if remote_identity != None:
    #     RNS.log("Executing command ["+command+"] for "+RNS.prettyhexrep(remote_identity.hash))
    # else:
    #     RNS.log("Executing command ["+command+"] for unknown requestor")

    result    = [
        False,                         # 0: Command was executed
        None,                          # 1: Return value
        None,                          # 2: Stdout
        None,                          # 3: Stderr
        datetime.datetime.now(),       # 4: Timestamp
    ]

    try:
        if process is not None:
            result[0] = not process.is_finished()
            if stdin is not None and len(stdin) > 0:
                stdin = base64.b64decode(stdin)
                process.write(stdin)
            return_code, stdout, stderr = process.read()
            result[1] = return_code
            result[2] = base64.b64encode(stdout).decode("utf-8") if stdout is not None else None
            result[3] = base64.b64encode(stderr).decode("utf-8") if stderr is not None else None

    except Exception as e:
        result[0] = False
        if process is not None:
            process.terminate()
            process.link.teardown()

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
    global identity, reticulum, link, listener_destination, remote_exec_grace

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

    if reticulum == None:
        targetloglevel = 3+verbosity-quietness
        reticulum = RNS.Reticulum(configdir=configdir, loglevel=targetloglevel)

    if identity == None:
        prepare_identity(identitypath)

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
        link.identify(identity)
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
        listen(
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
