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


import time
from .constants import ADDED, COMMITED, PUSHED, FETCHED, UP, MODIFIED, ERROR


def current_milli_time():
    """Retrieve current time in millisecond"""
    return int(round(time.time() * 1000))


def get_state_str(state):
    state_str = "UNKOWN"
    if state == ADDED:
        state_str = "ADDED"
    elif state == COMMITED:
        state_str = "COMMITED"
    elif state == PUSHED:
        state_str = "PUSHED"
    elif state == FETCHED:
        state_str = "FETCHED"
    elif state == UP:
        state_str = "UP"
    elif state == MODIFIED:
        state_str = "MODIFIED"
    elif state == ERROR:
        state_str = "ERROR"
    return state_str
