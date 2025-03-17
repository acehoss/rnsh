import os
import sys
import threading
import socket
import RNS

from rnsh.socksext.socksproxy import SOCKS_APP_NAME
from rnsh.socksext.protocol import RequestMessage

COUNTERPART_IDENTITY_FILE = "socks5_identity"


class SOCKS5CounterPart:
    def __init__(self):
        self.reticulum = RNS.Reticulum(configdir=None, loglevel=RNS.LOG_INFO)
        self.identity = self.load_or_create_identity()
        self.destination = RNS.Destination(
            self.identity, RNS.Destination.IN, RNS.Destination.SINGLE, SOCKS_APP_NAME
        )
        self.channels = {}
        self.connections = {}
        self.lock = threading.Lock()
        self.next_link_id = 0

    def load_or_create_identity(self):
        if os.path.exists(COUNTERPART_IDENTITY_FILE):
            identity = RNS.Identity.from_file(COUNTERPART_IDENTITY_FILE)
            print("Loaded existing identity")
        else:
            identity = RNS.Identity()
            identity.to_file(COUNTERPART_IDENTITY_FILE)
            print("Created and saved new identity")
        return identity

    def handle_message(self, message: RequestMessage, link_id: int):
        try:
            # Parse bytes directly
            data = message.data
            parts = data.split(b":", 2)
            if len(parts) < 2:
                print(f"Invalid message format on link {link_id}")
                return
            command = parts[0].decode('utf-8')  # Command is ASCII
            handler_id = int(parts[1].decode('utf-8'))  # Handler ID is ASCII
            payload = parts[2] if len(parts) > 2 else b""

            if command == "CONNECT":
                addr, port = payload.decode('utf-8').split(":", 1)  # CONNECT payload is text
                port = int(port)
                print(f"Received CONNECT {handler_id} for {addr}:{port}")
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(10)
                sock.connect((addr, port))
                with self.lock:
                    self.connections[handler_id] = sock
                threading.Thread(target=self.relay_from_destination, args=(handler_id, sock, link_id), daemon=True).start()

            elif command == "DATA":
                print(f"Received {len(payload)} bytes for {handler_id}")
                with self.lock:
                    if handler_id in self.connections:
                        sock = self.connections[handler_id]
                        sock.sendall(payload)
                    else:
                        print(f"No connection for {handler_id}")
        except Exception as e:
            print(f"Error handling message: {e}")

    def relay_from_destination(self, handler_id: int, sock: socket.socket, link_id: int):
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                with self.lock:
                    if link_id in self.channels:
                        channel = self.channels[link_id]
                        response = RequestMessage()
                        response.data = f"DATA:{handler_id}:".encode() + data
                        channel.send(response)
                        print(f"Sent {len(data)} bytes back for {handler_id} on link {link_id}")
        except Exception as e:
            print(f"Error relaying from destination for {handler_id}: {e}")
        finally:
            with self.lock:
                if handler_id in self.connections:
                    del self.connections[handler_id]
            sock.close()

    def link_established(self, link):
        link_id = self.next_link_id
        self.next_link_id += 1
        print(f"Link {link_id} established from {link.get_remote_identity()}")
        channel = link.get_channel()
        channel.register_message_type(RequestMessage)
        channel.add_message_handler(lambda msg: self.handle_message(msg, link_id))
        with self.lock:
            self.channels[link_id] = channel

    def run(self):
        print(f"Destination hash: {self.destination.hash.hex()}")
        self.destination.set_link_established_callback(self.link_established)
        self.destination.accepts_links(True)
        self.destination.announce()
        print("Counterpart running. Press Ctrl+C to exit.")
        sys.stdout.flush()

        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print("Shutting down...")
            self.reticulum.exit_handler()
            sys.stdout.flush()
            sys.exit(0)