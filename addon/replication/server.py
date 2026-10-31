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

import argparse
import cmd
import logging
import os
import sys
from pathlib import Path
import traceback

# REFACTOR: remove this in favor of a better env setup
replication_lib = Path(os.path.dirname(os.path.realpath(__file__)))


sys.path.insert(0, str(replication_lib.parent))

import copy
import logging

import os
import queue
import sys
import threading
import time
from pathlib import Path

from replication import __version__
from replication.constants import (
    CLIENT_PING_FREQUENCY,
    CONNECTION_TIMEOUT,
    RP_COMMON,
    STATE_ACTIVE,
    STATE_DATA_PENDING,
    STATE_INITIAL,
    STATE_LOBBY,
    STATE_SRV_SYNC,
    STATE_WAITING,
    UP,
)
from replication.objects import (
    ReplicationObject,
    Auth,
    Delete,
    Disconnect,
    Kick,
    Right,
    ServerSnapshot,
    Snapshot,
    UpdateClientsState,
    UpdateUserMetadata,
    RequestServerInfo,
)

from replication.repository import Repository
from replication.utils import current_milli_time
from replication import net


class ServerNetService(threading.Thread):
    def __init__(self):

        # Threading
        threading.Thread.__init__(self)
        self.name = "command"
        self._exit_event = threading.Event()

        # Networking
        self._repository = Repository(bare=True)
        self._command = None
        self._data = None
        self._state = 0
        self.clients = {}
        self._ttl = None
        # kick() can be called from any thread (ServerShell's cmdloop
        # thread, in the CLI shell case) but Router/_Connection are only
        # safe to touch from ServerNetService's own thread - queue the
        # request and act on it from inside run().
        self._kick_queue = queue.Queue()
        # identity -> (auth_status, queued_at) for clients that passed auth
        # but whose DATA channel hasn't registered with the server yet.
        self._pending_auth = {}

    def listen(
        self,
        port=5560,
        server_password=None,
        admin_password="admin",
        attached=False,
        timeout=CONNECTION_TIMEOUT,
    ):
        self._server_password = server_password
        self._admin_password = admin_password
        self._port = port
        self._attached = attached
        self._connection_timeout = timeout

        # Single public port shared by all 3 roles - connections are
        # dispatched to the right subsystem by role tag at handshake time.
        self._listener = net.Listener(port)
        self._command_queue = queue.Queue()
        data_queue = queue.Queue()
        ttl_queue = queue.Queue()
        self._listener.register_role(net.ROLE_COMMAND, self._command_queue)
        self._listener.register_role(net.ROLE_DATA, data_queue)
        self._listener.register_role(net.ROLE_TTL, ttl_queue)
        self._listener.start()

        self._command = net.Router()

        # TTL communication - two one-way queues rather than one shared
        # queue (see ServerTTL for why: a single Queue would let ServerTTL's
        # own drain loop race with us to consume its own notifications).
        self._ttl_notify_queue = queue.Queue()  # ServerTTL -> us: STATE / LOST
        self._ttl_watch_queue = queue.Queue()  # us -> ServerTTL: STOP_WATCHING

        self._ttl = ServerTTL(
            ttl_queue, self._ttl_notify_queue, self._ttl_watch_queue, timeout=timeout
        )

        # Data handling
        self._data = ServerData(
            data_queue, repository=self._repository, clients_state=self.clients
        )

        self._client_snapshot_size = 0
        self._client_snapshot_progress = 0

        self.start()

    def _drain_new_connections(self):
        try:
            while True:
                identity, sock = self._command_queue.get_nowait()
                self._command.add_connection(identity, sock)
        except queue.Empty:
            pass

    def disconnect_client(self, client, reason):
        leaving_client = self.clients.get(client)
        if leaving_client:
            client_id = leaving_client.get("id")
            locked_nodes = [
                n.uuid for n in self._repository.graph.values() if n.owner == client_id
            ]

            if locked_nodes:
                reset_right = Right(
                    owner="server", data={"uuid": locked_nodes, "owner": RP_COMMON}
                )

                logging.debug(f"Changing node {locked_nodes} rights to COMMON")

                for cli in list(self.clients.keys()):
                    if cli != client:
                        self._repository.push(self._command, reset_right, identity=cli)
                        reset_right.execute(self._repository.object_store)

            del self.clients[client]
            logging.info(f"{client_id} disconnected from the server [{reason}]")

    def _update_clients_states(self):
        user_dict = {}
        for user, user_data in self.clients.items():
            user_dict[user_data["id"]] = user_data

        clients_states = UpdateClientsState(owner="server", data=user_dict)

        # Push it to every clients
        chunks = clients_states.as_raw_chunks()
        for client_uid in list(self.clients):
            self._command.send(client_uid, chunks)

    def _login_client(
        self, auth_type, uid, id, server_password, admin_password, version
    ):
        """Register a client on the server

        return:
        FAILED
        LOBBY
        RUNNING
        """
        logging.debug(f"Processing logging request from {id}")
        for cli in self.clients.values():
            if id == cli["id"]:
                logging.debug("client logged in")
                return "FAILED: client already logged in"

        if auth_type == "ADMIN" and admin_password != self._admin_password:
            return "FAILED: wrong admin password"

        if str(server_password) != self._server_password:
            return "FAILED: wrong server password"

        if version != __version__:
            return f"FAILED: wrong client version ({version} != {__version__})"

        logging.info(f"{id} logged in.")

        self.clients[uid] = {
            "id": id,
            "admin": auth_type == "ADMIN",
            "latency": 999,
            # Not STATE_LOBBY yet - ServerData excludes STATE_DATA_PENDING
            # clients from broadcast until their data channel registers (see
            # the pending-auth resolution block in run()), otherwise a live
            # update could land and silently drop before they're reachable.
            "status": STATE_DATA_PENDING,
            "metadata": {},
        }
        if self._state in [STATE_WAITING, STATE_SRV_SYNC]:
            return "LOBBY"
        else:
            return "RUNNING"

    def kick(self, user):
        """kick the given user - safe to call from any thread, the actual
        disconnect happens on this service's own thread in run() (see
        _kick_queue).

        :arg user: username of the kicked client
        :type user: str
        """
        self._kick_queue.put(user)

    def _do_kick(self, user):
        for k, v in list(self.clients.items()):
            if v["id"] == user:
                disconnect = Disconnect(
                    owner="server",
                    data={
                        "reason": "kicked by admin",
                    },
                )
                self._repository.push(self._command, disconnect, identity=k)

                self._ttl_watch_queue.put(("STOP_WATCHING", k))
                self.disconnect_client(k, "kicked")

                logging.warning(f"{user} kicked from the session.")
                return

        logging.error(f"Can't kick {user}, user not found.")

    def send_client_snapshot_init(self, client):
        catalog = [str(k) for k in self._repository.object_store.keys()]
        snapshot_state = Snapshot(
            owner="server", data={"STATE": "INIT", "CATALOG": catalog}
        )
        logging.info(f"{self.clients[client]['id']} - Snapshot : Pushing nodes catalog")
        self._repository.push(self._command, snapshot_state, identity=client)

    def handle_client_snapshot(self, command):
        """Handle client snapshot commands"""

        snapshot_state = command.data.get("STATE")
        snapshot_client = self.clients.get(command.sender)

        if snapshot_client:
            if snapshot_state == "REQUEST_INIT":
                self.send_client_snapshot_init(command.sender)
            elif snapshot_state == "GET":
                node = self._repository.graph.get(command.data["ID"])

                if node:
                    logging.debug(
                        f"{snapshot_client['id']} - Snapshot : pushing node {node.uuid}"
                    )
                else:
                    logging.debug(
                        f"{snapshot_client['id']} - Snapshot : node {command.data['ID']} no longer exist, skipping it."
                    )

                node_data = node.as_raw_chunks() if node else b"removed"
                snapshot_cmd = Snapshot(
                    owner="server", data={"STATE": "SET", "DATA": node_data}
                )
                self._repository.push(
                    self._command, snapshot_cmd, identity=command.sender
                )
            elif snapshot_state == "DONE":
                # Set client ready
                logging.info(f"{snapshot_client['id']} - Snapshot : done.")
                self.clients[command.sender]["status"] = STATE_ACTIVE
                self._update_clients_states()
        else:
            logging.error(f"Snapshot cancelled {command.sender} (client disconnected)")

    def handle_server_repository_init(self, command):
        cli_snapshot_state = command.data.get("STATE")
        cli_snapshot_lenght = command.data.get("SIZE")
        cli_snapshot_dict = command.data.get("NODES")
        cli_snapshot_data = command.data.get("DATA")

        if cli_snapshot_state == "INIT":
            if self._state == STATE_SRV_SYNC:  # REJECT
                snapshot_status = "REJECTED"
            if self._state == STATE_WAITING:
                snapshot_status = "ACCEPTED"
                self._client_snapshot_size = cli_snapshot_lenght
                self._client_snapshot_dict = cli_snapshot_dict

            snapshot_cmd = ServerSnapshot(
                owner="server", data={"STATE": snapshot_status}
            )
            self._repository.push(self._command, snapshot_cmd, identity=command.sender)
            self._state = STATE_SRV_SYNC

        if cli_snapshot_state == "SET":
            node = ReplicationObject.from_raw_chunks(cli_snapshot_data)
            node.state = UP

            self._repository.do_commit(node)

            self._client_snapshot_progress += 1
            logging.debug(
                f"Receiving snapshot {self._client_snapshot_progress}/{self._client_snapshot_size}"
            )
            self._client_snapshot_dict.remove(node.uuid)
            if len(self._client_snapshot_dict) == 0:
                # Launch snapshot for other waiting clients
                snapshot_cmd = ServerSnapshot(owner="server", data={"STATE": "DONE"})

                self._repository.push(
                    self._command, snapshot_cmd, identity=command.sender
                )

                for client in list(self.clients.keys()):
                    if client != command.sender:
                        self.send_client_snapshot_init(client)

                self._state = STATE_ACTIVE

        if cli_snapshot_state == "END":
            self.clients[command.sender]["status"] = STATE_ACTIVE
            self._update_clients_states()
            logging.info("Done")

    def run(self):
        logging.info(f"Listening on {self._port}.")

        self._state = STATE_WAITING

        while not self._exit_event.is_set():
            self._drain_new_connections()
            self._command.poll(1)

            try:
                while True:
                    self._do_kick(self._kick_queue.get_nowait())
            except queue.Empty:
                pass

            # COMMAND HANDLING
            try:
                result = self._repository.fetch(self._command)
            except Exception as e:
                logging.error(
                    f"Corrupted command frame received, skipping it. Cause:{e}"
                )
                traceback.print_exc()
                result = None

            if result is not None:
                identity, command = result
                command.sender = identity

                # AUHTENTIFICATION
                if isinstance(command, Auth):
                    auth_type = command.data.get("AUTH_TYPE")
                    auth_origin = command.sender
                    auth_id = command.data.get("AUTH_ID")
                    auth_s_pass = command.data.get("S_PWD", None)
                    auth_a_pass = command.data.get("A_PWD", None)
                    client_version = command.data.get("VERSION")

                    auth_status = self._login_client(
                        auth_type,
                        auth_origin,
                        auth_id,
                        auth_s_pass,
                        auth_a_pass,
                        client_version,
                    )

                    if auth_status in ("LOBBY", "RUNNING"):
                        # Hold the reply until the data channel registers -
                        # see the pending-auth resolution block below.
                        self._pending_auth[auth_origin] = (
                            auth_status,
                            current_milli_time(),
                        )
                    else:
                        auth_response = Auth(owner="server", data=auth_status)
                        self._repository.push(
                            self._command, auth_response, identity=command.sender
                        )
                        self._update_clients_states()

                # SERVER-> CLIENT SNAPSHOT
                if isinstance(command, Snapshot):
                    self.handle_client_snapshot(command)

                # CLIENT-> SERVER SNAPSHOT
                if isinstance(command, ServerSnapshot):
                    self.handle_server_repository_init(command)

                # CLIENT METADATA
                if isinstance(command, UpdateUserMetadata):
                    user = self.clients.get(command.sender)

                    if user:
                        try:
                            user["metadata"].update(command.data)
                        except Exception as e:
                            logging.error(e)
                            traceback.print_exc()
                        else:
                            for client_uid in list(self.clients):
                                if client_uid != command.sender:
                                    self._repository.push(
                                        self._command, command, identity=client_uid
                                    )

                # KICK
                if isinstance(command, Kick):
                    self.kick(command.data["user"])

                if isinstance(command, RequestServerInfo):
                    server_info = RequestServerInfo(
                        owner="server",
                        data={
                            "private": True if self._server_password else False,
                            "version": __version__,
                        },
                    )
                    self._repository.push(
                        self._command, server_info, identity=command.sender
                    )

                # OTHERS
                if type(command) in [Delete, Right]:
                    try:
                        command.execute(self._repository.object_store)
                    except Exception as e:
                        logging.error(e)
                        traceback.print_exc()
                    else:
                        chunks = command.as_raw_chunks()
                        for client_uid in list(self.clients):
                            self._command.send(client_uid, chunks)

            # PENDING AUTH RESOLUTION - a client that authenticated is held
            # out of self.clients' STATE_LOBBY (and thus out of ServerData's
            # broadcast) until its DATA channel has actually registered, so
            # a live update landing in between can't silently drop for it.
            if self._pending_auth:
                now = current_milli_time()
                connected = self._data.identities
                for auth_origin, (auth_status, queued_at) in list(
                    self._pending_auth.items()
                ):
                    client = self.clients.get(auth_origin)
                    if client is None:
                        del self._pending_auth[auth_origin]
                        continue

                    if auth_origin in connected:
                        client["status"] = STATE_LOBBY
                        auth_response = Auth(owner="server", data=auth_status)
                        self._repository.push(
                            self._command, auth_response, identity=auth_origin
                        )
                        self._update_clients_states()
                        del self._pending_auth[auth_origin]
                    elif now - queued_at > self._connection_timeout:
                        logging.warning(
                            f"{client['id']} authenticated but its data channel never "
                            "connected in time, rejecting."
                        )
                        auth_response = Auth(
                            owner="server", data="FAILED: data channel timeout"
                        )
                        self._repository.push(
                            self._command, auth_response, identity=auth_origin
                        )
                        self.disconnect_client(auth_origin, "data channel timeout")
                        del self._pending_auth[auth_origin]

            # TTL HANDLING
            try:
                while True:
                    notification = self._ttl_notify_queue.get_nowait()

                    if notification[0] == "STATE":
                        clients_states = notification[1]

                        # Prepare update
                        for id, state in clients_states.items():
                            cli = self.clients.get(id)
                            if cli:
                                cli.update(state)
                            else:
                                self._ttl_watch_queue.put(("STOP_WATCHING", id))

                        self._update_clients_states()

                    if notification[0] == "LOST":
                        self.disconnect_client(notification[1], "connection closed")
            except queue.Empty:
                pass

        while self._ttl._state != STATE_INITIAL:
            time.sleep(1)

        self._data.stop()
        self._data.join(timeout=2.0)
        self._command.close()
        self._listener.stop()

    def stop(self):
        self._ttl.stop()
        self._data.stop()
        self._exit_event.set()
        self._state = 0


class ServerData(threading.Thread):
    def __init__(self, connection_queue, repository=None, clients_state=None):
        threading.Thread.__init__(self)
        self.name = "data"
        self.daemon = False
        self._exit_event = threading.Event()
        self._repository = repository
        self._connection_queue = connection_queue

        # Update all clients - connections are handed in by the shared
        # net.Listener (ROLE_DATA) rather than binding our own port.
        self._data = net.Router()
        self.clients = clients_state

        self.start()

    @property
    def identities(self):
        return self._data.identities

    def _drain_new_connections(self):
        try:
            while True:
                identity, sock = self._connection_queue.get_nowait()
                self._data.add_connection(identity, sock)
        except queue.Empty:
            pass

    def run(self):
        while not self._exit_event.is_set():
            self._drain_new_connections()
            self._data.poll(1)

            try:
                result = self._repository.fetch(self._data)
            except Exception as e:
                logging.error(f"Corrupted data frame received, skipping it. Cause:{e}")
                traceback.print_exc()
                continue

            if result is None:
                continue

            identity, update = result
            self._repository.do_commit(update)

            # Serialize once and reuse the same chunks for every recipient -
            # as_raw_chunks() re-pickles the payload on every call, so doing
            # it inside the fan-out loop below cost one redundant
            # serialization per connected client instead of one total.
            chunks = update.as_raw_chunks()

            # Update all ready clients - skip clients still in
            # STATE_DATA_PENDING, whose data channel hasn't registered yet
            # (ServerNetService holds their auth reply until it has).
            for client_uid, client_data in list(self.clients.items()):
                if (
                    client_uid != identity
                    and client_data.get("status") != STATE_DATA_PENDING
                ):
                    if not self._data.send(client_uid, chunks):
                        logging.debug(
                            f"Broadcast to {client_uid!r} dropped (no live data connection)."
                        )

        self._data.close()

    def stop(self):
        self._exit_event.set()


class ServerTTL(threading.Thread):
    def __init__(
        self, connection_queue, notify_queue, watch_queue, timeout=CONNECTION_TIMEOUT
    ):
        # Threading
        threading.Thread.__init__(self)
        self.name = "ttl"
        self.daemon = False
        self._exit_event = threading.Event()

        # Networking - connection_queue is fed by the shared net.Listener
        # (ROLE_TTL). Replaces the old inproc zmq pipe with two one-way
        # queues rather than one shared queue: a single Queue has no notion
        # of sender, so ServerTTL's own STOP_WATCHING-drain loop would
        # otherwise race with ServerNetService to consume ServerTTL's own
        # just-put STATE/LOST notifications.
        self._connection_queue = connection_queue
        self._notify_queue = notify_queue  # ServerTTL -> ServerNetService: STATE / LOST
        self._watch_queue = watch_queue  # ServerNetService -> ServerTTL: STOP_WATCHING
        self._heartbeat = net.Router()

        self._timeout = timeout
        self._state = STATE_INITIAL
        self._clients_state = {}

        self.start()

    def _drain_new_connections(self):
        try:
            while True:
                identity, sock = self._connection_queue.get_nowait()
                self._heartbeat.add_connection(identity, sock)
        except queue.Empty:
            pass

    def run(self):
        self._state = STATE_ACTIVE
        last_update_time = current_milli_time()
        while not self._exit_event.is_set():
            self._drain_new_connections()
            self._heartbeat.poll(1)
            current_time = current_milli_time()

            message = self._heartbeat.recv()
            if message is not None:
                identity, chunks = message
                frame = chunks[0] if chunks else b""

                if frame == b"INIT":
                    self._clients_state[identity] = {}
                    self._clients_state[identity]["latency"] = 999
                    self._clients_state[identity]["last_received_update"] = current_time
                    self._clients_state[identity]["last_sent_update"] = current_time
                    self._heartbeat.send(identity, [b"PING"])

                client = self._clients_state.get(identity)

                if client is not None:
                    client["last_received_update"] = current_time

            try:
                while True:
                    notification = self._watch_queue.get_nowait()
                    if notification[0] == "STOP_WATCHING":
                        self.stop_monitor(notification[1])
            except queue.Empty:
                pass

            if current_time - last_update_time > 1000:
                last_update_time = current_time
                client_to_remove = []

                # Check clients status
                for client, client_data in self._clients_state.items():
                    client_data["latency"] = abs(
                        (client_data["last_received_update"])
                        - (client_data["last_sent_update"])
                    )

                    if client_data["latency"] > self._timeout:
                        client_to_remove.append(client)
                        self._notify_queue.put(("LOST", client))

                for client in client_to_remove:
                    self.stop_monitor(client)

                state_update = {
                    cli: {"latency": val["latency"]}
                    for cli, val in self._clients_state.items()
                }
                self._notify_queue.put(("STATE", state_update))

            for cli_key, cli_data in self._clients_state.items():
                if (
                    current_time - cli_data["last_received_update"]
                ) > CLIENT_PING_FREQUENCY and current_time - cli_data[
                    "last_sent_update"
                ] > CLIENT_PING_FREQUENCY:

                    self._heartbeat.send(cli_key, [b"PING"])
                    cli_data["last_sent_update"] = current_time

        self._heartbeat.close()
        self._state = STATE_INITIAL

    def stop_monitor(self, client):
        if client in self._clients_state:
            logging.debug(f"Removing client {client} from watchlist")
            del self._clients_state[client]

    @property
    def state(self):
        return self._state

    @property
    def clients_state(self):
        return copy.copy(self._clients_state)

    def stop(self):
        self._exit_event.set()


class ServerShell(cmd.Cmd):
    intro = (
        "Welcome the replication server shell.\n   Type help or ? to list commands.\n"
    )
    prompt = ">> "
    file = None

    def __init__(
        self,
        port=5555,
        timeout=CONNECTION_TIMEOUT,
        server_password=None,
        admin_password=None,
        attached=False,
    ):

        cmd.Cmd.__init__(self)
        self._net = ServerNetService()

        self._net.listen(
            port=port,
            server_password=server_password,
            admin_password=admin_password,
            attached=attached,
            timeout=timeout,
        )

    def do_users(self, args):
        """Print online users"""
        print(f"{len(self._net.clients)} user online")
        for cli_id, cli_data in self._net.clients.items():
            role = "admin" if cli_data["admin"] else ""
            print(f"{cli_data['id']}({role}) - {cli_data['latency']} ms")

    def do_kick(self, args):
        """Kick the target user"""
        self._net.kick(args)

    def do_exit(self, args):
        """Exit the server"""
        self._net.stop()
        return -1

    def do_EOF(self, args):
        print("*** EOF")
        return True


def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--port", default=5560, help="port to listen")
    parser.add_argument("-l", "--log-level", default="INFO", help="set logging level ")
    parser.add_argument(
        "-lf", "--log-file", default="multi_user_server.log", help="set log file output"
    )
    parser.add_argument(
        "-t",
        "--timeout",
        default=CONNECTION_TIMEOUT,
        help="connection timeout in millisecond",
    )
    parser.add_argument(
        "-apwd", "--admin-password", default="admin", help="session admin password"
    )
    parser.add_argument(
        "-spwd", "--server-password", default=None, help="session server password"
    )

    parser.add_argument(
        "--attached", help="server attached to a blender instance", action="store_true"
    )

    args = parser.parse_args()

    formatter = logging.Formatter(
        fmt="%(asctime)s SERVER %(levelname)-8s %(message)s", datefmt="%H:%M:%S"
    )

    logging.basicConfig(level=logging._nameToLevel[args.log_level])
    logger = logging.getLogger()
    handler = logging.FileHandler(args.log_file, mode="w")
    logger.addHandler(handler)

    for handler in logger.handlers:
        if isinstance(handler, logging.NullHandler):
            continue

        handler.setFormatter(formatter)

    shell = ServerShell(
        port=int(args.port),
        timeout=int(args.timeout),
        server_password=str(args.server_password),
        admin_password=str(args.admin_password),
        attached=bool(args.attached),
    )
    try:
        shell.cmdloop()
    except KeyboardInterrupt:
        shell.do_exit(None)


if __name__ == "__main__":
    cli()
