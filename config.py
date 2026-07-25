"""Runtime configuration for server.py / client.py.

Reads config.ini (same directory as this file) if present, layered over
built-in defaults. The defaults exactly reproduce Basic Level behavior
(single-threaded server, FIXED data mode) so the app runs unmodified if
config.ini is missing or a key is left out — config.ini only lets a user
*opt into* Advanced Level techniques (concurrency, Active/Passive mode)
without touching code.
"""

import configparser
import os

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")

_DEFAULTS = {
    "server": {
        "host": "0.0.0.0",
        "threading": "single",       # single | thread
        "storage_root": "server_storage",
        "advertise_ip": "",          # override the IP announced in PASV replies (e.g. a cloud VM's public IP behind NAT); blank = auto-detect
        "passive_port_min": "",      # restrict ACTIVE/PASSIVE per-session sockets to [min, max] (both required together); blank = any OS-assigned ephemeral port
        "passive_port_max": "",
    },
    "client": {
        "data_mode": "fixed",        # fixed | active | passive
        "download_dir": "client_downloads",
    },
}


def load():
    parser = configparser.ConfigParser()
    parser.read_dict(_DEFAULTS)
    if os.path.isfile(_CONFIG_PATH):
        parser.read(_CONFIG_PATH)
    return parser


CONFIG = load()
