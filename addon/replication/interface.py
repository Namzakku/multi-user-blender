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

import logging
import os
import queue
import subprocess
import sys
import traceback
import os
import queue
import sys

from replication import __version__

from .constants import (
    FETCHED,
    SERVER_SCRIPT_PATH,
    STATE_ACTIVE,
    STATE_AUTH,
    STATE_INITIAL,
    CONNECTING,
    STATE_LOBBY,
    STATE_SRV_SYNC,
    STATE_SYNCING,
    UP,
    TTL_SCRIPT_PATH,
    STATE_QUITTING,
)
from .exception import StateError
from .repository import Repository
from .objects import (
    ReplicationObject,
    Auth,
    Delete,
    Disconnect,
    Right,
    ServerSnapshot,
    Snapshot,
    UpdateClientsState,
    UpdateUserMetadata,
)
from .utils import current_milli_time
from . import porcelain

this = sys.modules[__name__]


class Session(object):
    def __init__(self):
        self.repository = None
        self.callbacks = {}
        self._state = STATE_INITIAL
        self._state_progress = {"current": -1, "total": -1}
        self._server = None

        # Networking
        self._state = STATE_INITIAL
        self._srv_snapshot_size = 0
        self._srv_snapshot_progress = 0

    def register(self, name):
        def func_wrapper(func):
            self.callbacks[name] = func
            return func

        return func_wrapper

    def call_registered(self, name=None, **kwargs):
        func = self.callbacks.get(name, None)
        if func is None:
            logging.info("No function registered against - " + str(name))
            return None
        return func(**kwargs)

    def connect(
        self,
        repository: Repository = None,
        remote: str = "origin",
        timeout=1000,
        server_password=None,
        admin_password=None,
        subprocess_python_args=[],
    ):
        """Connect to a session

        :param id: user name
        :type id: string
        :param address: host ip address
        :type address: string
        :param port: host port
        :type port: int
        """
        self.repository = repository
        self.remote = repository.remotes.get(remote)
        self._data_protocol = repository.rdp

        self._id = repository.username
        # uuid needed to avoid reconnexion problems on router sockets
        self._connection_timeout = timeout
        self._state = CONNECTING

        auth_type = "ADMIN" if admin_password else "CLIENT"
        auth_request = Auth(
            owner=self._id,
            data={
                "AUTH_TYPE": auth_type,
                "AUTH_ID": self._id,
                "S_PWD": server_password,
                "A_PWD": admin_password,
                "VERSION": __version__,
            },
        )
        self.repository.push(self.remote.command, auth_request)

        self._state = STATE_AUTH
        self._connection_start_time = current_milli_time()
        self._ttl = subprocess.Popen(
            [
                sys.executable,
                *subprocess_python_args,
                TTL_SCRIPT_PATH,
                "-p",
                str(self.remote.port),
                "-d",
                self.remote.address,
                "-i",
                str(self.remote.uuid),
                "-t",
                str(timeout),
            ]
        )

    def host(
        self,
        id="Default",
        repository: Repository = None,
        remote="origin",
        timeout=5000,
        server_password=None,
        admin_password=None,
        cache_directory="",
        server_log_level="INFO",
        subprocess_python_args=[],
    ):
        """Host a session

        :param id: user name
        :type id: string
        :param address: host ip address
        :type address: strings
        :param port: host port
        :type port: int
        """
        active_remote = repository.remotes.get(remote)
        # Create a server and serve
        self._server = subprocess.Popen(
            [
                sys.executable,
                *subprocess_python_args,
                SERVER_SCRIPT_PATH,
                "-p",
                str(active_remote.port),
                "-t",
                str(timeout),
                "-spwd",
                str(server_password),
                "-apwd",
                str(admin_password),
                "--attached",
                "--log-level",
                server_log_level,
                "--log-file",
                os.path.join(cache_directory, "multiuser_server.log"),
            ]
        )

        self.connect(
            repository=repository,
            remote=remote,
            timeout=timeout,
            admin_password=admin_password,
            server_password=server_password,
        )

    def init(self):
        """Init the repository data

        commit and push initial graph to the server
        """
        if len(self.repository.object_store) == 0:
            logging.error("Add some data first")
            return

        self.request_server_repository_init()

    def disconnect(self, reason: str = "None"):
        """Disconnect from session"""
        self._state = STATE_QUITTING
        self.remote.command.close()
        self.remote.data.close()
        logging.info(f"Exiting session, reason {reason}")
        if self.is_connection_alive():
            self._ttl.kill()
        if self._server:
            self._server.kill()
        self.call_registered("on_exit", reason=reason)

        self._state = STATE_INITIAL

    # NETWORKING
    def is_connection_alive(self):
        return self._ttl.poll() is None

    def _assert_state(self, state):
        if self._state not in state:
            raise StateError("Client in wrong state")

    def request_snapshot_init(self):
        """Ask to the server for repository init."""

        logging.info("Snapshot: request init")

        self._snapshot_progress = 0
        self._snapshot_total = 0

        snapshot_request = Snapshot(owner=self._id, data={"STATE": "REQUEST_INIT"})

        self.repository.push(self.remote.command, snapshot_request)

    def request_server_repository_init(self):
        """Ask to the server for repository init.

        :param command: incoming command
        :type command: Auth
        """
        logging.info("Request server init")

        self._srv_snapshot_size = len(self.repository.object_store)
        keys = [k for k, v in self.repository.object_store.items()]
        assert self._srv_snapshot_size > 0

        snapshot_cmd = ServerSnapshot(
            owner="server",
            data={"STATE": "INIT", "SIZE": self._srv_snapshot_size, "NODES": keys},
        )
        self.repository.push(self.remote.command, snapshot_cmd)

        self._state = STATE_SRV_SYNC

    def handle_authentification(self, command):
        """Manage client authentification

        :param command: incoming command
        :type command: Auth
        """
        self._assert_state([STATE_AUTH])

        connection_status = command.data

        if "LOBBY" in connection_status:
            self._state = STATE_LOBBY
            if self._server:
                self.request_server_repository_init()
        if "RUNNING" in connection_status:
            self._state = STATE_LOBBY
            self.request_snapshot_init()
        if "FAILED" in connection_status:
            self.disconnect(
                reason=f"Failed to connect, authentification refused [{connection_status}]"
            )

    def handle_client_snapshot(self, command):
        """Manage incoming snapshot commands

        :param command: incoming command
        :type command: Snapshot
        """
        self._assert_state([STATE_SYNCING, STATE_LOBBY])

        snapshot_state = command.data["STATE"]
        snapshot_data = command.data.get("DATA")
        if snapshot_state == "INIT":
            logging.info("client init")
            self._snapshot_progress = 0
            self._snapshot_catalog = command.data.get("CATALOG")
            self._snapshot_total = len(self._snapshot_catalog)
            self._snapshot_late_updates = queue.Queue()

            self._state_progress = {
                "current": self._snapshot_progress,
                "total": self._snapshot_total,
            }

            self._current_snapshot = self._snapshot_catalog.pop()
            self.get_snapshot(self._current_snapshot)

            self._state = STATE_SYNCING
        elif snapshot_state == "SET":
            if snapshot_data == b"removed":
                logging.info(
                    f"Snapshot : node {self._current_snapshot} removed, skipping it."
                )
            else:
                node = ReplicationObject.from_raw_chunks(snapshot_data)
                node.state = FETCHED

                if node.uuid != self._current_snapshot:
                    logging.error("Snapshot : wrong node received, skipping it")
                    return

                self.repository.do_commit(node)

            self._snapshot_progress += 1
            self._state_progress = {
                "current": self._snapshot_progress,
                "total": self._snapshot_total,
            }

            if not self._snapshot_catalog:
                # Apply late updates
                logging.info(
                    f"Snapshot : Applying {self._snapshot_late_updates.qsize()} late updates."
                )
                while not self._snapshot_late_updates.empty():
                    late_update = self._snapshot_late_updates.get()
                    self.repository.do_commit(late_update)

                snapshot_request = Snapshot(owner=self._id, data={"STATE": "DONE"})
                self.repository.push(self.remote.command, snapshot_request)
                logging.info("Snapshot : done.")
                self._state = STATE_ACTIVE
                self.call_registered("on_connection")
            else:
                self._current_snapshot = self._snapshot_catalog.pop()
                self.get_snapshot(self._current_snapshot)

    def handle_server_repository_init(self, command):
        """Manage server initialization commands

        :param command: incoming command
        :type command: ServerSnapshot
        """
        self._assert_state([STATE_SRV_SYNC])

        cli_snapshot_state = command.data.get("STATE")

        if cli_snapshot_state == "ACCEPTED":
            for index, node in enumerate(self.repository.object_store.values()):
                porcelain.commit(self.repository, node.uuid)
                node.state = UP
                snapshot_cmd = ServerSnapshot(
                    owner="server", data={"STATE": "SET", "DATA": node.as_raw_chunks()}
                )

                self.repository.push(self.remote.command, snapshot_cmd)
                self._state_progress = {
                    "current": index,
                    "total": len(self.repository.object_store),
                }

            snapshot_cmd = ServerSnapshot(owner="server", data={"STATE": "END"})
            self.repository.push(self.remote.command, snapshot_cmd)
        elif cli_snapshot_state == "DONE":
            self._state = STATE_ACTIVE
            self.call_registered("on_connection")
        elif cli_snapshot_state == "REJECTED":
            logging.error("client snapshot refused by the server.")
            self._state = STATE_LOBBY

    def get_snapshot(self, id):
        """Ask a specific snapshot to the server

        :param id: uuid of the data
        :type id: str
        """
        logging.debug(f"Snapshot : request node {id}")
        snapshot_request = Snapshot(owner=self._id, data={"STATE": "GET", "ID": id})

        self.repository.push(self.remote.command, snapshot_request)

    def listen(self, timeout: int = 0):
        """Non-blocking network listening

        :param timeout: network packet waiting time in millisecond
        :type timeout: int
        """
        if self._state in [STATE_INITIAL, STATE_QUITTING]:
            return

        if not self.is_connection_alive():
            self.disconnect(reason="Server lost")
            return

        # Servicing both dealers here also flushes/reads them - fetch()
        # below is non-blocking and returns None if nothing complete has
        # arrived yet, which can happen even right after poll() (a socket
        # can be readable with only a partial frame buffered so far).
        self.remote.poller.poll(timeout)

        # COMMANDS I/O
        try:
            command_result = self.repository.fetch(self.remote.command)
        except Exception as e:
            logging.error(f"Corrupted frame received, skipping it. Cause:{e}")
            traceback.print_exc()
            command_result = None

        if command_result is not None:
            identity, command = command_result
            # AUTHENTIFICATION
            if isinstance(command, Auth):
                self.handle_authentification(command)

            # DISCONNECT CONFIRMATION
            if isinstance(command, Disconnect):
                reason = command.data.get("reason", "None")
                self.disconnect(reason=reason)

            # CLIENTS INFO UPDATE
            if isinstance(command, UpdateClientsState):
                self.repository.remote.online_users = command.data

            if isinstance(command, UpdateUserMetadata):
                user = self.repository.remote.online_users.get(command.owner)
                user["metadata"].update(command.data)

            # SERVER-->CLIENT SNAPSHOT
            if isinstance(command, Snapshot):
                self.handle_client_snapshot(command)

            # CLIENT -> SERVER SNAPSHOT
            if isinstance(command, ServerSnapshot):
                self.handle_server_repository_init(command)

            # GRAPH OPERATION (DELETE, CHANGE_RIGHT)
            if type(command) in [Delete, Right]:
                command.execute(self.repository.object_store)

        # DATA IN
        try:
            data_result = self.repository.fetch(self.remote.data)
        except Exception as e:
            logging.error(f"Corrupted frame received, skipping it. Cause:{e}")
            traceback.print_exc()
            data_result = None

        if data_result is not None:
            identity, update = data_result
            # Client snapshot
            if self._state == STATE_SYNCING:
                # If the snapshot is expected in the snapshot catalog we store
                logging.info("Snapshot: Adding an update for the late one...")
                self._snapshot_late_updates.put(update)

            # Store received updates
            if self._state == STATE_ACTIVE:
                self.repository.do_commit(update)
                # TODO: remove with stateless refactor
                node_id = getattr(update, "uuid", getattr(update, "node_id", None))
                node = self.repository.graph.get(node_id)
                node.state = FETCHED
        # Various timeout checks
        # auth
        if self._state == STATE_AUTH:
            if (
                current_milli_time() - self._connection_start_time
            ) > self._connection_timeout:
                self.disconnect(reason="Connection failed, server not found")

    @property
    def state(self):
        """Get active session state

        :return: session state
        """
        return self._state

    @property
    def state_progress(self):
        """Get active session state

        :return: session state
        """
        return self._state_progress

    @property
    def online_users(self):
        return self.repository.remote.online_users


this.session = Session()
