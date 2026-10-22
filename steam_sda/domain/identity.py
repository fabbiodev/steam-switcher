"""Steam account identity, independent of Windows and Qt."""
import re
import time
from datetime import datetime

STEAM_ID_BASE = 76561197960265728

def valid_id(value):
    return isinstance(value, str) and value.isascii() and value.isdigit() and 0 < int(value) - STEAM_ID_BASE <= 0xFFFFFFFF

def identity_from_log(text, process_started, now=None):
    """Only consider authenticated events belonging to the current Steam process."""
    current = None
    now = time.time() if now is None else now
    for line in text.splitlines():
        match = re.match(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] \[(Logged On|Logged Off|Logging On),", line)
        if not match:
            continue
        try:
            stamp = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if not process_started - 5 <= stamp <= now + 60:
            continue
        if match[2] in ("Logged Off", "Logging On"):
            current = None
        if "RecvMsgClientLogOnResponse()" in line and ("'OK'" in line or "processing complete" in line):
            ids = re.findall(r"\[U:1:(\d+)\]", line)
            if ids and 0 < int(ids[-1]) <= 0xFFFFFFFF:
                current = str(STEAM_ID_BASE + int(ids[-1]))
    return current
