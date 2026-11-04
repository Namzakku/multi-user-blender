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


import bmesh
import bpy
import numpy as np
from replication.exception import ContextError
from replication.protocol import ReplicatedDatablock
from replication.topo3 import SKIN_KEY, check as check_mesh  # patched

from ..utils import get_preferences
from .bl_action import (dump_animation_data, load_animation_data,
                        resolve_animation_dependencies)
from .bl_datablock import resolve_datablock_from_uuid
from .bl_material import dump_materials_slots, load_materials_slots
from .dump_anything import (Dumper, Loader, np_dump_collection,
                            np_dump_collection_primitive, np_load_collection,
                            np_load_collection_primitives)

VERTICE = ['co']

EDGE = [
    'vertices',
    'use_seam',
    'use_edge_sharp',
]
LOOP = [
    'vertex_index',
    'normal',
]

POLYGON = [
    'loop_total',
    'loop_start',
    'use_smooth',
    'material_index',
]

GENERIC_ATTRIBUTES =[
    'crease_vert',
    'crease_edge',
    'bevel_weight_vert',
    'bevel_weight_edge',
    'mu_vid',  # patched: permanent vertex ids for the topology merge (replication/topo3.py)
]

GENERIC_ATTRIBUTES_ENSURE = {
    'crease_vert': 'vertex_crease_ensure',
    'crease_edge': 'edge_crease_ensure'
}


def dump_skin(mesh):
    """patched: the Skin modifier's per-vertex data as float32 rows [radius x,
    radius y, use_loose, use_root], in vertex order, so topo3 can carry it by
    vertex id. The object dump sent it by index, which swapped radii when two
    users added vertices at once."""
    if not mesh.skin_vertices:
        return None
    layer = mesh.skin_vertices[0].data
    n = len(mesh.vertices)
    radius = np.zeros(n * 2, dtype=np.float32)
    loose = np.zeros(n, dtype=bool)
    root = np.zeros(n, dtype=bool)
    if n:
        layer.foreach_get("radius", radius)
        layer.foreach_get("use_loose", loose)
        layer.foreach_get("use_root", root)
    rows = np.column_stack([radius.reshape(-1, 2), loose, root]).astype(np.float32)
    return {"data": rows.tobytes()}


def load_skin(skin, mesh):
    """patched: put dumped skin rows back. clear_geometry() removes the skin
    layer, so it is made again here; without this a Skin modifier added in
    Object Mode lost its data on the next load."""
    rows = np.frombuffer((skin or {}).get("data") or b"", dtype=np.float32).reshape(-1, 4)
    if len(rows) != len(mesh.vertices):
        return
    if not mesh.skin_vertices:
        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.verts.layers.skin.verify()
        bm.to_mesh(mesh)
        bm.free()
    if not len(rows):
        return
    layer = mesh.skin_vertices[0].data
    layer.foreach_set("radius", np.ascontiguousarray(rows[:, :2]).ravel())
    layer.foreach_set("use_loose", rows[:, 2] > 0.5)
    layer.foreach_set("use_root", rows[:, 3] > 0.5)


class BlMesh(ReplicatedDatablock):
    use_delta = True

    bl_id = "meshes"
    bl_class = bpy.types.Mesh
    bl_check_common = False
    bl_icon = 'MESH_DATA'
    bl_reload_parent = True

    @staticmethod
    def construct(data: dict) -> object:
        return bpy.data.meshes.new(data.get("name"))

    @staticmethod
    def load(data: dict, datablock: object):
        if not datablock or datablock.is_editmode:
            raise ContextError
        # patched: refuse an update whose counts do not match its buffers before
        # touching the mesh. Loading one failed halfway with "Array length
        # mismatch" and the half-loaded mesh crashed Blender (2026-11-04).
        problem = check_mesh(data)
        if problem:
            raise ValueError(f"Refused a mesh update for {datablock.name}: {problem}")
        load_animation_data(data.get('animation_data'), datablock)

        loader = Loader()
        loader.load(datablock, data)

        # MATERIAL SLOTS
        src_materials = data.get('materials', None)
        if src_materials:
            load_materials_slots(src_materials, datablock.materials)

        # patched: keep the mesh as it was if the geometry load fails anyway
        backup = bmesh.new()
        backup.from_mesh(datablock)
        try:
            BlMesh._load_geometry(data, datablock)
        except Exception:
            datablock.clear_geometry()
            backup.to_mesh(datablock)
            datablock.update()
            raise
        finally:
            backup.free()

    @staticmethod
    def _load_geometry(data: dict, datablock: object):
        # CLEAR GEOMETRY
        if datablock.vertices:
            datablock.clear_geometry()

        datablock.vertices.add(data["vertex_count"])
        datablock.edges.add(data["egdes_count"])
        datablock.loops.add(data["loop_count"])
        datablock.polygons.add(data["poly_count"])

        # LOADING
        np_load_collection(data['vertices'], datablock.vertices, VERTICE)
        np_load_collection(data['edges'], datablock.edges, EDGE)
        np_load_collection(data['loops'], datablock.loops, LOOP)
        np_load_collection(data["polygons"],datablock.polygons, POLYGON)

        # UV Layers
        if 'uv_layers' in data.keys():
            for layer in data['uv_layers']:
                if layer not in datablock.uv_layers:
                    datablock.uv_layers.new(name=layer)

                np_load_collection_primitives(
                    datablock.uv_layers[layer].data, 
                    'uv',
                    data["uv_layers"][layer]['data'])

        # Vertex color
        if 'vertex_colors' in data.keys():
            for color_layer in data['vertex_colors']:
                if color_layer not in datablock.vertex_colors:
                    datablock.vertex_colors.new(name=color_layer)

                np_load_collection_primitives(
                    datablock.vertex_colors[color_layer].data,
                    'color', 
                    data["vertex_colors"][color_layer]['data'])

        # Generic attibutes
        for attribute_name, attribute_data_type, attribute_domain, attribute_data in data["attributes"]:
            if attribute_name not in datablock.attributes:
                datablock.attributes.new(
                    attribute_name,
                    attribute_data_type,
                    attribute_domain
                )
            np_load_collection(attribute_data, datablock.attributes[attribute_name].data, ['value'])

        if data.get(SKIN_KEY):
            load_skin(data[SKIN_KEY], datablock)

        datablock.validate()
        datablock.update()

    @staticmethod
    def dump(datablock: object) -> dict:
        if (datablock.is_editmode or bpy.context.mode == "SCULPT") and not get_preferences().sync_flags.sync_during_editmode:
            raise ContextError("Mesh is in edit mode")
        mesh = datablock

        dumper = Dumper()
        dumper.depth = 1
        dumper.include_filter = [
            'name',
            'use_auto_smooth',
            'auto_smooth_angle',
            'use_customdata_edge_bevel',
        ]

        data = dumper.dump(mesh)

        data['animation_data'] = dump_animation_data(datablock)

        # VERTICES
        data["vertex_count"] = len(mesh.vertices)
        data["vertices"] = np_dump_collection(mesh.vertices, VERTICE)

        # EDGES
        data["egdes_count"] = len(mesh.edges)
        data["edges"] = np_dump_collection(mesh.edges, EDGE)

        # ATTIBUTES
        data["attributes"] = []
        for attribute_name in GENERIC_ATTRIBUTES:
            if attribute_name in datablock.attributes:
                attribute_data = datablock.attributes.get(attribute_name)
                dumped_attr_data = np_dump_collection(attribute_data.data, ['value'])

                data["attributes"].append(
                    (
                        attribute_name,
                        attribute_data.data_type,
                        attribute_data.domain,
                        dumped_attr_data
                    )
                )
        # POLYGONS
        data["poly_count"] = len(mesh.polygons)
        data["polygons"] = np_dump_collection(mesh.polygons, POLYGON)

        # LOOPS
        data["loop_count"] = len(mesh.loops)
        data["loops"] = np_dump_collection(mesh.loops, LOOP)

        # UV Layers
        if mesh.uv_layers:
            data['uv_layers'] = {}
            for layer in mesh.uv_layers:
                data['uv_layers'][layer.name] = {}
                data['uv_layers'][layer.name]['data'] = np_dump_collection_primitive(layer.data, 'uv')

        # Vertex color
        if mesh.vertex_colors:
            data['vertex_colors'] = {}
            for color_map in mesh.vertex_colors:
                data['vertex_colors'][color_map.name] = {}
                data['vertex_colors'][color_map.name]['data'] = np_dump_collection_primitive(color_map.data, 'color')

        # patched: skin data per vertex, carried by vertex id
        skin = dump_skin(mesh)
        if skin is not None:
            data[SKIN_KEY] = skin

        # Materials
        data['materials'] = dump_materials_slots(datablock.materials)
        return data

    @staticmethod
    def resolve_deps(datablock: object) -> list[object]:
        deps = []

        for material in datablock.materials:
            if material:
                deps.append(material)

        deps.extend(resolve_animation_dependencies(datablock))

        return deps

    @staticmethod
    def resolve(data: dict) -> object:
        uuid = data.get('uuid')
        return resolve_datablock_from_uuid(uuid, bpy.data.meshes)

    @staticmethod
    def needs_update(datablock: object, data: dict) -> bool:
        return ('EDIT' not in bpy.context.mode and bpy.context.mode != 'SCULPT') \
            or get_preferences().sync_flags.sync_during_editmode


_type = bpy.types.Mesh
_class = BlMesh
