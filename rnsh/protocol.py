from __future__ import annotations

import enum
import queue
import threading
import time
import typing
import uuid
from types import TracebackType
from typing import Type, Callable, TypeVar, Tuple
import RNS
from RNS.vendor import umsgpack
import rnsh.retry
import abc
import contextlib
import struct
import logging as __logging

module_logger = __logging.getLogger(__name__)


_TReceipt = TypeVar("_TReceipt")
_TLink = TypeVar("_TLink")
MSG_MAGIC = 0xac
PROTOCOL_VERSION=1


def _make_MSGTYPE(val: int):
    return ((MSG_MAGIC << 8) & 0xff00) | (val & 0x00ff)


class METype(enum.IntEnum):
    ME_NO_MSG_TYPE = 0
    ME_INVALID_MSG_TYPE = 1
    ME_NOT_REGISTERED = 2
    ME_LINK_NOT_READY = 3
    ME_ALREADY_SENT = 4


class MessagingException(Exception):
    def __init__(self, type: METype, *args):
        super().__init__(args)
        self.type = type


class MessageState(enum.IntEnum):
    MSGSTATE_NEW       = 0
    MSGSTATE_SENT      = 1
    MSGSTATE_DELIVERED = 2
    MSGSTATE_FAILED    = 3


class Message(abc.ABC):
    MSGTYPE = None

    def __init__(self):
        self.ts = time.time()
        self.msgid = uuid.uuid4()
        self.raw: bytes | None = None
        self.receipt: _TReceipt = None
        self.link: _TLink = None
        self.tracked: bool = False

    def __str__(self):
        return f"{self.__class__.__name__} {self.msgid}"

    @abc.abstractmethod
    def pack(self) -> bytes:
        raise NotImplemented()

    @abc.abstractmethod
    def unpack(self, raw):
        raise NotImplemented()

    def unwrap_MSGTYPE(self, raw: bytes) -> bytes:
        if self.MSGTYPE is None:
            raise MessagingException(METype.ME_NO_MSG_TYPE, f"{self.__class__} lacks MSGTYPE")
        mid, raw = self.static_unwrap_MSGTYPE(raw)
        if mid != self.MSGTYPE:
            raise MessagingException(METype.ME_INVALID_MSG_TYPE,
                                     f"invalid msg id, expected {hex(self.MSGTYPE)} got {hex(mid)}")
        return raw

    def wrap_MSGTYPE(self, raw: bytes) -> bytes:
        if self.__class__.MSGTYPE is None:
            raise MessagingException(METype.ME_NO_MSG_TYPE, f"{self.__class__} lacks MSGTYPE")
        return struct.pack(">H", self.MSGTYPE) + raw

    @staticmethod
    def static_unwrap_MSGTYPE(raw: bytes) -> (int, bytes):
        return struct.unpack(">H", raw[:2])[0], raw[2:]


class NoopMessage(Message):
    MSGTYPE = _make_MSGTYPE(0)

    def pack(self) -> bytes:
        return self.wrap_MSGTYPE(bytes())

    def unpack(self, raw):
        self.unwrap_MSGTYPE(raw)


class WindowSizeMessage(Message):
    MSGTYPE = _make_MSGTYPE(2)

    def __init__(self, rows: int = None, cols: int = None, hpix: int = None, vpix: int = None):
        super().__init__()
        self.rows = rows
        self.cols = cols
        self.hpix = hpix
        self.vpix = vpix

    def pack(self) -> bytes:
        raw = umsgpack.packb((self.rows, self.cols, self.hpix, self.vpix))
        return self.wrap_MSGTYPE(raw)

    def unpack(self, raw):
        raw = self.unwrap_MSGTYPE(raw)
        self.rows, self.cols, self.hpix, self.vpix = umsgpack.unpackb(raw)


class ExecuteCommandMesssage(Message):
    MSGTYPE = _make_MSGTYPE(3)

    def __init__(self, cmdline: [str] = None, pipe_stdin: bool = False, pipe_stdout: bool = False,
                 pipe_stderr: bool = False, tcflags: [any] = None, term: str | None = None):
        super().__init__()
        self.cmdline = cmdline
        self.pipe_stdin = pipe_stdin
        self.pipe_stdout = pipe_stdout
        self.pipe_stderr = pipe_stderr
        self.tcflags = tcflags
        self.term = term

    def pack(self) -> bytes:
        raw = umsgpack.packb((self.cmdline, self.pipe_stdin, self.pipe_stdout, self.pipe_stderr,
                              self.tcflags, self.term))
        return self.wrap_MSGTYPE(raw)

    def unpack(self, raw):
        raw = self.unwrap_MSGTYPE(raw)
        self.cmdline, self.pipe_stdin, self.pipe_stdout, self.pipe_stderr, self.tcflags, self.term \
            = umsgpack.unpackb(raw)

class StreamDataMessage(Message):
    MSGTYPE = _make_MSGTYPE(4)
    STREAM_ID_STDIN  = 0
    STREAM_ID_STDOUT = 1
    STREAM_ID_STDERR = 2

    def __init__(self, stream_id: int = None, data: bytes = None, eof: bool = False):
        super().__init__()
        self.stream_id = stream_id
        self.data = data
        self.eof = eof

    def pack(self) -> bytes:
        raw = umsgpack.packb((self.stream_id, self.eof, self.data))
        return self.wrap_MSGTYPE(raw)

    def unpack(self, raw):
        raw = self.unwrap_MSGTYPE(raw)
        self.stream_id, self.eof, self.data = umsgpack.unpackb(raw)


class VersionInfoMessage(Message):
    MSGTYPE = _make_MSGTYPE(5)

    def __init__(self, sw_version: str = None):
        super().__init__()
        self.sw_version = sw_version
        self.protocol_version = PROTOCOL_VERSION

    def pack(self) -> bytes:
        raw = umsgpack.packb((self.sw_version, self.protocol_version))
        return self.wrap_MSGTYPE(raw)

    def unpack(self, raw):
        raw = self.unwrap_MSGTYPE(raw)
        self.sw_version, self.protocol_version = umsgpack.unpackb(raw)


class ErrorMessage(Message):
    MSGTYPE = _make_MSGTYPE(6)

    def __init__(self, msg: str = None, fatal: bool = False, data: dict = None):
        super().__init__()
        self.msg = msg
        self.fatal = fatal
        self.data = data

    def pack(self) -> bytes:
        raw = umsgpack.packb((self.msg, self.fatal, self.data))
        return self.wrap_MSGTYPE(raw)

    def unpack(self, raw: bytes):
        raw = self.unwrap_MSGTYPE(raw)
        self.msg, self.fatal, self.data = umsgpack.unpackb(raw)


class Messenger(contextlib.AbstractContextManager):

    @staticmethod
    def _get_msg_constructors() -> (int, Type[Message]):
        subclass_tuples = []
        for subclass in Message.__subclasses__():
            subclass_tuples.append((subclass.MSGTYPE, subclass))
        return subclass_tuples

    def __init__(self, receipt_checker: Callable[[_TReceipt], MessageState],
                 link_timeout_callback: Callable[[_TLink], None],
                 link_mdu_getter: Callable[[_TLink], int],
                 link_rtt_getter: Callable[[_TLink], float],
                 link_usable_getter: Callable[[_TLink], bool],
                 packet_sender: Callable[[_TLink, bytes], _TReceipt],
                 retry_delay_min: float = 10.0):
        self._log = module_logger.getChild(self.__class__.__name__)
        self._receipt_checker = receipt_checker
        self._link_timeout_callback = link_timeout_callback
        self._link_mdu_getter = link_mdu_getter
        self._link_rtt_getter = link_rtt_getter
        self._link_usable_getter = link_usable_getter
        self._packet_sender = packet_sender
        self._sent_messages: list[Message] = []
        self._lock = threading.RLock()
        self._retry_timer = rnsh.retry.RetryThread()
        self._message_factories = dict(self.__class__._get_msg_constructors())
        self._inbound_queue = queue.Queue()
        self._retry_delay_min = retry_delay_min

    def __enter__(self):
        pass

    def __exit__(self, __exc_type: Type[BaseException] | None, __exc_value: BaseException | None,
                 __traceback: TracebackType | None) -> bool | None:
        self.shutdown()
        return False

    def shutdown(self):
        self._run = False
        self._retry_timer.close()

    def inbound(self, raw: bytes):
        (mid, contents) = Message.static_unwrap_MSGTYPE(raw)
        ctor = self._message_factories.get(mid, None)
        if ctor is None:
            raise MessagingException(METype.ME_NOT_REGISTERED, f"unable to find constructor for message type {hex(mid)}")
        message = ctor()
        message.unpack(raw)
        self._log.debug("Message received: {message}")
        self._inbound_queue.put(message)

    def get_mdu(self, link: _TLink) -> int:
        return self._link_mdu_getter(link) - 4

    def get_rtt(self, link: _TLink) -> float:
        return self._link_rtt_getter(link)

    def is_link_ready(self, link: _TLink) -> bool:
        if not self._link_usable_getter(link):
            return False

        with self._lock:
            for message in self._sent_messages:
                if message.link == link:
                    return False
        return True

    def send_message(self, link: _TLink, message: Message):
        with self._lock:
            if not self.is_link_ready(link):
                raise MessagingException(METype.ME_LINK_NOT_READY, f"link {link} not ready")

            if message in self._sent_messages:
                raise MessagingException(METype.ME_ALREADY_SENT)
            self._sent_messages.append(message)
            message.tracked = True

        if not message.raw:
            message.raw = message.pack()
        message.link = link

        def send(tag: any, tries: int):
            state = MessageState.MSGSTATE_NEW if not message.receipt else self._receipt_checker(message.receipt)
            if state in [MessageState.MSGSTATE_NEW, MessageState.MSGSTATE_FAILED]:
                try:
                    self._log.debug(f"Sending packet for {message}")
                    message.receipt = self._packet_sender(link, message.raw)
                except Exception as ex:
                    self._log.exception(f"Error sending message {message}")
            elif state in [MessageState.MSGSTATE_SENT]:
                self._log.debug(f"Retry skipped, message still pending {message}")
            elif state in [MessageState.MSGSTATE_DELIVERED]:
                latency = round(time.time() - message.ts, 1)
                self._log.debug(f"Message delivered {message.msgid} after {tries-1} tries/{latency} seconds")
                with self._lock:
                    self._sent_messages.remove(message)
                    message.tracked = False
                self._retry_timer.complete(link)
            return link

        def timeout(tag: any, tries: int):
            latency = round(time.time() - message.ts, 1)
            msg = "delivered" if message.receipt and self._receipt_checker(message.receipt) == MessageState.MSGSTATE_DELIVERED else "retry timeout"
            self._log.debug(f"Message {msg} {message} after {tries} tries/{latency} seconds")
            with self._lock:
                self._sent_messages.remove(message)
                message.tracked = False
            self._link_timeout_callback(link)

        rtt = self._link_rtt_getter(link)
        self._retry_timer.begin(5, min(rtt * 100, max(rtt * 2, self._retry_delay_min)), send, timeout)

    def poll_inbound(self, block: bool = True, timeout: float = None) -> Message | None:
        try:
            return self._inbound_queue.get(block=block, timeout=timeout)
        except queue.Empty:
            return None


