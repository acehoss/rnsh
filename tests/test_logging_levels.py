import logging
import pytest
import sys
import types

# Provide a lightweight RNS stub if the real module isn't available
try:
    import RNS  # type: ignore
except Exception:
    RNS = types.ModuleType("RNS")
    # Define level ordering consistent with rnsh expectations
    RNS.LOG_CRITICAL = 0
    RNS.LOG_ERROR = 1
    RNS.LOG_WARNING = 2
    RNS.LOG_NOTICE = 3
    RNS.LOG_INFO = 4
    RNS.LOG_VERBOSE = 5
    RNS.LOG_DEBUG = 6
    # minimal surface used by rnsh.rnslogging
    RNS.loglevel = RNS.LOG_INFO
    def _noop_log(msg, level=3, _override_destination=False):
        pass
    RNS.log = _noop_log
    RNS.compact_log_fmt = True
    RNS.loglevelname = lambda level: {0:"CRIT",1:"ERROR",2:"WARN",3:"NOTE",4:"INFO",5:"VERB",6:"DEBUG"}.get(level, str(level))
    sys.modules['RNS'] = RNS

import rnsh.rnslogging as rnslogging


def test_rnslogging_python_level_mapping():
    mapping_expectations = {
        RNS.LOG_CRITICAL: logging.CRITICAL,
        RNS.LOG_ERROR: logging.ERROR,
        RNS.LOG_WARNING: logging.WARNING,
        RNS.LOG_NOTICE: logging.INFO,
        RNS.LOG_INFO: logging.INFO,
        RNS.LOG_VERBOSE: logging.DEBUG,
        RNS.LOG_DEBUG: logging.DEBUG,
    }

    root_logger = logging.getLogger()
    for rns_level, expected_logging_level in mapping_expectations.items():
        rnslogging.RnsHandler.set_log_level_with_rns_level(rns_level)
        assert root_logger.getEffectiveLevel() == expected_logging_level


def test_compute_target_rns_loglevel_clamps_and_shifts():
    base = RNS.LOG_INFO
    # No flags -> base
    assert rnslogging.compute_target_rns_loglevel(0, 0, base) == base
    # More verbose
    assert rnslogging.compute_target_rns_loglevel(2, 0, base) == min(RNS.LOG_DEBUG, base + 2)
    # More quiet
    assert rnslogging.compute_target_rns_loglevel(0, 2, base) == max(RNS.LOG_CRITICAL, base - 2)
    # Clamp upper bound
    assert rnslogging.compute_target_rns_loglevel(50, 0, base) == RNS.LOG_DEBUG
    # Clamp lower bound
    assert rnslogging.compute_target_rns_loglevel(0, 50, base) == RNS.LOG_CRITICAL


    # Only validate mapping behavior here; listener-level shifting tested elsewhere


