"""Select a Linux link without changing system routes, Wi-Fi association or DNS."""
import contextlib
import copy
import ipaddress
from pathlib import Path
import re
import select
import socket
import socketserver
import threading
from .config import peer
from .runner import Failure


def interfaces():
    result=[]
    for path in sorted(Path("/sys/class/net").iterdir()):
        try:
            if (path/"type").read_text().strip()!="1" or not re.fullmatch(r"[\w.-]{1,15}",path.name):
                continue
            wifi=(path/"wireless").exists() or (path/"phy80211").exists()
            result.append(dict(name=path.name,kind="Wi-Fi" if wifi else "Ethernet",up=(path/"operstate").read_text().strip()=="up"))
        except OSError:
            continue
    return result


def require_link(name):
    if not any(x["name"]==name and x["up"] for x in interfaces()):
        raise Failure("interface_unavailable")


def connect(ip, port, iface):
    family=socket.AF_INET6 if ipaddress.ip_address(ip).version==6 else socket.AF_INET
    sock=socket.socket(family,socket.SOCK_STREAM)
    try:
        sock.settimeout(8)
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,iface.encode()+b"\0")
        sock.connect((ip,port))
        return sock
    except BaseException:
        sock.close()
        raise


@contextlib.contextmanager
def tunnel(out,iface):
    if not iface:
        yield out
        return
    require_link(iface)
    # Only the peer TCP socket crosses the selected link. Xray keeps TLS/SNI,
    # WS, Reality and authentication, with a loopback destination it cannot bypass.
    bound=copy.deepcopy(out)
    dst=peer(bound)
    ip,port=dst["address"],dst["port"]
    if "h3" in bound.get("streamSettings",{}).get("tlsSettings",{}).get("alpn",[]):
        raise Failure("udp_transport_unsupported")
    stopping=threading.Event()

    class Relay(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                with connect(ip,port,iface) as upstream:
                    sockets=[self.request,upstream]
                    self.request.settimeout(8)
                    while sockets and not stopping.is_set():
                        ready,_,_=select.select(sockets,[],[],.25)
                        for src in ready:
                            target=upstream if src is self.request else self.request
                            data=src.recv(65536)
                            if data:
                                target.sendall(data)
                            else:
                                sockets.remove(src)
                                target.shutdown(socket.SHUT_WR)
            except OSError:
                return

    with socketserver.ThreadingTCPServer(("127.0.0.1",0),Relay) as server:
        thread=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":.1})
        thread.start()
        try:
            dst.update(address="127.0.0.1",port=server.server_address[1])
            yield bound
        finally:
            stopping.set()
            server.shutdown()
            thread.join()
