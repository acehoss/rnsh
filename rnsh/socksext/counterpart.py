import os
import sys
import threading
import socket
import time
import RNS

from rnsh.socksext.socksproxy import SOCKS_APP_NAME
from rnsh.socksext.protocol import RequestMessage

COUNTERPART_IDENTITY_FILE = "socks5_identity"


class SOCKS5CounterPart:
    def __init__(self, announce_interval: int = 60):
        self.reticulum = RNS.Reticulum(configdir=None, loglevel=RNS.LOG_INFO)
        self.identity = self.load_or_create_identity()
        self.destination = RNS.Destination(
            self.identity, RNS.Destination.IN, RNS.Destination.SINGLE, SOCKS_APP_NAME
        )
        self.channels = {}
        self.connections = {}
        self.lock = threading.Lock()
        self.next_link_id = 0
        self.running = False
        self.announce_interval = announce_interval

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
            data = message.data
            parts = data.split(b":", 2)
            if len(parts) < 2:
                print(f"Invalid message format on link {link_id}")
                return
            command = parts[0].decode('utf-8')
            handler_id = int(parts[1].decode('utf-8'))
            payload = parts[2] if len(parts) > 2 else b""

            if command == "CONNECT":
                addr, port = payload.decode('utf-8').split(":", 1)
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
        max_retries = 5
        retry_delay = 1  # seconds
        try:
            while True:
                try:
                    data = sock.recv(4096)
                    if not data:
                        print(f"Destination closed for handler {handler_id}")
                        break
                except socket.error as e:
                    print(f"Socket recv error for handler {handler_id}: {e}")
                    break  # Exit on recv error (destination likely closed)

                with self.lock:
                    if link_id not in self.channels:
                        print(f"Channel for link {link_id} gone for handler {handler_id}")
                        break
                    channel = self.channels[link_id]
                    response = RequestMessage()
                    response.data = f"DATA:{handler_id}:".encode() + data

                retries = 0
                while retries < max_retries:
                    try:
                        channel.send(response)
                        print(f"Sent {len(data)} bytes back for {handler_id} on link {link_id}")
                        break
                    except Exception as e:
                        retries += 1
                        print(f"Channel send failed for handler {handler_id} (retry {retries}/{max_retries}): {e}")
                        if retries == max_retries:
                            print(f"Max retries reached for handler {handler_id}, giving up")
                            return  # Exit thread if retries exhausted
                        time.sleep(retry_delay)

        except Exception as e:
            print(f"Unexpected error in relay for handler {handler_id}: {e}")
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

    def announce_loop(self):
        while self.running:
            self.destination.announce()
            print(f"Announced destination {self.destination.hash.hex()}")
            time.sleep(self.announce_interval)

    def run(self):
        print(f"Destination hash: {self.destination.hash.hex()}")
        self.destination.set_link_established_callback(self.link_established)
        self.destination.accepts_links(True)
        self.running = True

        threading.Thread(target=self.announce_loop, daemon=True).start()

        self.destination.announce()
        print("Counterpart running. Press Ctrl+C to exit.")
        sys.stdout.flush()

        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print("Shutting down...")
            self.running = False
            self.reticulum.exit_handler()
            sys.stdout.flush()
            sys.exit(0)