import logging
from logging import Handler, getLevelName
from types import GenericAlias
import os

import RNS

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


log_format = '%(name)-40s %(message)s [%(threadName)s]'

logging.basicConfig(
    level=logging.INFO,
    #format='%(asctime)s.%(msecs)03d %(levelname)-6s %(threadName)-15s %(name)-15s %(message)s',
    format=log_format,
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[RnsHandler()])