from __future__ import annotations

import logging
logging.getLogger().setLevel(logging.DEBUG)

import rnsh.protocol
import contextlib
import typing
import types
import time
import uuid


module_logger = logging.getLogger(__name__)


class Link:
    def __init__(self, mdu: int, rtt: float):
        self.link_id = uuid.uuid4()
        self.timeout_callbacks = 0
        self.mdu = mdu
        self.rtt = rtt
        self.usable = True
        self.receipts = []

    def timeout_callback(self):
        self.timeout_callbacks += 1

    def __str__(self):
        return str(self.link_id)


class Receipt:
    def __init__(self, link: Link, state: rnsh.protocol.MessageState, raw: bytes):
        self.state = state
        self.raw = raw
        self.link = link


class ProtocolHarness(contextlib.AbstractContextManager):
    def __init__(self, retry_delay_min: float = 1):
        self._log = module_logger.getChild(self.__class__.__name__)
        self.messenger = rnsh.protocol.Messenger(receipt_checker=self.receipt_checker,
                                                 link_timeout_callback=self.link_timeout_callback,
                                                 link_mdu_getter=self.link_mdu_getter,
                                                 link_rtt_getter=self.link_rtt_getter,
                                                 link_usable_getter=self.link_usable_getter,
                                                 packet_sender=self.packet_sender,
                                                 retry_delay_min=retry_delay_min)

    def packet_sender(self, link: Link, raw: bytes) -> Receipt:
        receipt = Receipt(link, rnsh.protocol.MessageState.MSGSTATE_SENT, raw)
        link.receipts.append(receipt)
        return receipt

    @staticmethod
    def link_mdu_getter(link: Link):
        return link.mdu

    @staticmethod
    def link_rtt_getter(link: Link):
        return link.rtt

    @staticmethod
    def link_usable_getter(link: Link):
        return link.usable

    @staticmethod
    def receipt_checker(receipt: Receipt) -> rnsh.protocol.MessageState:
        return receipt.state

    @staticmethod
    def link_timeout_callback(link: Link):
        link.timeout_callback()

    def cleanup(self):
        self.messenger.shutdown()

    def __exit__(self, __exc_type: typing.Type[BaseException], __exc_value: BaseException,
                 __traceback: types.TracebackType) -> bool:
        # self._log.debug(f"__exit__({__exc_type}, {__exc_value}, {__traceback})")
        self.cleanup()
        return False


def test_mdu():
    with ProtocolHarness() as h:
        mdu = 500
        link = Link(mdu=mdu, rtt=0.25)
        assert h.messenger.get_mdu(link) == mdu - 4
        link.mdu = mdu = 600
        assert h.messenger.get_mdu(link) == mdu - 4


def test_rtt():
    with ProtocolHarness() as h:
        rtt = 0.25
        link = Link(mdu=500, rtt=rtt)
        assert h.messenger.get_rtt(link) == rtt


def test_send_one_retry():
    rtt = 0.001
    retry_interval = rtt * 150
    message_content = b'Test'
    with ProtocolHarness(retry_delay_min=retry_interval) as h:
        link = Link(mdu=500, rtt=rtt)
        message = rnsh.protocol.StreamDataMessage(stream_id=rnsh.protocol.StreamDataMessage.STREAM_ID_STDIN,
                                                  data=message_content, eof=True)
        assert len(link.receipts) == 0
        h.messenger.send_message(link, message)
        assert message.tracked
        assert message.raw is not None
        assert len(link.receipts) == 1
        receipt = link.receipts[0]
        assert receipt.state == rnsh.protocol.MessageState.MSGSTATE_SENT
        assert receipt.raw == message.raw
        time.sleep(retry_interval * 1.5)
        assert len(link.receipts) == 1
        receipt.state = rnsh.protocol.MessageState.MSGSTATE_FAILED
        module_logger.info("set failed")
        time.sleep(retry_interval)
        assert len(link.receipts) == 2
        receipt = link.receipts[1]
        assert receipt.state == rnsh.protocol.MessageState.MSGSTATE_SENT
        receipt.state = rnsh.protocol.MessageState.MSGSTATE_DELIVERED
        time.sleep(retry_interval)
        assert len(link.receipts) == 2
        assert not message.tracked


def eat_own_dog_food(message: rnsh.protocol.Message, checker: typing.Callable[[rnsh.protocol.Message], None]):
    rtt = 0.001
    retry_interval = rtt * 150
    with ProtocolHarness(retry_delay_min=retry_interval) as h:
        link = Link(mdu=500, rtt=rtt)
        assert len(link.receipts) == 0
        h.messenger.send_message(link, message)
        assert message.tracked
        assert message.raw is not None
        assert len(link.receipts) == 1
        receipt = link.receipts[0]
        assert receipt.state == rnsh.protocol.MessageState.MSGSTATE_SENT
        assert receipt.raw == message.raw
        module_logger.info("set delivered")
        receipt.state = rnsh.protocol.MessageState.MSGSTATE_DELIVERED
        time.sleep(retry_interval * 2)
        assert len(link.receipts) == 1
        assert receipt.state == rnsh.protocol.MessageState.MSGSTATE_DELIVERED
        assert not message.tracked
        module_logger.info("injecting rx message")
        h.messenger.inbound(message.raw)
        rx_message = h.messenger.poll_inbound(block=False)
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





