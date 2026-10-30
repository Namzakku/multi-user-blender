"""Three-way merge for concurrent edits (test patch for Multi-User / replication).

Each node keeps a `base`: its data as last synced with the server. When a remote
update arrives (node.data is now base + remote delta) and the local datablock
also changed since base, the two are merged field by field instead of the
remote state overwriting the local one:

- a field only one side changed keeps that change
- dicts merge per key, equal-length lists/tuples per element
- equal-length numpy byte buffers (vertex coordinates, etc.) merge per 4-byte
  word, so two users moving different vertices both keep their moves
- a field both sides changed to different values is resolved by a fixed
  tie-break (larger repr / larger word wins), identical on every client, so
  all copies converge
"""
import copy
import logging

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

ENABLED = True
# Free editing: do not lock objects to whoever selects them, so several users
# can select and edit the same object; concurrent changes go through merge().
FREE_EDIT = True
_MISSING = object()
_base = {}


def remember(uuid, data):
    try:
        _base[uuid] = copy.deepcopy(data)
    except Exception as e:  # never break sync because of the merge cache
        logging.debug(f"merge3: cannot snapshot {uuid}: {e}")
        _base.pop(uuid, None)


def _eq(a, b):
    if a is b:
        return True
    if type(a) is not type(b):
        return False
    try:
        r = a == b
        if isinstance(r, bool):
            return r
        return bool(np.all(r)) if np is not None else False
    except Exception:
        return False


def _tie(r, l):
    try:
        return r if repr(r) >= repr(l) else l
    except Exception:
        return r


def _tie_whole(r, l):
    """Pick one whole side by a deterministic key every client computes alike."""
    try:
        kr = sorted((k, repr(r[k])) for k in r if k.endswith("_count"))
        kl = sorted((k, repr(l[k])) for k in l if k.endswith("_count"))
        return r if kr >= kl else l
    except Exception:
        return r


def _merge_bytes(b, r, l):
    width = 4 if len(b) % 4 == 0 else 1
    dt = np.uint32 if width == 4 else np.uint8
    ab, ar, al = (np.frombuffer(x, dtype=dt) for x in (b, r, l))
    local_changed = al != ab
    remote_changed = ar != ab
    out = np.where(local_changed, al, ar)
    both = local_changed & remote_changed & (al != ar)
    if both.any():
        out = np.where(both, np.maximum(al, ar), out)
    return out.tobytes()


def merge(b, r, l):
    if _eq(l, b):
        return r
    if _eq(r, b):
        return l
    if _eq(r, l):
        return r
    if isinstance(r, dict) and isinstance(l, dict) and isinstance(b, dict):
        # topology guard: if both sides changed the element counts of a mesh
        # (or any datablock exposing *_count keys), keep one whole side so
        # vertices, edges and faces always come from the same version
        counts = [k for k in r if k.endswith("_count") and k in l]
        if counts and any(not _eq(r[k], l[k]) for k in counts):
            return _tie_whole(r, l)
        out = {}
        for k in list(r.keys()) + [k for k in l.keys() if k not in r]:
            v = merge(b.get(k, _MISSING), r.get(k, _MISSING), l.get(k, _MISSING))
            if v is not _MISSING:
                out[k] = v
        return out
    if (isinstance(r, (list, tuple)) and isinstance(l, (list, tuple)) and isinstance(b, (list, tuple))
            and len(r) == len(l) == len(b)):
        return type(r)(merge(x, y, z) for x, y, z in zip(b, r, l))
    if (np is not None and isinstance(r, bytes) and isinstance(l, bytes) and isinstance(b, bytes)
            and len(r) == len(l) == len(b)):
        return _merge_bytes(b, r, l)
    return _tie(r, l)


def merge_fetched(repository, node):
    """Return (data_to_load, local_contributed)."""
    if not ENABLED or node.instance is None:
        return node.data, False
    base = _base.get(node.uuid)
    if base is None:
        return node.data, False
    try:
        local = repository.rdp.dump(node.instance, stamp_uuid=node.uuid)
    except Exception as e:
        logging.debug(f"merge3: dump failed for {node.uuid}: {e}")
        return node.data, False
    if _eq(local, base):
        return node.data, False
    merged = merge(base, node.data, local)
    contributed = not _eq(merged, node.data)
    if contributed:
        logging.info(f"merge3: merged local edits into {node.uuid}")
    return merged, contributed
