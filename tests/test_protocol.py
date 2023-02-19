from __future__ import annotations

import logging

from rnsh.protocol import _TReceipt, MessageState
from typing import Callable

logging.getLogger().setLevel(logging.DEBUG)

import rnsh.protocol
import contextlib
import typing
import types
import time
import uuid


module_logger = logging.getLogger(__name__)


class Receipt:
    def __init__(self, state: rnsh.protocol.MessageState, raw: bytes):
        self.state = state
        self.raw = raw
        self.msgid = uuid.uuid4()
        self.tries = 1


class MessageOutletTest(rnsh.protocol.MessageOutletBase):
    def __init__(self, mdu: int, rtt: float):
        self.link_id = uuid.uuid4()
        self.timeout_callbacks = 0
        self._mdu = mdu
        self._rtt = rtt
        self._usable = True
        self.receipts = []
        self.packet_callback: Callable[[rnsh.protocol.MessageOutletBase, bytes], None] | None = None

    def send(self, raw: bytes) -> Receipt:
        receipt = Receipt(rnsh.protocol.MessageState.MSGSTATE_SENT, raw)
        self.receipts.append(receipt)
        return receipt

    def resend(self, receipt: Receipt) -> Receipt:
        receipt.tries += 1
        receipt.state = rnsh.protocol.MessageState.MSGSTATE_SENT
        return receipt

    def set_packet_received_callback(self, cb: Callable[[rnsh.protocol.MessageOutletBase, bytes], None]):
        self.packet_callback = cb

    def receive(self, raw: bytes):
        if self.packet_callback:
            self.packet_callback(self, raw)

    @property
    def mdu(self):
        return self._mdu

    @property
    def rtt(self):
        return self._rtt

    @property
    def is_usuable(self):
        return self._usable

    def get_receipt_state(self, receipt: Receipt) -> MessageState:
        return receipt.state

    def timed_out(self):
        self.timeout_callbacks += 1

    def __str__(self):
        return str(self.link_id)


class ProtocolHarness(contextlib.AbstractContextManager):
    def __init__(self, retry_delay_min: float = 1):
        self._log = module_logger.getChild(self.__class__.__name__)
        self.messenger = rnsh.protocol.Messenger(retry_delay_min=retry_delay_min)

    def cleanup(self):
        self.messenger.shutdown()

    def __exit__(self, __exc_type: typing.Type[BaseException], __exc_value: BaseException,
                 __traceback: types.TracebackType) -> bool:
        # self._log.debug(f"__exit__({__exc_type}, {__exc_value}, {__traceback})")
        self.cleanup()
        return False


def test_send_one_retry():
    rtt = 0.001
    retry_interval = rtt * 150
    message_content = b'Test'
    with ProtocolHarness(retry_delay_min=retry_interval) as h:
        outlet = MessageOutletTest(mdu=500, rtt=rtt)
        message = rnsh.protocol.StreamDataMessage(stream_id=rnsh.protocol.StreamDataMessage.STREAM_ID_STDIN,
                                                  data=message_content, eof=True)
        assert len(outlet.receipts) == 0
        h.messenger.send(outlet, message)
        assert message.tracked
        assert message.raw is not None
        assert len(outlet.receipts) == 1
        receipt = outlet.receipts[0]
        assert receipt.state == rnsh.protocol.MessageState.MSGSTATE_SENT
        assert receipt.raw == message.raw
        time.sleep(retry_interval * 1.5)
        assert len(outlet.receipts) == 1
        receipt.state = rnsh.protocol.MessageState.MSGSTATE_FAILED
        module_logger.info("set failed")
        time.sleep(retry_interval)
        assert len(outlet.receipts) == 1
        assert receipt == outlet.receipts[0]
        assert receipt.state == rnsh.protocol.MessageState.MSGSTATE_SENT
        assert receipt.tries == 2
        receipt.state = rnsh.protocol.MessageState.MSGSTATE_DELIVERED
        time.sleep(retry_interval)
        assert len(outlet.receipts) == 1
        assert not message.tracked


def test_send_timeout():
    rtt = 0.001
    retry_interval = rtt * 150
    message_content = b'Test'
    with ProtocolHarness(retry_delay_min=retry_interval) as h:
        outlet = MessageOutletTest(mdu=500, rtt=rtt)
        message = rnsh.protocol.StreamDataMessage(stream_id=rnsh.protocol.StreamDataMessage.STREAM_ID_STDIN,
                                                  data=message_content, eof=True)
        assert len(outlet.receipts) == 0
        h.messenger.send(outlet, message)
        assert message.tracked
        assert message.raw is not None
        assert len(outlet.receipts) == 1
        receipt = outlet.receipts[0]
        assert receipt.state == rnsh.protocol.MessageState.MSGSTATE_SENT
        assert receipt.raw == message.raw
        time.sleep(retry_interval * 1.5)
        assert outlet.timeout_callbacks == 0
        time.sleep(retry_interval * 7)
        assert len(outlet.receipts) == 1
        assert outlet.timeout_callbacks == 1
        assert receipt.state == rnsh.protocol.MessageState.MSGSTATE_SENT
        assert not message.tracked


def eat_own_dog_food(message: rnsh.protocol.Message, checker: typing.Callable[[rnsh.protocol.Message], None]):
    rtt = 0.001
    retry_interval = rtt * 150
    with ProtocolHarness(retry_delay_min=retry_interval) as h:

        decoded: [rnsh.protocol.Message] = []
        def packet(outlet, buffer):
            decoded.append(h.messenger.receive(buffer))

        outlet = MessageOutletTest(mdu=500, rtt=rtt)
        outlet.set_packet_received_callback(packet)
        assert len(outlet.receipts) == 0
        h.messenger.send(outlet, message)
        assert message.tracked
        assert message.raw is not None
        assert len(outlet.receipts) == 1
        receipt = outlet.receipts[0]
        assert receipt.state == rnsh.protocol.MessageState.MSGSTATE_SENT
        assert receipt.raw == message.raw
        module_logger.info("set delivered")
        receipt.state = rnsh.protocol.MessageState.MSGSTATE_DELIVERED
        time.sleep(retry_interval * 2)
        assert len(outlet.receipts) == 1
        assert receipt.state == rnsh.protocol.MessageState.MSGSTATE_DELIVERED
        assert not message.tracked
        module_logger.info("injecting rx message")
        assert len(decoded) == 0
        outlet.receive(message.raw)
        assert len(decoded) == 1
        rx_message = decoded[0]
        assert rx_message is not None
        assert isinstance(rx_message, message.__class__)
        assert rx_message.msgid != message.msgid
        checker(rx_message)


def test_send_receive_streamdata():
    message = rnsh.protocol.StreamDataMessage(stream_id=rnsh.protocol.StreamDataMessage.STREAM_ID_STDIN,
                                              data=b'Test', eof=True)

    def check(rx_message: rnsh.protocol.Message):
        assert isinstance(rx_message, message.__class__)
        assert rx_message.stream_id == message.stream_id
        assert rx_message.data == message.data
        assert rx_message.eof == message.eof

    eat_own_dog_food(message, check)


def test_send_receive_noop():
    message = rnsh.protocol.NoopMessage()

    def check(rx_message: rnsh.protocol.Message):
        assert isinstance(rx_message, message.__class__)

    eat_own_dog_food(message, check)


def test_send_receive_execute():
    message = rnsh.protocol.ExecuteCommandMesssage(cmdline=["test", "one", "two"],
                                                   pipe_stdin=False,
                                                   pipe_stdout=True,
                                                   pipe_stderr=False,
                                                   tcflags=[12, 34, 56, [78, 90]],
                                                   term="xtermmmm")

    def check(rx_message: rnsh.protocol.Message):
        assert isinstance(rx_message, message.__class__)
        assert rx_message.cmdline == message.cmdline
        assert rx_message.pipe_stdin == message.pipe_stdin
        assert rx_message.pipe_stdout == message.pipe_stdout
        assert rx_message.pipe_stderr == message.pipe_stderr
        assert rx_message.tcflags == message.tcflags
        assert rx_message.term == message.term

    eat_own_dog_food(message, check)


def test_send_receive_windowsize():
    message = rnsh.protocol.WindowSizeMessage(1, 2, 3, 4)

    def check(rx_message: rnsh.protocol.Message):
        assert isinstance(rx_message, message.__class__)
        assert rx_message.rows == message.rows
        assert rx_message.cols == message.cols
        assert rx_message.hpix == message.hpix
        assert rx_message.vpix == message.vpix

    eat_own_dog_food(message, check)


def test_send_receive_versioninfo():
    message = rnsh.protocol.VersionInfoMessage(sw_version="1.2.3")
    message.protocol_version = 30

    def check(rx_message: rnsh.protocol.Message):
        assert isinstance(rx_message, message.__class__)
        assert rx_message.sw_version == message.sw_version
        assert rx_message.protocol_version == message.protocol_version

    eat_own_dog_food(message, check)


def test_send_receive_error():
    message = rnsh.protocol.ErrorMessage(msg="TESTerr",
                                         fatal=True,
                                         data={"one": 2})

    def check(rx_message: rnsh.protocol.Message):
        assert isinstance(rx_message, message.__class__)
        assert rx_message.msg == message.msg
        assert rx_message.fatal == message.fatal
        assert rx_message.data == message.data

    eat_own_dog_food(message, check)


def test_send_receive_cmdexit():
    message = rnsh.protocol.CommandExitedMessage(5)

    def check(rx_message: rnsh.protocol.Message):
        assert isinstance(rx_message, message.__class__)
        assert rx_message.return_code == message.return_code

    eat_own_dog_food(message, check)





