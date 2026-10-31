from uuid import uuid4
from deepdiff import Delta

try:
    import _pickle as pickle
except ImportError:
    import pickle


def object_classes(type_id):
    """ """
    return _TYPE_MAP.get(type_id)


class ReplicationObject(object):
    __slots__ = ["_sha"]

    def _serialize(self):
        """Convert the object into chunks of bytes"""
        raise NotImplementedError()

    def _deserialize(self, chunks):
        """Load chunks of to object"""
        raise NotImplementedError()

    @staticmethod
    def from_raw_chunks(chunks):
        """Reconstruct a replication object from chunks"""
        type_num = pickle.loads(chunks[0])
        obj = object_classes(type_num)()
        obj._deserialize(chunks)

        return obj

    def as_raw_chunks(self):
        return self._serialize()


class Commit(ReplicationObject):
    __slots__ = ["node_id", "delta", "deps"]

    type_num = 1

    def __init__(self):
        self.node_id = None
        self.deps = None
        self.delta = None

    def _serialize(self):
        chunks = []
        chunks.append(pickle.dumps(self.type_num, protocol=4))
        chunks.append(self.node_id.encode())
        chunks.append(pickle.dumps(self.deps, protocol=4))
        chunks.append(self.delta.dumps())
        return chunks

    def _deserialize(self, chunks):
        self.node_id = chunks[1].decode()
        self.deps = pickle.loads(chunks[2])
        self.delta = Delta(chunks[3])


class Node(ReplicationObject):
    __slots__ = [
        "uuid",  # uuid used as key      (string)
        "data",  # dcc data ref          (DCC type)
        "instance",  # raw data              (json)
        "dependencies",  # dependencies array    (string)
        "owner",  # Data owner            (string)
        "last_commit",  # Serialized local buffer (bytes)
        "state",  # Node state            (int)
        "sender",  # Node sender origin (client uuid)
    ]

    type_num = 2

    def __init__(
        self,
        owner=None,
        instance=None,
        uuid=None,
        data=None,
        sender=None,
        dependencies=[],
        state=-1,
    ):

        self.uuid = uuid if uuid else str(uuid4())
        self.owner = owner
        self.last_commit = None
        self.state = state
        self.data = {}
        self.instance = instance
        self.data = data
        self.dependencies = dependencies
        self.sender = sender

    def _serialize(self):
        chunks = []
        chunks.append(pickle.dumps(self.type_num, protocol=4))
        chunks.append(self.uuid.encode())
        chunks.append(self.owner.encode())
        chunks.append(pickle.dumps(self.dependencies, protocol=4))
        chunks.append(pickle.dumps(self.data, protocol=4))

        return chunks

    def _deserialize(self, chunks):
        self.uuid = chunks[1].decode()
        self.owner = chunks[2].decode()
        self.dependencies = pickle.loads(chunks[3])
        self.data = pickle.loads(chunks[4])

    def patch(self, commit: Commit):
        self.data = self.data + commit.delta
        self.dependencies = commit.deps

    def add_dependency(self, dependency):
        if not self.dependencies:
            self.dependencies = []
        if dependency not in self.dependencies:
            self.dependencies.append(dependency)

    def __repr__(self):
        return f" {self.uuid} - owner: {self.owner} - deps: {self.dependencies}"


class Command(ReplicationObject):
    type_num = 3

    def __init__(self, owner=None, data=None):
        self.owner = owner
        self.data = data
        self.str_type = type(self).__name__

    def _serialize(self):
        chunks = []
        chunks.append(pickle.dumps(self.type_num, protocol=4))
        chunks.append(self.owner.encode())
        chunks.append(pickle.dumps(self.data, protocol=4))

        return chunks

    def _deserialize(self, chunks):
        self.owner = chunks[1].decode()
        self.data = pickle.loads(chunks[2])

    def execute(self, graph):
        raise NotImplementedError()


class Delete(Command):
    type_num = 4

    def execute(self, graph):
        assert self.data

        if graph and self.data in graph.keys():
            # Clean all reference to this node
            for key, value in graph.items():
                if value.dependencies and self.data in value.dependencies:
                    value.dependencies.remove(self.data)
            # Remove the node itself
            del graph[self.data]


class Right(Command):
    type_num = 5

    def execute(self, graph):
        assert self.data
        nodes_ids = self.data.get("uuid")
        new_owner = self.data.get("owner")

        for node_id in nodes_ids:
            node = graph.get(node_id)
            if node:
                node.owner = new_owner


class Config(Command):
    type_num = 6
    pass


class Snapshot(Command):
    type_num = 7
    pass


class ServerSnapshot(Command):
    type_num = 8
    pass


class Auth(Command):
    type_num = 9
    pass


class Disconnect(Command):
    type_num = 10
    pass


class Kick(Command):
    type_num = 11
    pass


class UpdateClientsState(Command):
    type_num = 12
    pass


class UpdateUserMetadata(Command):
    type_num = 13
    pass


class RequestServerInfo(Command):
    type_num = 14
    pass


OBJECT_CLASSES = (
    Commit,
    Node,
    Command,
    UpdateUserMetadata,
    UpdateClientsState,
    Kick,
    Disconnect,
    Auth,
    ServerSnapshot,
    Snapshot,
    Config,
    Right,
    Delete,
    RequestServerInfo,
)

_TYPE_MAP = {}

for cls in OBJECT_CLASSES:
    _TYPE_MAP[cls.type_num] = cls
