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
import sys
from pathlib import Path, WindowsPath, PosixPath

import bpy
from replication.protocol import ReplicatedDatablock

from .. import utils
from ..utils import get_preferences


def get_filepath(filename):
    """
    Construct the local filepath
    """
    return str(Path(
        utils.get_preferences().cache_directory,
        filename
    ))


def ensure_unpacked(datablock):
    if datablock.packed_file:
        logging.info(f"Unpacking {datablock.name}")

        filename = Path(bpy.path.abspath(datablock.filepath)).name
        datablock.filepath = get_filepath(filename)

        datablock.unpack(method="WRITE_ORIGINAL")


class BlFile(ReplicatedDatablock):
    bl_id = 'file'
    bl_name = "file"
    bl_class = Path
    bl_check_common = False
    bl_icon = 'FILE'
    bl_reload_parent = True

    @staticmethod
    def construct(data: dict) -> object:
        return Path(get_filepath(data['name']))

    @staticmethod
    def resolve(data: dict) -> object:
        return Path(get_filepath(data['name']))

    @staticmethod
    def dump(datablock: object) -> dict:
        """
        Read the file and return a dict as:
        {
            name : filename
            extension :
            file: file content
        }
        """
        logging.info("Extracting file metadata")

        data = {
            'name': datablock.name,
        }

        # patched: a missing file (an image path into another machine's cache)
        # raised FileNotFoundError from stat() here and ended the session
        try:
            with open(datablock, "rb") as file:
                data['file'] = file.read()
            logging.info(f"Read {datablock.name}: {len(data['file'])} bytes")
        except OSError as e:
            logging.warning(f"Cannot read {datablock} ({e}), sending it without content")

        return data

    @staticmethod
    def load(data: dict, datablock: object):
        """
        Writing the file
        """
        if 'file' not in data:
            logging.warning(f"No content received for {datablock}, skipping")
            return
        try:
            with open(datablock, "wb") as file:
                file.write(data['file'])

            if get_preferences().clear_memory_filecache:
                del data["file"]
        except OSError as e:
            logging.warning(f"Cannot write {datablock} ({e}), skipping")

    @staticmethod
    def resolve_deps(datablock: object) -> list[object]:
        return []

    @staticmethod
    def needs_update(datablock: object, data: dict) -> bool:
        if get_preferences().clear_memory_filecache:
            return False
        else:
            if not datablock:
                return None

            if not data:
                return True

            # patched: a missing file has nothing new to send
            try:
                disk_size = datablock.stat().st_size
            except OSError:
                return False
            if 'file' not in data:
                return True
            memory_size = sys.getsizeof(data['file'])-33

            if memory_size != disk_size:
                return True
            else:
                return False


_type = [WindowsPath, PosixPath]
_class = BlFile
