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


import collections.abc
import copy
import gzip
import logging
import threading
from pathlib import Path
from uuid import uuid4
from datetime import datetime


from . import net
from .constants import FETCHED, RP_COMMON, STATE_INITIAL
from . import merge3 as _merge3  # patched: merge on receive
from .exception import NonAuthorizedOperationError
from .protocol import DataTranslationProtocol
from .objects import Commit, Node, ReplicationObject

try:
    import _pickle as pickle
except ImportError:
    import pickle


class GraphObjectStore(collections.abc.MutableMapping):
    """A basic structure to store object as a graph

    Reads of a single key (``__getitem__``/``get``) rely on the GIL making a
    plain dict lookup atomic and are left unlocked. Anything that mutates the
    store or iterates over more than one key takes ``_lock`` so that a writer
    on one thread (e.g. ``Repository.do_commit`` from the server's data
    thread) can never be observed mid-mutation by a reader on another thread
    (e.g. ``ServerNetService.disconnect_client``) - which previously could
    raise "RuntimeError: dictionary changed size during iteration".
    """

    def __init__(self, *args, **kwargs):
        self._lock = threading.RLock()
        self.store = dict()
        self.update(dict(*args, **kwargs))  # use the free update to set keys

    def __getitem__(self, key):
        return self.store[key]

    def __setitem__(self, key, value):
        with self._lock:
            self.store[key] = value

    def __delitem__(self, key):
        with self._lock:
            del self.store[key]

    def __iter__(self):
        with self._lock:
            return iter(list(self.store.keys()))

    def __len__(self):
        return len(self.store)

    def __repr__(self):
        str = "\n"
        for item in self.values():
            str += repr(item)
        return str

    def keys(self):
        with self._lock:
            return list(self.store.keys())

    def values(self):
        with self._lock:
            return list(self.store.values())

    def items(self):
        with self._lock:
            return list(self.store.items())

    def get_parents(self, target: str):
        """Retrieve node's parents

        :param node: target node
        :type node: Node
        """
        parents = []
        for node in self.values():
            if node.dependencies and target in node.dependencies:
                parents.append(node)

        return parents

    def rbfs_from(self, node: Node):
        """Run a reversed Breadth Fist Search from the given nodes

        :param node:
        """
        visited = []
        queue = node if isinstance(node, list) else [node]

        with self._lock:
            store_snapshot = dict(self.store)

        while queue:
            current = queue.pop(0)

            if current not in visited:
                visited.append(current)
                current_ref = store_snapshot.get(current)
                if current_ref and current_ref.dependencies:
                    queue.extend(current_ref.dependencies)

        return reversed(visited)


class Remote:
    def __init__(
        self,
        name="origin",
        address="127.0.0.1",
        port=5555,
        server_password=None,
        admin_password=None,
        realtime=False,
    ):
        self.name = name
        self.address = address
        self.port = port
        self.realtime = realtime
        self.server_password = server_password
        self.admin_password = admin_password

        self.online_users = {}
        self.connection_status = STATE_INITIAL
        self.uuid = uuid4()
        self.poller = net.Poller()

        # Both connect to the same port - command/data are distinguished by
        # role at connect-handshake time, not by a separate port each.
        self.command = net.Dealer(
            address, port, identity=self.uuid.bytes, role=net.ROLE_COMMAND
        )
        self.command.connect()

        self.data = net.Dealer(
            address, port, identity=self.uuid.bytes, role=net.ROLE_DATA
        )
        self.data.connect()

        self.poller.register(self.command)
        self.poller.register(self.data)

    def is_admin(self):
        return self.admin_password is not None

    def is_private(self):
        return self.server_password is not None


class Repository:
    """
    Structure responsible for replication graph manipulation
    """

    def __init__(
        self,
        rdp: DataTranslationProtocol = None,
        username: str = None,
        bare=False,
        *args,
        **kwargs,
    ):
        self.username = username
        self.object_store = GraphObjectStore()
        self.staging = GraphObjectStore()
        self.rdp = rdp
        self.remotes = {}
        self.remote = None
        self._bare = bare

    def dumps(self, filepath: str):
        """Dumps the repository data to a .db file

        :param filepath: target filepath
        :type filepath: str
        """
        nodes = []
        for node in self.graph.values():
            nodes.append(node.as_raw_chunks())

        db = dict()
        db["nodes"] = nodes

        if self.remote:
            db["users"] = copy.copy(self.remote.online_users)

        stime = datetime.now().strftime("%Y_%m_%d_%H-%M-%S")

        filepath = Path(filepath)
        filepath = filepath.with_name(f"{filepath.stem}_{stime}{filepath.suffix}")

        with gzip.open(filepath, "wb") as f:
            logging.info(f"Writing session snapshot to {filepath}")
            pickle.dump(db, f, protocol=4)

    def loads(self, filepath: str):
        """Load a repository snapshot

        :param filepath: snapshot filepath
        :type filepath: str
        """
        f = gzip.open(filepath, "rb")
        db = pickle.load(f)

        nodes = db.get("nodes")

        logging.info(f"Loading {len(nodes)} node")
        self.object_store.clear()
        for node_data in nodes:
            instance = Node.from_raw_chunks(node_data)
            self.do_commit(instance)
            instance.state = FETCHED

        logging.info(f"Repository loaded from {filepath}")

    def get_orphans_nodes(self):
        return list(set(self.object_store.keys()).difference(self.index_sorted))

    def get_node_by_datablock(self, datablock, default=None):
        for v in self.object_store.values():
            if not v.instance:
                continue
            if datablock == v.instance:
                return v
        return default

    def do_commit(self, update: ReplicationObject, cache_delta=False):
        node_id = getattr(update, "uuid", getattr(update, "node_id", None))
        delta_based = isinstance(update, Commit)
        if node_id:
            existing_node = self.object_store.get(node_id)

            if existing_node:
                if delta_based:
                    previous = existing_node.data
                    existing_node.patch(update)
                    existing_node.data = _merge3.merge_incoming(previous, existing_node.data)  # patched
                else:
                    existing_node.data = _merge3.merge_incoming(existing_node.data, update.data)  # patched
                    existing_node.dependencies = update.dependencies
            elif not delta_based:
                self.object_store[node_id] = update
            else:
                logging.error(f"Skipping update : {update}")
            logging.debug(f"Committed {node_id}")

    def assert_modification_rights(self, node_id):
        node = self.graph.get(node_id)
        if node is None:
            logging.warning(f"Node {node_id} not found in graph")
        if node and node.owner not in [self.username, RP_COMMON]:
            raise NonAuthorizedOperationError(
                f"Not authorized to modify the node {node_id}"
            )

    def is_node_readonly(self, node_id: str) -> bool:
        """Check local user modification rights on a node

        :param node_id: node identifier
        :type node_id: str
        :return: bool
        """
        node = self.graph.get(node_id)
        return node and (node.owner in [self.username, RP_COMMON])

    def push(self, socket, replication_object, identity=None, force=False):
        chunks = replication_object.as_raw_chunks()
        if identity is not None:
            # Server to specific Client case
            return socket.send(identity, chunks)
        else:
            return socket.send(chunks)

    def fetch(self, socket):
        """
        Here we receive data from the wire:
            - read data from the socket
            - reconstruct an instance

        Returns None if no message is currently available (socket.recv() is
        non-blocking) - callers should only call this after their poll loop
        indicated the socket has something ready.
        """
        if self._bare:
            result = socket.recv()
            if result is None:
                return None
            identity, chunks = result
        else:
            chunks = socket.recv()
            if chunks is None:
                return None
            identity = "server"

        return (identity, ReplicationObject.from_raw_chunks(chunks))

    @property
    def heads(self):
        return [
            n.uuid
            for n in self.object_store.values()
            if self.rdp.implementations[n.data["type_id"]].is_root
        ]

    @property
    def index_sorted(self):
        """Return the repository graph node index sorted by dependencies
        order

        :return: list
        """
        return self.object_store.rbfs_from(self.heads)

    @property
    def graph(self):
        return self.object_store
