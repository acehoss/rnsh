import logging as __logging
import os

log_format = '%(levelname)-6s %(name)-40s %(message)s [%(threadName)s]' \
    if os.environ.get('UNDER_SYSTEMD') == "1" \
    else '\r%(asctime)s.%(msecs)03d %(levelname)-6s %(name)-40s %(message)s [%(threadName)s]'

__logging.basicConfig(
    level=__logging.INFO,
    #format='%(asctime)s.%(msecs)03d %(levelname)-6s %(threadName)-15s %(name)-15s %(message)s',
    format=log_format,
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[__logging.StreamHandler()])