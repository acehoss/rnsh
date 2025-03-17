import socket
import threading
from typing import Optional

import RNS


class SOCKS5Request:
    def __init__(self, client_socket: socket.socket, addr: str, port: int, handler_id: int):
        self.client_socket = client_socket
        self.addr = addr
        self.port = port
        self.handler_id = handler_id
        self.response: Optional[bytes] = None
        self.event = threading.Event()


class RequestMessage(RNS.MessageBase):
    MSGTYPE = 0x0091
    def pack(self):
        return self.data
    def unpack(self, raw):
        self.data = raw
