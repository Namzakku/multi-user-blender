import logging
import traceback
from uuid import uuid4

from . import net
from .constants import ADDED, COMMITED, FETCHED, RP_COMMON, UP
from . import merge3 as _merge3  # patched: three-way merge
from .exception import (
    NonAuthorizedOperationError,
    UnsupportedTypeError,
)
from .objects import (
    Commit,
    Delete,
    Kick,
    Node,
    ReplicationObject,
    RequestServerInfo,
    Right,
    UpdateUserMetadata,
)
from .repository import Remote, Repository


def add(
    repostitory: Repository,
    datablock: object,
    owner=None,
    dependencies=[],
    stamp=True,
    skip_unsupported=False,
):
    """Register a python to the given repository stagging area

    :param repository: Target repository
    :type repository: Repository
    :param objet: Any registered object
    :type object: Any registered object type in the given factory
    :param dependencies: Object dependencies uuid
    :type dependencies: Array of string
    :raise: UnsupportedTypeError
    """
    if repostitory.rdp.get_implementation(datablock):
        default_owner = RP_COMMON

        new_owner = owner if owner else default_owner
        new_node = Node(
            owner=new_owner, instance=datablock, dependencies=dependencies, state=ADDED
        )
        if stamp:
            try:
                setattr(datablock, "uuid", new_node.uuid)
            except AttributeError:
                logging.warning(f"Can stamp uuid on {type(datablock)}")

        dependencies = repostitory.rdp.resolve_deps(datablock)

        for dependance in dependencies:
            dep_ref = repostitory.get_node_by_datablock(dependance)
            if dep_ref:
                new_node.add_dependency(dep_ref.uuid)
            else:
                if dependance:
                    try:
                        new_child_node = add(repostitory, dependance, owner=new_owner)
                        if new_child_node:
                            new_node.add_dependency(new_child_node)
                    except UnsupportedTypeError as e:
                        if skip_unsupported:
                            logging.warning(f"Skipping {type(datablock)}.")
                        else:
                            raise e
        logging.debug(f"Registering {datablock} as {new_node.uuid} (owner:{new_owner})")
        repostitory.do_commit(new_node)

        return new_node.uuid
    elif skip_unsupported:
        logging.warning(f"Skipping {type(datablock)} not supported, skipping.")
    else:
        raise UnsupportedTypeError(f"{type(datablock)} not supported")


def apply(repository, node_id, force=False, stamp=True):
    """Apply proxy to version to local datablock

    :param node: node key to apply
    :type node: string
    :param force: force node apply
    :type force: bool
    """
    node = repository.graph.get(node_id)

    # Setup apply queue
    deps = repository.graph.rbfs_from(node.uuid)
    apply_queue = []
    for dep in deps:
        dep_node = repository.graph.get(dep)
        if dep_node and (dep_node.state in [FETCHED] or force):
            apply_queue.append(dep_node)

    # Apply node in dependencies order
    for node in apply_queue:
        logging.debug(f"Applying {node.uuid}")
        repository.graph.get(node)
        if node.instance is None:
            instance = repository.rdp.resolve(node.data)
            if instance is None:
                logging.debug(f"Instanciating {node.uuid}")
                node.instance = repository.rdp.construct(node.data)
            else:
                node.instance = instance
            if stamp:
                try:
                    setattr(node.instance, "uuid", node.uuid)
                except AttributeError:
                    logging.warning(f"Can stamp uuid on {type(node.instance)}")
        try:
            was_fetched = node.state == FETCHED  # patched: three-way merge
            # merge on a forced reload too, so it cannot wipe uncommitted local edits
            to_load, contributed = _merge3.merge_fetched(repository, node)
            was_fetched = True
            repository.rdp.load(to_load, node.instance)
            node.state = UP
            last = _merge3._last_merge.pop(node.uuid, None) if was_fetched else None
            if last is not None:
                _merge3.remember_merged(node.uuid, last[0], last[1], contributed)
            else:
                _merge3.remember(node.uuid, node.data)
            if contributed and _merge3.PUBLISH_MERGES:  # patched: LWW needs no republish
                try:
                    repository.assert_modification_rights(node.uuid)
                    commit(repository, node.uuid)
                    push(repository, "origin", node.uuid)
                except Exception as e:
                    logging.info(f"merge3: could not publish merge of {node.uuid}: {e}")
        except ReferenceError:
            logging.error(f"Apply reference error")
            node.instance = repository.rdp.resolve(node.data)
            traceback.print_exc()


def evaluate_node_dependencies(repository: Repository, node_id: str):
    node = repository.graph.get(node_id)

    assert node
    if not node.instance:
        return

    if node.dependencies:
        logging.debug(f"Clearing {len(node.dependencies)} dependencies.")
        node.dependencies.clear()

    dependencies = repository.rdp.resolve_deps(node.instance)

    logging.debug(f"found dependencies: {dependencies}")
    for dep in dependencies:
        registered_dep = repository.get_node_by_datablock(dep)
        if registered_dep:
            node.add_dependency(registered_dep.uuid)
        else:
            try:
                dep_node_uuid = add(repository, dep, owner=node.owner)
            except UnsupportedTypeError:
                logging.warning(f"Skipping {type(dep)}")
            else:
                node.add_dependency(dep_node_uuid)


def commit(repository: Repository, node_id: str):
    """Commit the given node

    :param uuid: node uuid
    :type uuid: string
    :raise ReferenceError:
    :raise StateError:
    :raise ContextError:
    """

    node = repository.graph.get(node_id)

    if node.state not in [ADDED, UP]:
        logging.warning(f"Commit skipped: data in a wrong state:{repr(node)}")
        return

    evaluate_node_dependencies(repository, node_id)

    # Check for additionnal nodes to commit
    commit_queue = []

    for dep_uuid in repository.graph.rbfs_from(node_id):
        dep = repository.graph.get(dep_uuid)
        if dep.state in [ADDED]:
            commit_queue.append(dep_uuid)
    commit_queue.append(node_id)

    for node_id in commit_queue:
        node = repository.graph.get(node_id)
        impl = repository.rdp.get_implementation(node.instance)
        if node.state == ADDED or (
            repository.rdp.needs_update(node.instance, node.data) and node.state == UP
        ):
            new_version = repository.rdp.dump(node.instance, stamp_uuid=node.uuid)
            new_version = _merge3.stamp_commit(node, new_version)  # patched: per-value stamps

            delta = repository.rdp.compute_delta(node.data, new_version)
            if delta and delta.diff:
                if impl.use_delta and not node.state == ADDED and not _merge3.ENABLED:  # patched: full state carries stamps
                    commit = Commit()
                    commit.node_id = node.uuid
                    commit.deps = node.dependencies
                    commit.delta = delta

                    node.patch(commit)
                    node.last_commit = commit
                else:
                    node.data = new_version
                node.state = COMMITED
                _merge3.after_commit(node.uuid, node.data)  # patched: base is the dumped state
                logging.debug(f"Committed {node.uuid}")
            else:
                logging.debug(f"Nothing to commit on node {node.uuid}")


def remote_add(
    repository: Repository,
    name: str,
    address: str,
    port: int,
    server_password: str = None,
    admin_password: str = None,
    realtime: bool = False,
):
    """Add a new distant server remote to the repository

    :param repository: target repository to add the remote
    :type repository: Repository
    :param name: name of the remote use for operations
    :type name: str
    :param address: remote ip or dns name
    :type address: str
    :param port: remote port
    :type port: int
    """
    rt_remotes = realtime and not [r for r in repository.remotes.values() if r.realtime]
    already_registered = name in repository.remotes.keys()

    if not already_registered and (not rt_remotes or not realtime):
        logging.info(f"Adding remote {name} ({address}:{port})")
        repository.remotes[name] = Remote(
            name=name,
            address=address,
            port=port,
            server_password=server_password,
            admin_password=admin_password,
            realtime=realtime,
        )
        if not rt_remotes:
            logging.info(f"Detecting realtime remote {name}")
            repository.remote = repository.remotes[name]
    else:
        logging.error(f"Remote {name} already existing.")


def push(repository: Repository, remote: str, node_id, force=False):
    """Publish the node and its dependencies(optionnal) on the server repository

    :param repository: target repository to add the remote
    :type repository: Repository
    :param remote: name of the remote use for operations
    :type remote: str
    :param node_id: uuid of the node
    :type node_id: str

    """
    repository.assert_modification_rights(node_id)
    remote = repository.remotes.get(remote)
    node = repository.graph.get(node_id)

    if node.state != COMMITED and not force:
        logging.debug("Nothing to push")
        return

    # Evaluate node to push
    push_queue = []
    for dep in repository.graph.rbfs_from(node_id):
        dep_node = repository.graph.get(dep)
        if dep_node.state in [ADDED, COMMITED] and dep_node not in push_queue:
            push_queue.append(dep_node)

    # push
    for node in push_queue:
        logging.debug(f"Push {node.uuid}")
        if not node.last_commit or force:
            repository.push(repository.remote.data, node)
        elif node.last_commit:
            repository.push(repository.remote.data, node.last_commit)

        node.state = _merge3.state_after_push(node.uuid, UP, FETCHED)  # patched
        node.last_commit = None


def lock(
    repository: Repository,
    node_ids: list,
    new_owner: str = None,
    ignore_warnings: bool = True,
    affect_dependencies: bool = True,
):
    """Lock a node to the local repository user or to the given one

    :param repository: target repository
    :type repository: Repository
    :param uuid: node key
    :type uuid: string
    :param new_owner: new owner id
    :type new_owner: string
    :param ignore_warnings: ignore NonAuthorizedOperationError
    :type ignore_warnings: bool
    :param affect_dependencies: change dependencies owner
    :type affect_dependencies: bool

    """
    new_owner = repository.username if new_owner is None else new_owner
    locked_nodes = node_ids if isinstance(node_ids, list) else [node_ids]
    # find dependencies
    if affect_dependencies:
        for node in node_ids:
            for dependency in repository.graph.rbfs_from(node):
                if dependency not in locked_nodes:
                    try:
                        repository.assert_modification_rights(dependency)
                    except NonAuthorizedOperationError:
                        logging.debug(f"Node {dependency} already locked.")
                        continue
                    else:
                        locked_nodes.append(dependency)

    # Setup the right override command
    right_command = Right(
        owner=repository.username, data={"uuid": locked_nodes, "owner": new_owner}
    )

    repository.push(repository.remote.command, right_command)


def unlock(
    repository: Repository,
    node_ids: list,
    ignore_warnings: bool = True,
    affect_dependencies: bool = True,
):
    """Release a node to COMMON rights

    :param repository: target repository
    :type repository: Repository
    :param uuid: node key
    :type uuid: string
    :param new_owner: new owner id
    :type new_owner: string
    :param ignore_warnings: ignore NonAuthorizedOperationError
    :type ignore_warnings: bool
    :param affect_dependencies: change dependencies owner
    :type affect_dependencies: bool

    """
    unlocked_nodes = node_ids if isinstance(node_ids, list) else [node_ids]
    # find dependencies
    if affect_dependencies:
        for node in node_ids:
            for dependency in repository.graph.rbfs_from(node):
                if dependency not in unlocked_nodes:
                    try:
                        repository.assert_modification_rights(dependency)
                    except NonAuthorizedOperationError:
                        logging.debug(f"Node {dependency} already locked.")
                        continue
                    else:
                        unlocked_nodes.append(dependency)

    # Setup the right override command
    right_command = Right(
        owner=repository.username, data={"uuid": unlocked_nodes, "owner": RP_COMMON}
    )

    repository.push(repository.remote.command, right_command)


def kick(repository: Repository, username: str):
    """Kick a user from the active realtime remote.

    :param repository: target repository
    :type repository: Repository
    :param user: target user to kick
    :type user: str
    """
    if username == repository.username:
        logging.error("You can't kick ypurself")
        return
    if repository.remote.is_admin():
        kick_cmd = Kick(owner=repository.username, data={"user": username})
        repository.push(repository.remote.command, kick_cmd)
    else:
        logging.error("Insufisent rights to kick.")


def rm(repository: Repository, node_id: str, remove_dependencies: bool = True):
    """Unregister for replication the given object.

    :param repository: target repository
    :type repository: Repository
    :param node_id: node node_id
    :type node_id: string
    :param remove_dependencies: remove all dependencies
    :type remove_dependencies: bool (default: True)
    :raise NonAuthorizedOperationError:
    :raise KeyError:
    """
    repository.assert_modification_rights(node_id)

    if repository.graph.get(node_id):
        nodes_to_delete = []

        if remove_dependencies:
            nodes_to_delete.extend(repository.graph.rbfs_from(node_id))

        nodes_to_delete.append(node_id)

        logging.debug(f"Removing node {nodes_to_delete}")
        for node in nodes_to_delete:
            # patched: say when the datablock is gone here, so other clients
            # remove theirs too instead of keeping a copy that no longer syncs
            ref = repository.graph.get(node)
            gone = ref is not None and repository.rdp.resolve(ref.data) is None
            delete_command = Delete(owner="client:deleted" if gone else "client", data=node)
            # remove the key from our store
            repository.push(repository.remote.command, delete_command)
    else:
        raise KeyError("Cannot unregister key")


def update_user_metadata(repository: Repository, metadata_fieds: dict):
    """Update user metadata

    Update local client informations to others (ex: localisation)

    :param repository: target repository
    :type repository: Repository
    :param metadata_fieds: metadata fields to update
    :type metadata_fieds: dict
    """

    user_state_update = UpdateUserMetadata(
        owner=repository.username, data=metadata_fieds
    )

    repository.push(repository.remote.command, user_state_update)


def purge_orphan_nodes(repository: Repository):
    """Remove non-root orphan nodes and their dependencies of the given
    repository

    :param repository: target repository
    :type repository: Repository
    """
    orphan_nodes = repository.get_orphans_nodes()
    for node in orphan_nodes:
        try:
            repository.assert_modification_rights(node)
        except NonAuthorizedOperationError:
            logging.warning(f"Skipping node {node} removal")
        else:
            rm(repository, node, remove_dependencies=False)


def request_session_info(address: str, timeout: int = 100):
    """Retrieve the target session informations in the following format:
    {
      "private": str, is the session protected by a password
      "version": str, server version
    }

    :param address: server address (ip:port)
    :type address: str
    :return: dict
    """
    session_info = None

    host, port = address.rsplit(":", 1)
    command = net.Dealer(host, int(port), identity=uuid4().bytes, role=net.ROLE_COMMAND)
    command.connect(timeout=2.0, retry=False)

    info_request = RequestServerInfo(owner="guest", data="RQ_INFO")

    command.send(info_request.as_raw_chunks())

    command.poll(timeout)
    reply = command.recv()
    if reply is not None:
        session_info_frame = ReplicationObject.from_raw_chunks(reply)
        session_info = session_info_frame.data
    command.close()

    return session_info
