# MIT License
#
# Copyright (c) 2023 Aaron Heise
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

import asyncio
import logging
import sys
import termios
import rnsh.process as process
from logging import Handler, getLevelName
from types import GenericAlias
from typing import Any

import RNS

import rnsh.exception as exception


class RnsHandler(Handler):
    """
    A handler class which writes logging records, appropriately formatted,
    to the RNS logger.
    """

    def __init__(self):
        """
        Initialize the handler.
        """
        Handler.__init__(self)

    @staticmethod
    def get_rns_loglevel(loglevel: int) -> int:
        if loglevel == logging.CRITICAL:
            return RNS.LOG_CRITICAL
        if loglevel == logging.ERROR:
            return RNS.LOG_ERROR
        if loglevel == logging.WARNING:
            return RNS.LOG_WARNING
        if loglevel == logging.INFO:
            return RNS.LOG_INFO
        if loglevel == logging.DEBUG:
            return RNS.LOG_DEBUG
        return RNS.LOG_DEBUG

    def emit(self, record):
        """
        Emit a record.
        """
        try:
            msg = self.format(record)

            RNS.log(msg, RnsHandler.get_rns_loglevel(record.levelno))
        except RecursionError:  # See issue 36272
            raise
        except Exception:
            self.handleError(record)

    def __repr__(self):
        level = getLevelName(self.level)
        return '<%s (%s)>' % (self.__class__.__name__, level)

    __class_getitem__ = classmethod(GenericAlias)


log_format = '%(name)-30s %(message)s [%(threadName)s]'

logging.basicConfig(
    level=logging.DEBUG,  # RNS.log will filter it, but some formatting will still be processed before it gets there
    # format='%(asctime)s.%(msecs)03d %(levelname)-6s %(threadName)-15s %(name)-15s %(message)s',
    format=log_format,
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[RnsHandler()])

_loop: asyncio.AbstractEventLoop = None


def set_main_loop(loop: asyncio.AbstractEventLoop):
    global _loop
    _loop = loop


# hack for temporarily overriding term settings to make debug print right
_rns_log_orig = RNS.log


def _rns_log(msg, level=3, _override_destination=False):
    if not RNS.compact_log_fmt:
        msg = (" " * (7 - len(RNS.loglevelname(level)))) + msg

    def _rns_log_inner():
        nonlocal msg, level, _override_destination
        try:
            with process.TTYRestorer(sys.stdin.fileno()) as tr:
                    attr = tr.current_attr()
                    attr[process.TTYRestorer.ATTR_IDX_OFLAG] = attr[process.TTYRestorer.ATTR_IDX_OFLAG] | \
                                                               termios.ONLRET | termios.ONLCR | termios.OPOST
                    tr.set_attr(attr)
                    _rns_log_orig(msg, level, _override_destination)
        except ValueError:
            _rns_log_orig(msg, level, _override_destination)

    try:
        if _loop:
            _loop.call_soon_threadsafe(_rns_log_inner)
        else:
            _rns_log_inner()
    except RuntimeError:
        _rns_log_inner()


RNS.log = _rns_log
