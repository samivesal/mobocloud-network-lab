"""Read only the interface explicitly selected by the operator."""
import os
from pathlib import Path
import re


def interface_stats(name=None):
    name=name or os.getenv("LAB_INTERFACE","")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}",name) or name in (".",".."):
        return {}
    root=Path("/sys/class/net")/name
    result={"interface":name}
    for key,file in [("state","operstate"),("rx_bytes","statistics/rx_bytes"),("tx_bytes","statistics/tx_bytes"),("link_mbps","speed")]:
        try:
            value=(root/file).read_text().strip()
            if key=="state":
                result[key]=value
            elif int(value)>=0:
                result[key]=int(value)
        except (OSError,ValueError):
            continue
    return result
