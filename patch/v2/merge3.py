"""Concurrent-edit merge for Multi-User / replication (test patch).

v2: last-writer-wins per value. Every node's data carries a stamp tree under
STAMP_KEY that mirrors the data: one time per scalar, one per list element and
one float64 per 4-byte word of a numpy buffer (vertex coordinates, etc.). A
commit stamps exactly the values it changed. When a remote update arrives, each
value is taken from whichever side wrote it last, so a stale value carried
inside someone else's whole-buffer update cannot overwrite a newer one, and
uncommitted local edits survive. Equal stamps fall back to a deterministic
tie-break, so every client converges on the same result.

If element counts differ (vertex_count, poly_count, ...), one whole side is kept
(the one with the newest stamp), so a mesh never mixes two topologies.

Stamps use time.time(). Clients on one machine share a clock; across machines
the clocks must be synchronized (NTP) for "latest" to be meaningful.
"""
import copy
import logging
import time

import numpy as np

ENABLED = True
# Publish a merge that carried local edits not committed yet (see merge_fetched).
PUBLISH_MERGES = True
# Free editing: do not lock objects to whoever selects them, so several users
# can select and edit the same object; concurrent changes go through lww().
FREE_EDIT = True
STAMP_KEY = "_mu_ts"
_MISSING = object()
_base = {}
_carry = {}  # uuid -> (merged data, merged stamps) awaiting the commit that publishes them
# uuid -> (local dump, merged result) pairs, one per incoming update that changed
# local values, newest last. A tool that
# writes back a state it captured earlier (Blender's move tool, undo) restores
# one of these values; such a write is not a new edit.
_superseded = {}
SUPERSEDED_KEEP = 64


# ---------------------------------------------------------------- helpers
def strip(data):
    if isinstance(data, dict) and STAMP_KEY in data:
        d = dict(data)
        d.pop(STAMP_KEY, None)
        return d
    return data


def stamps_of(data):
    return data.get(STAMP_KEY) if isinstance(data, dict) else None


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
        return bool(np.all(r))
    except Exception:
        return False


def _width(buf):
    return 4 if len(buf) % 4 == 0 else 1


def _words(buf):
    return np.frombuffer(buf, dtype=np.uint32 if _width(buf) == 4 else np.uint8)


def _tmax(ts):
    if isinstance(ts, dict):
        return max([_tmax(v) for v in ts.values()] or [0.0])
    if isinstance(ts, list):
        return max([_tmax(v) for v in ts] or [0.0])
    if isinstance(ts, (bytes, bytearray)):
        a = np.frombuffer(ts, dtype=np.float64)
        return float(a.max()) if a.size else 0.0
    try:
        return float(ts)
    except Exception:
        return 0.0


def _child(ts, key):
    """Stamp sub-tree for a child, or a uniform scalar stamp."""
    if isinstance(ts, dict):
        return ts.get(key)
    if isinstance(ts, (int, float)):
        return float(ts)
    return None


# ---------------------------------------------------------------- stamping
def stamp(prev, prev_ts, new, now):
    """Stamp tree for `new`: unchanged values keep prev_ts, changed ones get now."""
    if isinstance(new, dict):
        pd = prev if isinstance(prev, dict) else {}
        return {k: stamp(pd.get(k, _MISSING), _child(prev_ts, k) if pd else None, v, now)
                for k, v in new.items() if k != STAMP_KEY}
    if isinstance(new, (list, tuple)):
        same = isinstance(prev, (list, tuple)) and len(prev) == len(new)
        out = []
        for i, v in enumerate(new):
            if same:
                pt = prev_ts[i] if isinstance(prev_ts, list) and len(prev_ts) == len(new) else (
                    float(prev_ts) if isinstance(prev_ts, (int, float)) else None)
                out.append(stamp(prev[i], pt, v, now))
            else:
                out.append(stamp(_MISSING, None, v, now))
        return out
    if isinstance(new, (bytes, bytearray)):
        n = len(new) // _width(new)
        if isinstance(prev, (bytes, bytearray)) and len(prev) == len(new):
            if isinstance(prev_ts, (bytes, bytearray)) and len(prev_ts) == n * 8:
                ts = np.frombuffer(prev_ts, dtype=np.float64).copy()
            else:
                ts = np.full(n, float(prev_ts) if isinstance(prev_ts, (int, float)) else 0.0)
            ts[_words(prev) != _words(new)] = now
            return ts.tobytes()
        return np.full(n, now, dtype=np.float64).tobytes()
    if prev is _MISSING or not _eq(prev, new):
        return now
    if isinstance(prev_ts, (int, float)):
        return float(prev_ts)
    return _tmax(prev_ts) if prev_ts is not None else 0.0


def unstale(base, new, olds):
    """Return (new', changed). `olds` holds (before, after) pairs, the local state
    before an incoming update and what that update loaded. A value of `new` that
    differs from `base` but equals a `before` value the update replaced is a stale
    state written back by a tool (move tool, undo), not an edit made here, and is
    put back to its `base` value. Values the update did not touch never count, so
    a user's own undo of their own edit still goes through."""
    if not olds:
        return new, False
    if isinstance(new, dict):
        if not isinstance(base, dict):
            return new, False
        out = None
        for k, v in new.items():
            if k == STAMP_KEY or k not in base:
                continue
            sub = [(b.get(k, _MISSING) if isinstance(b, dict) else _MISSING,
                    a.get(k, _MISSING) if isinstance(a, dict) else _MISSING) for b, a in olds]
            nv, changed = unstale(base[k], v, sub)
            if changed:
                out = dict(new) if out is None else out
                out[k] = nv
        return (new, False) if out is None else (out, True)
    if isinstance(new, (list, tuple)):
        if not isinstance(base, (list, tuple)) or len(base) != len(new):
            return new, False
        n = len(new)
        pick = lambda x, i: x[i] if isinstance(x, (list, tuple)) and len(x) == n else _MISSING
        out = None
        for i, v in enumerate(new):
            nv, changed = unstale(base[i], v, [(pick(b, i), pick(a, i)) for b, a in olds])
            if changed:
                out = list(new) if out is None else out
                out[i] = nv
        if out is None:
            return new, False
        return (tuple(out) if isinstance(new, tuple) else out), True
    if isinstance(new, (bytes, bytearray)):
        if not isinstance(base, (bytes, bytearray)) or len(base) != len(new):
            return new, False
        nw, bw = _words(new), _words(base)
        differs = nw != bw
        if not differs.any():
            return new, False
        mask = np.zeros(len(nw), dtype=bool)
        for b, a in olds:
            if (isinstance(b, (bytes, bytearray)) and isinstance(a, (bytes, bytearray))
                    and len(b) == len(new) == len(a)):
                bwo, awo = _words(b), _words(a)
                mask |= (bwo == nw) & (bwo != awo)
        mask &= differs
        if not mask.any():
            return new, False
        out = nw.copy()
        out[mask] = bw[mask]
        return out.tobytes(), True
    if _eq(base, new):
        return new, False
    for b, a in olds:
        if b is not _MISSING and a is not _MISSING and _eq(b, new) and not _eq(b, a):
            return base, True
    return new, False


def note_superseded(uuid, before, after):
    olds = _superseded.setdefault(uuid, [])
    olds.append((before, after))
    del olds[:-SUPERSEDED_KEEP]


# ---------------------------------------------------------------- merging
def _pick(a, ats, b, bts):
    ta, tb = _tmax(ats), _tmax(bts)
    if ta > tb:
        return a, ats
    if tb > ta:
        return b, bts
    try:
        return (a, ats) if repr(a) >= repr(b) else (b, bts)
    except Exception:
        return a, ats


def lww(a, ats, b, bts):
    """Merge two versions value by value; the later stamp wins. Returns (value, stamps)."""
    if isinstance(a, dict) and isinstance(b, dict):
        counts = [k for k in a if k.endswith("_count") and k in b]
        if counts and any(not _eq(a[k], b[k]) for k in counts):
            return _pick(a, ats, b, bts)  # topology differs: keep one whole side
        out, ot = {}, {}
        for k in list(a.keys()) + [k for k in b.keys() if k not in a]:
            if k == STAMP_KEY:
                continue
            if k in a and k in b:
                out[k], ot[k] = lww(a[k], _child(ats, k), b[k], _child(bts, k))
            elif k in a:
                out[k], ot[k] = a[k], _child(ats, k)
            else:
                out[k], ot[k] = b[k], _child(bts, k)
        return out, ot
    if (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and len(a) == len(b)
            and isinstance(ats, list) and isinstance(bts, list) and len(ats) == len(bts) == len(a)):
        pairs = [lww(x, xt, y, yt) for x, xt, y, yt in zip(a, ats, b, bts)]
        return type(a)(p[0] for p in pairs), [p[1] for p in pairs]
    if (isinstance(a, (bytes, bytearray)) and isinstance(b, (bytes, bytearray)) and len(a) == len(b)):
        n = len(a) // _width(a)
        ta = np.frombuffer(ats, dtype=np.float64) if isinstance(ats, (bytes, bytearray)) and len(ats) == n * 8 \
            else np.full(n, _tmax(ats))
        tb = np.frombuffer(bts, dtype=np.float64) if isinstance(bts, (bytes, bytearray)) and len(bts) == n * 8 \
            else np.full(n, _tmax(bts))
        wa, wb = _words(a), _words(b)
        take_b = (tb > ta) | ((tb == ta) & (wb > wa))
        return np.where(take_b, wb, wa).tobytes(), np.maximum(ta, tb).tobytes()
    if _eq(a, b):
        return a, (ats if _tmax(ats) >= _tmax(bts) else bts)
    return _pick(a, ats, b, bts)


# ---------------------------------------------------------------- hooks
def stamp_commit(node, new_version):
    """Called by commit(). Stamp the fresh dump against the state the datablock
    last held (its base), not against node.data: node.data may already hold a
    remote update that is not applied yet, and stamping against it would make
    the stale local copy of that value look like a new local edit."""
    if not ENABLED or not isinstance(new_version, dict):
        return new_version
    carried = _carry.pop(node.uuid, None)
    if carried is not None:
        prev, prev_ts = carried
    else:
        old = _base.get(node.uuid)
        if old is None:
            old = node.data if isinstance(node.data, dict) else None
        prev = strip(old) if old else _MISSING
        prev_ts = stamps_of(old) if old else None
    if prev is not _MISSING:
        fixed, changed = unstale(prev, strip(new_version), _superseded.get(node.uuid))
        if changed:
            logging.info(f"merge3: ignored a stale restore in {node.uuid}")
            new_version = dict(fixed)
            _reapply.add(node.uuid)  # the datablock still holds the stale values
    new_version[STAMP_KEY] = stamp(prev, prev_ts, strip(new_version), time.time())
    _commit_base[node.uuid] = copy.deepcopy(new_version)
    # keep remote values that arrived but are not applied yet
    stored = node.data if isinstance(node.data, dict) else None
    if stored is not None and stamps_of(stored) is not None:
        merged = merge_incoming(stored, new_version)
        if not _eq(strip(merged), strip(new_version)):
            _reapply.add(node.uuid)
        return merged
    return new_version


def after_commit(uuid, data):
    """Base after a commit is what the datablock holds, not the merged node.data."""
    remember(uuid, _commit_base.pop(uuid, data))


def state_after_push(uuid, up, fetched):
    """A commit that kept newer remote values must still apply them locally."""
    if uuid in _reapply:
        _reapply.discard(uuid)
        return fetched
    return up


_commit_base = {}
_reapply = set()


def remember_merged(uuid, merged, merged_ts, contributed):
    """After apply: the merged state is the new base; queue its stamps for publishing."""
    data = dict(merged)
    data[STAMP_KEY] = merged_ts
    remember(uuid, data)
    if contributed:
        _carry[uuid] = (merged, merged_ts)


def merge_fetched(repository, node):
    """Called by apply() for a fetched node. Returns (data_to_load, local_contributed)."""
    remote = node.data
    if not ENABLED or node.instance is None or not isinstance(remote, dict):
        return strip(remote), False
    base = _base.get(node.uuid)
    if base is None:
        return strip(remote), False
    try:
        local = repository.rdp.dump(node.instance, stamp_uuid=node.uuid)
    except Exception as e:  # e.g. mesh in edit mode: let apply raise and retry later
        logging.debug(f"merge3: dump failed for {node.uuid}: {e}")
        return strip(remote), False
    dumped = local
    local, _ = unstale(strip(base), local, _superseded.get(node.uuid))
    now = time.time()
    lts = stamp(strip(base), stamps_of(base), local, now)
    rts = stamps_of(remote)
    if rts is None:  # sender without stamps: treat all its values as just written
        rts = stamp(_MISSING, None, strip(remote), now)
    merged, mts = lww(local, lts, strip(remote), rts)
    if not _eq(merged, dumped):
        note_superseded(node.uuid, dumped, merged)
    kept = not _eq(merged, strip(remote))
    if kept:
        logging.info(f"merge3: kept newer local values in {node.uuid}")
    # publish only edits made here and not committed yet (an incoming update can
    # mark the node FETCHED before the edit's commit runs). Values that are merely
    # newer than a stale remote copy already went out with their author's commit.
    contributed = kept and not _eq(local, strip(base))
    _last_merge[node.uuid] = (merged, mts, contributed)
    return merged, contributed


_last_merge = {}


def merge_incoming(old, new):
    """Called when an update is stored (client and server): combine the stored
    data with the incoming data value by value, newest stamp wins, so a second
    update arriving before the first is applied cannot wipe its values."""
    if not ENABLED or not isinstance(old, dict) or not isinstance(new, dict):
        return new
    ots, nts = stamps_of(old), stamps_of(new)
    if ots is None or nts is None:
        return new
    try:
        merged, mts = lww(strip(old), ots, strip(new), nts)
    except Exception as e:
        logging.warning(f"merge3: incoming merge failed, keeping update as is: {e}")
        return new
    merged[STAMP_KEY] = mts
    return merged
