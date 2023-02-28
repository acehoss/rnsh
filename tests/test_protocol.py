from __future__ import annotations

import logging

from RNS.Channel import TPacket, MessageState, ChannelOutletBase, Channel
from typing import Callable

logging.getLogger().setLevel(logging.DEBUG)

import rnsh.protocol
import contextlib
import typing
import types
import time
import uuid
from RNS.Channel import MessageBase


module_logger = logging.getLogger(__name__)


def test_send_receive_streamdata():
    message = rnsh.protocol.StreamDataMessage(stream_id=rnsh.protocol.StreamDataMessage.STREAM_ID_STDIN,
                                              data=b'Test', eof=True)
    rx_message = message.__class__()
    rx_message.unpack(message.pack())

    assert isinstance(rx_message, message.__class__)
    assert rx_message.stream_id == message.stream_id
    assert rx_message.data == message.data
    assert rx_message.eof == message.eof


def test_send_receive_noop():
    message = rnsh.protocol.NoopMessage()

    rx_message = message.__class__()
    rx_message.unpack(message.pack())

    assert isinstance(rx_message, message.__class__)


def test_send_receive_execute():
    message = rnsh.protocol.ExecuteCommandMesssage(cmdline=["test", "one", "two"],
                                                   pipe_stdin=False,
                                                   pipe_stdout=True,
                                                   pipe_stderr=False,
                                                   tcflags=[12, 34, 56, [78, 90]],
                                                   term="xtermmmm")
    rx_message = message.__class__()
    rx_message.unpack(message.pack())

    assert isinstance(rx_message, message.__class__)
    assert rx_message.cmdline == message.cmdline
    assert rx_message.pipe_stdin == message.pipe_stdin
    assert rx_message.pipe_stdout == message.pipe_stdout
    assert rx_message.pipe_stderr == message.pipe_stderr
    assert rx_message.tcflags == message.tcflags
    assert rx_message.term == message.term


def test_send_receive_windowsize():
    message = rnsh.protocol.WindowSizeMessage(1, 2, 3, 4)
    rx_message = message.__class__()
    rx_message.unpack(message.pack())

    assert isinstance(rx_message, message.__class__)
    assert rx_message.rows == message.rows
    assert rx_message.cols == message.cols
    assert rx_message.hpix == message.hpix
    assert rx_message.vpix == message.vpix


def test_send_receive_versioninfo():
    message = rnsh.protocol.VersionInfoMessage(sw_version="1.2.3")
    message.protocol_version = 30
    rx_message = message.__class__()
    rx_message.unpack(message.pack())

    assert isinstance(rx_message, message.__class__)
    assert rx_message.sw_version == message.sw_version
    assert rx_message.protocol_version == message.protocol_version


def test_send_receive_error():
    message = rnsh.protocol.ErrorMessage(msg="TESTerr",
                                         fatal=True,
                                         data={"one": 2})
    rx_message = message.__class__()
    rx_message.unpack(message.pack())

    assert isinstance(rx_message, message.__class__)
    assert rx_message.msg == message.msg
    assert rx_message.fatal == message.fatal
    assert rx_message.data == message.data


def test_send_receive_cmdexit():
    message = rnsh.protocol.CommandExitedMessage(5)
    rx_message = message.__class__()
    rx_message.unpack(message.pack())

    assert isinstance(rx_message, message.__class__)
    assert rx_message.return_code == message.return_code





