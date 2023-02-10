import logging
from logging import Handler, getLevelName
from types import GenericAlias
import os
import tty
import termios
import sys
import RNS
import json

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

            # tattr = termios.tcgetattr(sys.stdin.fileno())
            # json.dump(tattr, sys.stdout)
            # termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, tattr | termios.ONLRET | termios.ONLCR | termios.OPOST)
            RNS.log(msg, RnsHandler.get_rns_loglevel(record.levelno))
            # termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, tattr)
        except RecursionError:  # See issue 36272
            raise
        except Exception:
            self.handleError(record)

    def __repr__(self):
        level = getLevelName(self.level)
        return '<%s (%s)>' % (self.__class__.__name__, level)

    __class_getitem__ = classmethod(GenericAlias)

log_format = '%(name)-40s %(message)s [%(threadName)s]'

logging.basicConfig(
    level=logging.INFO,
    #format='%(asctime)s.%(msecs)03d %(levelname)-6s %(threadName)-15s %(name)-15s %(message)s',
    format=log_format,
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[RnsHandler()])

#hack for temporarily overriding term settings to make debug print right
_rns_log_orig = RNS.log

def _rns_log(msg, level=3, _override_destination = False):
    tattr = termios.tcgetattr(sys.stdin.fileno())
    tattr_orig = tattr.copy()
    # tcflag_t c_iflag;      /* input modes */
    # tcflag_t c_oflag;      /* output modes */
    # tcflag_t c_cflag;      /* control modes */
    # tcflag_t c_lflag;      /* local modes */
    # cc_t     c_cc[NCCS];   /* special characters */
    tattr[1] = tattr[1] | termios.ONLRET | termios.ONLCR | termios.OPOST
    termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, tattr)
    _rns_log_orig(msg, level, _override_destination)
    termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, tattr_orig)

RNS.log = _rns_log