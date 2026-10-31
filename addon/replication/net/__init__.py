# ##### BEGIN GPL LICENSE BLOCK #####
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# ##### END GPL LICENSE BLOCK #####

"""
Stdlib-only (socket/selectors/queue) replacement for the zmq ROUTER/DEALER
transport this library used to depend on.

Wire format for one message (used identically by Dealer and Router
connections):
    4 bytes: chunk count (big-endian unsigned)
    for each chunk:
        4 bytes: chunk length (big-endian unsigned)
        N bytes: chunk payload

Connect handshake (sent once, immediately after a TCP connect, read once by
Listener right after accept):
    4 bytes: identity length (big-endian unsigned)
    N bytes: identity
    1 byte: role (ROLE_COMMAND / ROLE_DATA / ROLE_TTL)

All raw-socket connect/bind calls are funneled through _tcp_connect/_tcp_bind
and all framed read/write lives in _Connection, so a future non-TCP transport
(p2p/QUIC) only has to replace those, not every call site in the library.
"""

import collections
import logging
import queue
import selectors
import socket
import struct
import threading
import time

logger = logging.getLogger(__name__)

_LEN = struct.Struct('!I')

MAX_CHUNK_SIZE = 512 * 1024 * 1024  # sanity cap, guards against a corrupt/adversarial length prefix
MAX_IDENTITY_SIZE = 256
HANDSHAKE_TIMEOUT = 5.0
DEFAULT_BACKLOG = 64
RECV_BUFFER_SIZE = 65536

ROLE_COMMAND = b'C'
ROLE_DATA = b'D'
ROLE_TTL = b'T'
_VALID_ROLES = {ROLE_COMMAND, ROLE_DATA, ROLE_TTL}


class NetworkError(Exception):
    """Base error for the net package"""


class HandshakeError(NetworkError):
    """Raised when a peer's connect handshake is invalid, oversized or times out"""


def encode_message(chunks) -> bytes:
    parts = [_LEN.pack(len(chunks))]
    for chunk in chunks:
        parts.append(_LEN.pack(len(chunk)))
        parts.append(chunk)
    return b''.join(parts)


class FrameReader:
    """Incremental parser turning a raw byte stream into complete messages.

    Feed it bytes as they arrive off a non-blocking socket; it returns
    whichever messages (each a list of byte chunks) became fully available.
    Never assumes one recv() == one message.
    """

    _WAIT_COUNT, _WAIT_CHUNK_LEN, _WAIT_CHUNK_DATA = range(3)

    def __init__(self):
        self._buffer = bytearray()
        self._state = self._WAIT_COUNT
        self._remaining_chunks = 0
        self._current_chunks = []
        self._current_len = 0

    def feed(self, data: bytes) -> list:
        if not data:
            return []
        self._buffer.extend(data)
        messages = []
        while True:
            if self._state == self._WAIT_COUNT:
                if len(self._buffer) < 4:
                    break
                self._remaining_chunks, = _LEN.unpack_from(self._buffer, 0)
                del self._buffer[:4]
                self._current_chunks = []
                if self._remaining_chunks == 0:
                    messages.append(self._current_chunks)
                    self._current_chunks = []
                    continue
                self._state = self._WAIT_CHUNK_LEN
            elif self._state == self._WAIT_CHUNK_LEN:
                if len(self._buffer) < 4:
                    break
                self._current_len, = _LEN.unpack_from(self._buffer, 0)
                if self._current_len > MAX_CHUNK_SIZE:
                    raise NetworkError(f"frame chunk too large ({self._current_len} bytes)")
                del self._buffer[:4]
                self._state = self._WAIT_CHUNK_DATA
            elif self._state == self._WAIT_CHUNK_DATA:
                if len(self._buffer) < self._current_len:
                    break
                chunk = bytes(self._buffer[:self._current_len])
                del self._buffer[:self._current_len]
                self._current_chunks.append(chunk)
                self._remaining_chunks -= 1
                if self._remaining_chunks == 0:
                    messages.append(self._current_chunks)
                    self._current_chunks = []
                    self._state = self._WAIT_COUNT
                else:
                    self._state = self._WAIT_CHUNK_LEN
        return messages


def _tcp_connect(address, port, timeout=None) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((address, port))
    except OSError:
        # Not doing this leaks the socket: the caller never gets a
        # reference to it if connect() raises, since the exception
        # propagates before this function returns anything.
        sock.close()
        raise
    return sock


def _tcp_bind(address, port, backlog=DEFAULT_BACKLOG) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((address, port))
    sock.listen(backlog)
    return sock


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Blocking exact-length read, used only during the connect handshake."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise HandshakeError("peer closed during handshake")
        buf.extend(chunk)
    return bytes(buf)


class _Connection:
    """One non-blocking TCP connection with framed send/recv buffering.

    Shared by Dealer (one persistent outbound connection) and Router (many
    accepted connections) so the partial-read/partial-write logic exists in
    exactly one place.
    """

    def __init__(self, sock: socket.socket, identity: bytes):
        self.identity = identity
        self._sock = sock
        self._sock.setblocking(False)
        try:
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._reader = FrameReader()
        self._out_buffer = bytearray()
        self._pending = []
        self._closed = False

    def fileno(self):
        return self._sock.fileno()

    @property
    def closed(self):
        return self._closed

    @property
    def wants_write(self):
        return bool(self._out_buffer)

    def queue_send(self, chunks):
        if self._closed:
            return
        self._out_buffer.extend(encode_message(chunks))

    def flush(self) -> bool:
        """Try to send queued bytes. Returns False if the connection died."""
        if self._closed:
            return False
        if not self._out_buffer:
            return True
        try:
            sent = self._sock.send(self._out_buffer)
        except (BlockingIOError, InterruptedError):
            return True
        except OSError:
            self._mark_closed()
            return False
        del self._out_buffer[:sent]
        return True

    def _read_ready(self):
        """Read available bytes and decode whichever messages completed.
        Returns None if the connection died."""
        try:
            data = self._sock.recv(RECV_BUFFER_SIZE)
        except (BlockingIOError, InterruptedError):
            return []
        except OSError:
            self._mark_closed()
            return None
        if data == b'':
            self._mark_closed()
            return None
        try:
            return self._reader.feed(data)
        except NetworkError as e:
            logger.error("Corrupted frame from %r, closing connection: %s", self.identity, e)
            self._mark_closed()
            return None

    def service(self, readable: bool, writable: bool) -> bool:
        """Called by Poller when this connection is select()-ready.
        Returns False if the connection died (caller should drop it)."""
        if writable:
            if not self.flush():
                return False
        if readable:
            messages = self._read_ready()
            if messages is None:
                return False
            self._pending.extend(messages)
        return not self._closed

    def pop_pending(self):
        msgs, self._pending = self._pending, []
        return msgs

    def _mark_closed(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def close(self):
        # linger=0 semantics: drop unsent buffered bytes immediately, don't
        # try to drain the outbound buffer first.
        self._mark_closed()


class Poller:
    """Thin wrapper around one selectors.DefaultSelector.

    Only ever touched by the thread that owns it (never share one Poller
    across threads).
    """

    def __init__(self):
        self._sel = selectors.DefaultSelector()
        self._masks = {}
        # Transports registered before they had a usable fileno() yet (e.g.
        # a Dealer still background-retrying its connect) - retried on each
        # poll() until they're ready to actually register with the selector.
        self._pending = []

    def register(self, transport):
        if not self._try_register(transport):
            self._pending.append(transport)

    def _try_register(self, transport):
        try:
            transport.fileno()
        except (OSError, ValueError, AttributeError):
            return False
        mask = selectors.EVENT_READ
        self._sel.register(transport, mask, transport)
        self._masks[transport] = mask
        return True

    def unregister(self, transport):
        try:
            self._sel.unregister(transport)
        except KeyError:
            pass
        self._masks.pop(transport, None)
        if transport in self._pending:
            self._pending.remove(transport)

    def poll(self, timeout_ms=0):
        """Service every registered transport and return the subset that had
        read activity this pass (mirrors the old `dict(zmq_poller.poll())` +
        `X in socks` idiom)."""
        timeout = None if timeout_ms is None else timeout_ms / 1000.0

        if self._pending:
            still_pending = []
            for transport in self._pending:
                if not self._try_register(transport):
                    still_pending.append(transport)
            self._pending = still_pending

        if not self._masks:
            # select() on Windows raises OSError (WinError 10022) when called
            # with zero registered descriptors - nothing to wait on either way.
            if timeout:
                time.sleep(timeout)
            return {}

        for transport, mask in list(self._masks.items()):
            wants_write = transport.wants_write
            desired = selectors.EVENT_READ | (selectors.EVENT_WRITE if wants_write else 0)
            if desired != mask:
                self._sel.modify(transport, desired, transport)
                self._masks[transport] = desired

        ready = {}
        for key, mask in self._sel.select(timeout):
            transport = key.data
            alive = transport.service(
                readable=bool(mask & selectors.EVENT_READ),
                writable=bool(mask & selectors.EVENT_WRITE),
            )
            if not alive:
                self.unregister(transport)
                continue
            if mask & selectors.EVENT_READ:
                ready[transport] = mask
        return ready

    def close(self):
        self._sel.close()


class Dealer:
    """One persistent outbound TCP connection with a fixed identity."""

    def __init__(self, address, port, identity: bytes, role: bytes):
        if role not in _VALID_ROLES:
            raise ValueError(f"invalid role {role!r}")
        self._address = address
        self._port = port
        self._identity = identity
        self._role = role
        self._conn = None
        self._pending_outbound = []
        self._closed = False
        self._lock = threading.Lock()
        self._connect_thread = None

    @property
    def identity(self):
        return self._identity

    @property
    def closed(self):
        if self._closed:
            return True
        return self._conn is not None and self._conn.closed

    @property
    def wants_write(self):
        return self._conn is not None and self._conn.wants_write

    def connect(self, timeout=None, retry=True, retry_interval=0.2):
        """Connect to the peer.

        retry=True (default): returns immediately and keeps retrying in the
        background until it succeeds or close() is called - this is needed
        because some callers (e.g. Session.host()) construct and connect a
        Remote's Dealers *before* the server process they're about to spawn
        even exists yet (mirrors zmq DEALER's own connect-before-peer-exists
        tolerance). Anything sent via send() before the connection completes
        is queued and flushed once it does.

        retry=False: a single bounded blocking connect attempt, raising on
        failure - for one-shot callers (e.g. porcelain.request_session_info)
        that want a fast, clear failure rather than a silent background
        retry loop.
        """
        if not retry:
            sock = _tcp_connect(self._address, self._port, timeout=timeout)
            self._finish_connect(sock)
            return

        self._connect_thread = threading.Thread(
            target=self._connect_loop, args=(retry_interval,), daemon=True)
        self._connect_thread.start()

    def _connect_loop(self, retry_interval):
        while not self._closed:
            try:
                sock = _tcp_connect(self._address, self._port, timeout=retry_interval)
            except OSError:
                time.sleep(retry_interval)
                continue
            try:
                self._finish_connect(sock)
            except OSError:
                try:
                    sock.close()
                except OSError:
                    pass
                time.sleep(retry_interval)
                continue
            return

    def _finish_connect(self, sock):
        handshake = _LEN.pack(len(self._identity)) + self._identity + self._role
        sock.sendall(handshake)
        sock.settimeout(None)
        conn = _Connection(sock, self._identity)
        with self._lock:
            if self._closed:
                conn.close()
                return
            self._conn = conn
            for chunks in self._pending_outbound:
                conn.queue_send(chunks)
            self._pending_outbound.clear()
            # Actually put anything queued on the wire now - queuing alone
            # leaves it sitting in the buffer forever unless something else
            # happens to poll/flush this Dealer afterwards.
            conn.flush()

    def fileno(self):
        return self._conn.fileno()

    def send(self, chunks) -> bool:
        # Held for the whole operation, not just the None-check: _finish_connect()
        # (running on the background connect thread) mutates the same
        # _Connection's buffer under this same lock, and queue_send()+flush()
        # are not safe to run concurrently with that from a different thread.
        with self._lock:
            if self._conn is None:
                if self._closed:
                    return False
                self._pending_outbound.append(chunks)
                return True
            self._conn.queue_send(chunks)
            return self._conn.flush()

    def service(self, readable, writable):
        with self._lock:
            if self._conn is None:
                return True
            return self._conn.service(readable, writable)

    def poll(self, timeout_ms=0):
        """Standalone convenience poll for a single Dealer used outside of a
        shared Poller (e.g. the ttl.py heartbeat subprocess, or tests)."""
        if self._conn is None:
            # Still background-connecting - wait (in short increments) for
            # it rather than sleeping out the whole timeout and returning,
            # which callers (e.g. ttl.py) would otherwise misread as "timed
            # out waiting for a reply" and give up on the very first poll.
            deadline = None if timeout_ms is None else time.time() + timeout_ms / 1000.0
            check_interval = 0.05
            while self._conn is None:
                if deadline is not None and time.time() >= deadline:
                    return
                remaining = check_interval if deadline is None else min(check_interval, max(0.0, deadline - time.time()))
                time.sleep(remaining)
            if deadline is not None:
                timeout_ms = max(0.0, (deadline - time.time()) * 1000.0)

        sel = selectors.DefaultSelector()
        mask = selectors.EVENT_READ | (selectors.EVENT_WRITE if self.wants_write else 0)
        sel.register(self, mask, self)
        timeout = None if timeout_ms is None else timeout_ms / 1000.0
        for _key, ready_mask in sel.select(timeout):
            self.service(bool(ready_mask & selectors.EVENT_READ), bool(ready_mask & selectors.EVENT_WRITE))
        sel.close()

    def recv(self):
        with self._lock:
            if self._conn is None:
                return None
            pending = self._conn.pop_pending()
            if not pending:
                return None
            # Re-queue any extra messages decoded in the same pass so recv()
            # keeps its "one message per call" contract.
            if len(pending) > 1:
                self._conn._pending = pending[1:] + self._conn._pending
            return pending[0]

    def close(self):
        with self._lock:
            self._closed = True
            if self._conn is not None:
                self._conn.close()


class Router:
    """Multiplexes many already-accepted connections that share one role.

    Connections are handed in via add_connection() (normally fed by a
    Listener's accept loop through a per-role queue), not accepted directly
    by this class.
    """

    def __init__(self):
        self._poller = Poller()
        self._connections = {}  # identity -> _Connection
        self._inbox = collections.deque()

    def add_connection(self, identity: bytes, sock: socket.socket):
        old = self._connections.get(identity)
        if old is not None:
            # A reconnect always wins over a stale/still-registered old
            # connection for the same identity (mirrors zmq ROUTER behavior).
            logger.debug("Replacing existing connection for identity %r", identity)
            self._poller.unregister(old)
            old.close()
        conn = _Connection(sock, identity)
        self._connections[identity] = conn
        self._poller.register(conn)

    def send(self, identity: bytes, chunks) -> bool:
        conn = self._connections.get(identity)
        if conn is None or conn.closed:
            logger.debug("Router.send: unknown/dead identity %r, dropping message", identity)
            return False
        conn.queue_send(chunks)
        return conn.flush()

    def disconnect(self, identity: bytes):
        conn = self._connections.pop(identity, None)
        if conn is not None:
            self._poller.unregister(conn)
            conn.close()

    def poll(self, timeout_ms=0):
        ready = self._poller.poll(timeout_ms)
        for conn in ready:
            for chunks in conn.pop_pending():
                self._inbox.append((conn.identity, chunks))
        dead = [identity for identity, conn in self._connections.items() if conn.closed]
        for identity in dead:
            del self._connections[identity]

    def recv(self):
        if not self._inbox:
            return None
        return self._inbox.popleft()

    @property
    def identities(self):
        return list(self._connections.keys())

    def close(self):
        for conn in list(self._connections.values()):
            conn.close()
        self._connections.clear()
        self._poller.close()


class Listener:
    """Owns the single bound/listening TCP socket for the whole server.

    Runs its own accept loop in a background thread. Each accepted
    connection's handshake (identity + role) is read on its own short-lived
    thread, so one slow/silent/malicious peer can never block the shared
    accept loop from servicing the other roles.
    """

    def __init__(self, port, address='0.0.0.0', handshake_timeout=HANDSHAKE_TIMEOUT):
        self._sock = _tcp_bind(address, port)
        self._handshake_timeout = handshake_timeout
        self._queues = {}
        self._exit_event = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, name='net-listener', daemon=True)

    @property
    def port(self):
        return self._sock.getsockname()[1]

    def register_role(self, role: bytes, target_queue: "queue.Queue"):
        if role not in _VALID_ROLES:
            raise ValueError(f"invalid role {role!r}")
        self._queues[role] = target_queue

    def start(self):
        self._thread.start()

    def _accept_loop(self):
        self._sock.settimeout(0.5)
        while not self._exit_event.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handshake, args=(conn,), daemon=True).start()

    def _handshake(self, conn: socket.socket):
        conn.settimeout(self._handshake_timeout)
        try:
            identity_len, = _LEN.unpack(_recv_exact(conn, 4))
            if identity_len > MAX_IDENTITY_SIZE:
                raise HandshakeError(f"identity too large ({identity_len} bytes)")
            identity = _recv_exact(conn, identity_len)
            role = _recv_exact(conn, 1)
            if role not in _VALID_ROLES:
                raise HandshakeError(f"unknown role {role!r}")
        except (HandshakeError, socket.timeout, OSError) as e:
            logger.warning("Rejecting connection during handshake: %s", e)
            try:
                conn.close()
            except OSError:
                pass
            return

        target_queue = self._queues.get(role)
        if target_queue is None:
            logger.warning("No queue registered for role %r, dropping connection", role)
            conn.close()
            return
        target_queue.put((identity, conn))

    def stop(self):
        self._exit_event.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)
