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

import os
from pathlib import Path

# Constant values

# Datablock
ADDED = 0
COMMITED = 1
PUSHED = 2
FETCHED = 3
UP = 4
MODIFIED = 5
ERROR = 6


# networking
STATE_INITIAL = 0
CONNECTING = 9
STATE_WAITING = 6
STATE_CONFIG = 4
STATE_SYNCING = 1
STATE_SRV_SYNC = 7
STATE_ACTIVE = 2
STATE_QUITTING = 8
STATE_ERROR = 3
STATE_AUTH = 5
STATE_LOBBY = 10
# Client authed on the command channel but its data channel hasn't
# registered with the server yet - excluded from ServerData's broadcast
# until it flips to STATE_LOBBY, so a live update can't silently drop for a
# client that isn't reachable on the data channel yet.
STATE_DATA_PENDING = 11

# rights
RP_COMMON = "COMMON"

# Differential methodology
DIFF_BINARY = 0
DIFF_JSON = 1

# Default values
CONNECTION_TIMEOUT = 2000
CLIENT_PING_FREQUENCY = 1000

HEAD = "HEAD"

ROOT_PATH = Path(os.path.dirname(os.path.realpath(__file__)))
TTL_SCRIPT_PATH = os.path.join(ROOT_PATH, "ttl.py")
SERVER_SCRIPT_PATH = os.path.join(ROOT_PATH, "server.py")
