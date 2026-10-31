"""Unit tests for topomerge. Run with: python -m unittest -v (no Blender needed)."""

import copy
import unittest

from topomerge import (diff, face_key, make_allocator, merge, repair_ids, validate)


def cube():
    verts = {i + 1: ((i & 1) * 1.0, (i >> 1 & 1) * 1.0, (i >> 2 & 1) * 1.0) for i in range(8)}
    faces = [(1, 3, 4, 2), (5, 6, 8, 7), (1, 2, 6, 5), (3, 7, 8, 4), (1, 5, 7, 3), (2, 4, 8, 6)]
    return {"verts": verts, "faces": faces, "edges": []}


def grid(n=4):
    """n x n quads, vertex id = 1 + x + y * (n + 1)."""
    vid = lambda x, y: 1 + x + y * (n + 1)
    verts = {vid(x, y): (float(x), float(y), 0.0) for y in range(n + 1) for x in range(n + 1)}
    faces = [(vid(x, y), vid(x + 1, y), vid(x + 1, y + 1), vid(x, y + 1))
             for y in range(n) for x in range(n)]
    return {"verts": verts, "faces": faces, "edges": []}


def quad_at(mesh, x, y, n=4):
    vid = lambda x, y: 1 + x + y * (n + 1)
    return face_key((vid(x, y), vid(x + 1, y), vid(x + 1, y + 1), vid(x, y + 1)))


def split_quad(mesh, key, new_id):
    """Replace a quad by four triangles around a new center vertex."""
    out = copy.deepcopy(mesh)
    out["faces"] = [f for f in out["faces"] if face_key(f) != key]
    pts = [mesh["verts"][v] for v in key]
    out["verts"][new_id] = tuple(sum(c) / 4 for c in zip(*pts))
    for i in range(4):
        out["faces"].append((key[i], key[(i + 1) % 4], new_id))
    return out


def split_quad_diagonal(mesh, key):
    out = copy.deepcopy(mesh)
    out["faces"] = [f for f in out["faces"] if face_key(f) != key]
    out["faces"] += [(key[0], key[1], key[2]), (key[0], key[2], key[3])]
    return out


def extrude_quad(mesh, key, first_id, height=1.0):
    """Extrude a quad: new top cap plus four side walls."""
    out = copy.deepcopy(mesh)
    out["faces"] = [f for f in out["faces"] if face_key(f) != key]
    top = []
    for i, v in enumerate(key):
        x, y, z = mesh["verts"][v]
        out["verts"][first_id + i] = (x, y, z + height)
        top.append(first_id + i)
    out["faces"].append(tuple(top))
    for i in range(4):
        j = (i + 1) % 4
        out["faces"].append((key[i], key[j], top[j], top[i]))
    return out


class FaceKeyTests(unittest.TestCase):
    def test_rotation_and_winding(self):
        self.assertEqual(face_key((3, 1, 2)), (1, 2, 3))
        self.assertEqual(face_key((2, 3, 1)), (1, 2, 3))
        self.assertEqual(face_key((1, 3, 2)), (1, 3, 2))
        self.assertNotEqual(face_key((1, 3, 2)), face_key((1, 2, 3)))


class AllocatorTests(unittest.TestCase):
    def test_values_and_uniqueness(self):
        a, b = make_allocator(1), make_allocator(2)
        va = [a() for _ in range(100)]
        vb = [b() for _ in range(100)]
        self.assertEqual(va[0], (1 << 24) | 1)
        self.assertEqual(vb[0], (2 << 24) | 1)
        self.assertEqual(len(set(va) | set(vb)), 200)

    def test_rejects_bad_client(self):
        with self.assertRaises(ValueError):
            make_allocator(0)
        with self.assertRaises(ValueError):
            make_allocator(128)


class RepairTests(unittest.TestCase):
    def test_extruded_cube(self):
        base = cube()
        # Extruding the top face (ids 5..8, z=1) as Blender does: the 4 new top
        # vertices copy the ids of the ring they came from and sit at z=1.5.
        ids = list(range(1, 9)) + [5, 6, 7, 8]
        coords = [base["verts"][i] for i in range(1, 9)]
        coords += [(x, y, 1.5) for (x, y, _) in (base["verts"][i] for i in (5, 6, 7, 8))]
        out = repair_ids(ids, coords, base["verts"], make_allocator(3))
        self.assertEqual(out[:8], list(range(1, 9)))
        self.assertEqual(len(set(out)), 12)
        self.assertTrue(all(v >> 24 == 3 for v in out[8:]))

    def test_original_moved_copy_left_behind(self):
        # The copy at index 0 is nearer the base position, so it keeps the id.
        base = {"verts": {7: (0.0, 0.0, 0.0)}, "faces": [], "edges": []}
        out = repair_ids([7, 7], [(0.0, 0.0, 0.1), (0.0, 0.0, 2.0)], base["verts"], make_allocator(1))
        self.assertEqual(out[0], 7)
        self.assertNotEqual(out[1], 7)

    def test_zero_and_negative_allocated(self):
        out = repair_ids([0, 5, -1], [(0, 0, 0)] * 3, {5: (0, 0, 0)}, make_allocator(1))
        self.assertEqual(out[1], 5)
        self.assertEqual(len(set(out)), 3)
        self.assertTrue(out[0] > 0 and out[2] > 0)

    def test_skips_ids_in_use(self):
        taken = (1 << 24) | 1
        out = repair_ids([taken, 2, 2], [(0, 0, 0), (0, 0, 0), (1, 0, 0)],
                         {2: (0, 0, 0)}, make_allocator(1))
        self.assertEqual(out[0], taken)
        self.assertEqual(out[1], 2)
        self.assertEqual(out[2], (1 << 24) | 2)


class MergeTests(unittest.TestCase):
    def assertValid(self, mesh):
        errors = [p for p in validate(mesh) if not p.startswith("warning:")]
        self.assertEqual(errors, [])

    def test_disjoint_topology_edits(self):
        base = grid()
        a = split_quad(base, quad_at(base, 0, 0), (1 << 24) | 1)
        b = extrude_quad(base, quad_at(base, 3, 3), (2 << 24) | 1)
        merged, report = merge(base, a, b, 1, 2)
        self.assertValid(merged)
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(len(merged["verts"]), 25 + 1 + 4)
        self.assertEqual(len(merged["faces"]), 16 - 1 + 4 - 1 + 5)
        self.assertEqual(validate(merged), [])

    def test_same_quad_split_differently(self):
        base = grid()
        target = quad_at(base, 1, 1)
        a = split_quad(base, target, (1 << 24) | 1)
        a = extrude_quad(a, quad_at(base, 3, 3), (1 << 24) | 10)   # far edit, must survive
        b = split_quad_diagonal(base, target)
        merged, report = merge(base, a, b, 1, 2)
        self.assertValid(merged)
        self.assertEqual(len(report["conflicts"]), 1)
        c = report["conflicts"][0]
        self.assertEqual(c["winner"], "b")
        self.assertEqual(c["dropped_added_faces"], 4)
        # b's diagonal split is there, a's centre vertex is not, a's far extrude is.
        self.assertIn(face_key((target[0], target[1], target[2])), merged["faces"])
        self.assertNotIn((1 << 24) | 1, merged["verts"])
        for i in range(4):
            self.assertIn((1 << 24) | (10 + i), merged["verts"])
        self.assertEqual(validate(merged), [])

    def test_same_quad_older_side_wins_when_stamped_later(self):
        base = grid()
        target = quad_at(base, 1, 1)
        a = split_quad(base, target, (1 << 24) | 1)
        b = split_quad_diagonal(base, target)
        merged, report = merge(base, a, b, 5, 2)
        self.assertEqual(report["conflicts"][0]["winner"], "a")
        self.assertIn((1 << 24) | 1, merged["verts"])

    def test_delete_vs_move(self):
        base = grid()
        victim = 13   # centre vertex, used by 4 quads
        a = copy.deepcopy(base)
        del a["verts"][victim]
        a["faces"] = [f for f in a["faces"] if victim not in f]
        b = copy.deepcopy(base)
        b["verts"][victim] = (2.0, 2.0, 0.5)
        merged, report = merge(base, a, b, 1, 2)
        self.assertNotIn(victim, merged["verts"])
        self.assertFalse(any(victim in f for f in merged["faces"]))
        self.assertEqual(len(merged["faces"]), 12)
        self.assertValid(merged)

    def test_move_vs_move(self):
        base = grid()
        a = copy.deepcopy(base)
        b = copy.deepcopy(base)
        a["verts"][7] = (1.0, 1.0, 0.3)
        b["verts"][7] = (1.0, 1.0, 0.6)
        a["verts"][1] = (0.0, 0.0, 0.1)
        b["verts"][25] = (4.0, 4.0, 0.2)
        merged, report = merge(base, a, b, 1, 2)
        self.assertEqual(merged["verts"][7], (1.0, 1.0, 0.6))
        self.assertEqual(merged["verts"][1], (0.0, 0.0, 0.1))
        self.assertEqual(merged["verts"][25], (4.0, 4.0, 0.2))
        self.assertEqual(report["conflicts"], [])
        merged, _ = merge(base, a, b, 3, 2)
        self.assertEqual(merged["verts"][7], (1.0, 1.0, 0.3))

    def test_argument_symmetry(self):
        base = grid()
        target = quad_at(base, 1, 1)
        a = split_quad(base, target, (1 << 24) | 1)
        a["verts"][25] = (4.0, 4.0, 1.0)
        b = extrude_quad(split_quad_diagonal(base, target), quad_at(base, 3, 0), (2 << 24) | 1)
        b["verts"][25] = (4.0, 4.0, 2.0)
        m1, _ = merge(base, a, b, 1, 2)
        m2, _ = merge(base, b, a, 2, 1)
        self.assertEqual(m1, m2)

    def test_floating_geometry_kept(self):
        base = grid()
        a = copy.deepcopy(base)
        b = copy.deepcopy(base)
        a["verts"].update({101: (9, 0, 0), 102: (9, 1, 0), 103: (9, 1, 1)})
        a["faces"].append((101, 102, 103))
        b["verts"].update({201: (-9, 0, 0), 202: (-9, 1, 0)})
        b["edges"].append((201, 202))
        merged, report = merge(base, a, b, 1, 2)
        self.assertIn(face_key((101, 102, 103)), merged["faces"])
        self.assertIn((201, 202), merged["edges"])
        self.assertEqual(report["conflicts"], [])
        self.assertValid(merged)

    def test_edge_only_guide(self):
        verts = {i: (float(i), 0.0, 0.0) for i in range(1, 6)}
        base = {"verts": verts, "faces": [], "edges": [(1, 2), (2, 3), (3, 4), (4, 5)]}
        a = copy.deepcopy(base)
        a["verts"][101] = (0.0, 0.0, 0.0)
        a["edges"].append((101, 1))
        b = copy.deepcopy(base)
        b["verts"][201] = (6.0, 0.0, 0.0)
        b["edges"].append((5, 201))
        merged, report = merge(base, a, b, 1, 2)
        self.assertEqual(len(merged["edges"]), 6)
        self.assertEqual(len(merged["verts"]), 7)
        self.assertEqual(report["conflicts"], [])

    def test_same_new_id_from_both_sides_raises(self):
        base = grid()
        a = split_quad(base, quad_at(base, 0, 0), 999)
        b = split_quad(base, quad_at(base, 3, 3), 999)
        with self.assertRaises(ValueError):
            merge(base, a, b, 1, 2)

    def test_no_change(self):
        base = grid()
        merged, report = merge(base, copy.deepcopy(base), copy.deepcopy(base), 1, 2)
        self.assertEqual(merged["faces"], sorted(face_key(f) for f in base["faces"]))
        self.assertEqual(report["conflicts"], [])

    def test_diff_counts(self):
        base = grid()
        a = extrude_quad(base, quad_at(base, 2, 2), 500)
        d = diff(base, a)
        self.assertEqual(len(d.added_verts), 4)
        self.assertEqual(len(d.added_faces), 5)
        self.assertEqual(len(d.removed_faces), 1)
        self.assertEqual(d.removed_verts, set())


if __name__ == "__main__":
    unittest.main()
