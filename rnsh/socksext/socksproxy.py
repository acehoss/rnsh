import os
import queue
import socket
import struct
import threading
import time
from collections import deque
from typing import Dict, Tuple, Optional

import RNS

from rnsh.socksext.protocol import SOCKS5Request, RequestMessage

SOCKS_APP_NAME = "socks5proxy"

class SOCKS5Proxy:
    request_handlers: Dict[int, SOCKS5Request] = {}

    def __init__(self, host='127.0.0.1', port=1080, destination_hash: str = None):
        self.host = host
        self.port = port
        self.server_socket = None
        self.request_queue = queue.Queue()
        self.running = False
        self.handler_id = 0
        self.lock = threading.Lock()
        self.destination_hash = bytes.fromhex(destination_hash) if destination_hash else None
        self.link_pool = LinkPool(self.destination_hash) if destination_hash else None

    def start(self):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(10)
        self.running = True

        if self.link_pool:
            self.link_pool.start()

        threading.Thread(target=self.process_responses, daemon=True).start()
        print(f"SOCKS5 proxy started on {self.host}:{self.port}")

        try:
            while self.running:
                client_socket, addr = self.server_socket.accept()
                client_thread = threading.Thread(
                    target=self.handle_client,
                    args=(client_socket, addr)
                )
                client_thread.daemon = True
                client_thread.start()
        except KeyboardInterrupt:
            print("\nShutting down proxy...")
        finally:
            self.running = False
            if self.link_pool:
                self.link_pool.stop()
            self.server_socket.close()

    def handle_client(self, client_socket: socket.socket, client_addr: tuple):
        try:
            version, nmethods = struct.unpack("!BB", client_socket.recv(2))
            if version != 5:
                return
            client_socket.recv(nmethods)
            client_socket.sendall(struct.pack("!BB", 5, 0))

            version, cmd, rsv, atype = struct.unpack("!BBBB", client_socket.recv(4))
            if version != 5 or cmd != 1:
                return

            if atype == 1:
                addr = socket.inet_ntoa(client_socket.recv(4))
            elif atype == 3:
                addr_len = struct.unpack("!B", client_socket.recv(1))[0]
                addr = client_socket.recv(addr_len).decode()
            else:
                return

            port = struct.unpack("!H", client_socket.recv(2))[0]
            print(f"Request from {client_addr} to {addr}:{port}")

            with self.lock:
                handler_id = self.handler_id
                self.handler_id += 1
                request = SOCKS5Request(client_socket, addr, port, handler_id)
                self.request_handlers[handler_id] = request
                self.request_queue.put((handler_id, request))

            if request.event.wait(timeout=30):
                client_socket.sendall(struct.pack("!BBBB4sH", 5, 0, 0, 1,
                                                  socket.inet_aton("0.0.0.0"), 0))
                self.relay_data(request)
            else:
                print(f"Timeout waiting for connection to {addr}:{port}")
                client_socket.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
        except Exception as e:
            print(f"Error handling client {client_addr}: {e}")
        finally:
            with self.lock:
                for hid, req in list(self.request_handlers.items()):
                    if req.client_socket == client_socket:
                        del self.request_handlers[hid]
            client_socket.close()

    def relay_data(self, request: SOCKS5Request):
        try:
            client_socket = request.client_socket
            handler_id = request.handler_id
            while self.running:
                data = client_socket.recv(4096)
                if not data:
                    break
                self.link_pool.send_data(handler_id, data)
        except Exception as e:
            print(f"Error relaying data for handler {request.handler_id}: {e}")

    def process_responses(self):
        while self.running:
            try:
                handler_id, request = self.request_queue.get(timeout=1.0)

                if self.link_pool:
                    if not self.link_pool.connect(handler_id, request.addr, request.port):
                        request.response = b"Failed to connect"
                        request.event.set()
                    else:
                        request.event.set()
                else:
                    time.sleep(0.1)
                    response = f"Hello from {request.addr}:{request.port}".encode('utf-8')
                    request.response = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/plain\r\n"
                        b"Content-Length: " + str(len(response)).encode('utf-8') + b"\r\n"
                        b"\r\n" + response
                    )
                    request.event.set()

                self.request_queue.task_done()

            except queue.Empty:
                continue
            except Exception as e:
                print(f"Error processing response: {e}")

class LinkPool:
    def __init__(self, destination_hash: bytes, pool_size: int = 1, configdir: str = None):
        self.destination_hash = destination_hash
        self.pool_size = pool_size
        self.configdir = configdir
        self.links: Dict[int, RNS.Link] = {}
        self.channels: Dict[int, RNS.Channel.Channel] = {}
        self.active_link_ids = deque(maxlen=pool_size)
        self.lock = threading.Lock()
        self.reticulum = RNS.Reticulum(configdir=self.configdir, loglevel=RNS.LOG_INFO)
        self.running = False
        self.next_link_id = 0
        self.responses = {}
        print("Initializing Reticulum network...")
        self.identity = self.load_or_create_identity()
        self.target_identity = self.wait_for_identity(self.destination_hash)
        self.target_destination = RNS.Destination(
            self.target_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            SOCKS_APP_NAME,
        )

    def load_or_create_identity(self):
        identity_file = "proxy_identity"
        if os.path.exists(identity_file):
            identity = RNS.Identity.from_file(identity_file)
            print("Loaded proxy identity")
        else:
            identity = RNS.Identity()
            identity.to_file(identity_file)
            print("Created and saved proxy identity")
        return identity

    def wait_for_identity(self, destination_hash: bytes, timeout: int = 60*30):
        """Wait for the identity to be recalled, with path request"""
        target_identity = RNS.Identity.recall(destination_hash)
        if not target_identity:
            print(f"Waiting for identity of {destination_hash.hex()}...")
            RNS.Transport.request_path(destination_hash)  # Force path discovery
            start_time = time.time()
            while not target_identity and time.time() - start_time < timeout:
                target_identity = RNS.Identity.recall(destination_hash)
                if not target_identity:
                    time.sleep(1)
                    print(f"Still waiting... Elapsed: {int(time.time() - start_time)}s")
            if not target_identity:
                raise RuntimeError(f"Could not recall identity for {destination_hash.hex()} after {timeout}s")
            print(f"Identity recalled for {destination_hash.hex()}")
        return target_identity

    def start(self):
        self.running = True
        threading.Thread(target=self.maintain_pool, daemon=True).start()

    def maintain_pool(self):
        while self.running:
            with self.lock:
                print(f"Pool state: active={len(self.active_link_ids)}/{self.pool_size}, "
                      f"links={len(self.links)}, channels={len(self.channels)}")

                for link_id in list(self.links.keys()):
                    if self.links[link_id].status == RNS.Link.CLOSED:
                        self._cleanup_link(link_id)

                if len(self.active_link_ids) < self.pool_size:
                    if not RNS.Transport.has_path(self.destination_hash):
                        RNS.Transport.request_path(self.destination_hash)
                        print(f"Requested path to {self.destination_hash.hex()}")
                        time.sleep(1)
                        continue

                    link = RNS.Link(self.target_destination)
                    link_id = self.next_link_id
                    self.next_link_id += 1
                    self.links[link_id] = link
                    link.set_link_established_callback(lambda l: self.link_established(link_id))
                    print(f"Initiated link {link_id}, status: {link.status}")

                    timeout = time.time() + 10
                    while link.status != RNS.Link.ACTIVE and time.time() < timeout:
                        time.sleep(0.1)
                    if link.status != RNS.Link.ACTIVE:
                        print(f"Link {link_id} failed to activate, status: {link.status}")
                        del self.links[link_id]
                        continue
                    print(f"Link {link_id} confirmed ACTIVE")
            time.sleep(5)

    def _cleanup_link(self, link_id: int):
        if link_id in self.links:
            self.links[link_id].teardown()
            del self.links[link_id]
        if link_id in self.channels:
            del self.channels[link_id]
        if link_id in self.active_link_ids:
            self.active_link_ids.remove(link_id)
        if link_id in self.responses:
            del self.responses[link_id]
        print(f"Cleaned up link {link_id}")

    def link_established(self, link_id):
        with self.lock:
            if link_id in self.links:
                link = self.links[link_id]
                link.identify(self.identity)
                channel = link.get_channel()
                channel.register_message_type(RequestMessage)
                channel.add_message_handler(lambda msg: self.handle_channel_message(msg, link_id))
                self.channels[link_id] = channel
                if link_id not in self.active_link_ids:
                    self.active_link_ids.append(link_id)
                print(f"Link {link_id} established with channel, active pool size: {len(self.active_link_ids)}")

    def get_available_link(self) -> Tuple[Optional[RNS.Link], Optional[int]]:
        with self.lock:
            if not self.active_link_ids:
                print("No active links available")
                return None, None

            for _ in range(len(self.active_link_ids)):
                link_id = self.active_link_ids.popleft()
                if (link_id in self.links and
                        link_id in self.channels and
                        self.links[link_id].status == RNS.Link.ACTIVE):
                    self.active_link_ids.append(link_id)
                    print(f"Using active link {link_id}")
                    return self.links[link_id], link_id
                else:
                    print(f"Link {link_id} invalid, cleaning up")
                    self._cleanup_link(link_id)
            print("No active links with channels available")
            return None, None

    def connect(self, handler_id: int, addr: str, port: int):
        link, link_id = self.get_available_link()
        if link and link_id is not None:
            channel = self.channels[link_id]
            if channel and channel.is_ready_to_send():
                request_data = f"CONNECT:{handler_id}:{addr}:{port}".encode()
                message = RequestMessage()
                message.data = request_data
                channel.send(message)
                print(f"Sent CONNECT request {handler_id} over channel on link {link_id}")
                with self.lock:
                    self.responses[handler_id] = queue.Queue()
                return True
        print(f"Failed to connect request {handler_id}: No link available")
        return False

    def send_data(self, handler_id: int, data: bytes):
        link, link_id = self.get_available_link()
        if link and link_id is not None:
            channel = self.channels[link_id]
            if channel and channel.is_ready_to_send():
                message = RequestMessage()
                message.data = f"DATA:{handler_id}:".encode() + data
                channel.send(message)
                print(f"Sent {len(data)} bytes for {handler_id} over channel on link {link_id}")
            else:
                print(f"Channel not ready for link {link_id}")
        else:
            print(f"No link available to send data for {handler_id}")

    def handle_channel_message(self, message, link_id):
        try:
            data = message.data
            parts = data.split(b":", 2)
            if len(parts) < 2:
                print(f"Invalid message format on link {link_id}")
                return
            command = parts[0].decode('utf-8')
            handler_id = int(parts[1].decode('utf-8'))
            payload = parts[2] if len(parts) > 2 else b""

            with self.lock:
                if handler_id not in SOCKS5Proxy.request_handlers:
                    print(f"Handler {handler_id} not found")
                    return
                request = SOCKS5Proxy.request_handlers[handler_id]

            if command == "DATA":
                with self.lock:
                    if handler_id in self.responses:
                        self.responses[handler_id].put(payload)
                        print(f"Link {link_id} queued {len(payload)} bytes for {handler_id}")
                request.client_socket.sendall(payload)
            else:
                print(f"Unknown command {command} for {handler_id} on link {link_id}")
        except Exception as e:
            print(f"Error handling channel message on link {link_id}: {e}")

    def stop(self):
        self.running = False
        with self.lock:
            for link in self.links.values():
                link.teardown()
            self.links.clear()
            self.channels.clear()
            self.active_link_ids.clear()
            self.responses.clear()