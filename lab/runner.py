"""Linux-only owned-child supervision; curl uses a fixed HTTPS destination."""
import contextlib
import copy
import ipaddress
import json
import os
from pathlib import Path
import secrets
from dataclasses import dataclass, field
import signal
import socket
import shutil
import sys
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from .config import peer


class Failure(Exception):
    pass


def owns_listener(pid, port):
    fds = Path(f"/proc/{pid}/fd")
    try:
        sockets = {os.readlink(f) for f in fds.iterdir() if f.is_symlink()}
        for row in Path("/proc/net/tcp").read_text().splitlines()[1:]:
            cols = row.split()
            if cols[1] == f"0100007F:{port:04X}" and cols[3]=="0A" and f"socket:[{cols[9]}]" in sockets:
                return True
    except (FileNotFoundError, ProcessLookupError):
        pass
    return False


@dataclass(frozen=True)
class Endpoint:
    port: int
    password: str = field(repr=False)


def handshake(sock, password):
    with sock.makefile("rb") as reader:
        sock.sendall(b"\x05\x01\x02")
        if reader.read(2)!=b"\x05\x02":
            raise Failure("socks_handshake")
        encoded=password.encode("ascii")
        sock.sendall(b"\x01\x03lab"+bytes([len(encoded)])+encoded)
        if reader.read(2)!=b"\x01\x00":
            raise Failure("socks_auth")


@contextlib.contextmanager
def proxy(out, binary="xray", cancelled=lambda: False):
    engine=shutil.which(binary)
    if not engine:
        raise Failure("engine_missing")
    with tempfile.TemporaryDirectory(prefix="mobo-") as folder:
        path = Path(folder)/"runtime.json"
        for attempt in range(3):
            with socket.socket() as s:
                s.bind(("127.0.0.1",0))
                port = s.getsockname()[1]
                endpoint=Endpoint(port,secrets.token_hex(16))
                cfg = {"log":{"loglevel":"none"},"inbounds":[{"listen":"127.0.0.1","port":port,"protocol":"socks","settings":{"auth":"password","accounts":[{"user":"lab","pass":endpoint.password}],"udp":False}}],"outbounds":[out]}
                path.write_text(json.dumps(cfg))
                path.chmod(0o600)
            try:
                engine_env={key:value for key,value in os.environ.items()
                            if key in {"PATH","LANG","TZ","XRAY_LOCATION_ASSET"}}
                p = subprocess.Popen([sys.executable,str(Path(__file__).with_name("child.py")),str(os.getpid()),engine,"run","-c",str(path)],env=engine_env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            except FileNotFoundError:
                raise Failure("engine_missing") from None
            try:
                deadline = time.monotonic()+5
                while p.poll() is None and time.monotonic()<deadline:
                    if cancelled():
                        raise Failure("cancelled")
                    if owns_listener(p.pid,port):
                        with socket.create_connection(("127.0.0.1",port),timeout=1) as sock:
                            handshake(sock,endpoint.password)
                        yield endpoint
                        if p.poll() is not None or not owns_listener(p.pid,port):
                            raise Failure("engine_exited")
                        return
                    time.sleep(.04)
            finally:
                if p.poll() is None:
                    p.send_signal(signal.SIGCONT)
                    p.terminate()
                    try:
                        p.wait(1)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait()
        raise Failure("engine_start")


def pinned(out, allowed):
    result = copy.deepcopy(out)
    dest = peer(result)
    if dest["address"] not in allowed:
        raise Failure("peer_unapproved")
    try:
        ips = [r[4][0] for r in socket.getaddrinfo(dest["address"],dest["port"],type=socket.SOCK_STREAM)]
    except OSError:
        raise Failure("peer_dns") from None
    if not ips or not all(ipaddress.ip_address(x).is_global for x in ips):
        raise Failure("peer_restricted")
    stream = result.get("streamSettings",{})
    sec = stream.get("security")
    if sec in ("tls","reality"):
        stream[sec+"Settings"].setdefault("serverName",dest["address"])
    net=stream.get("network")
    if net in ("ws","xhttp","grpc"):
        stream.setdefault(net+"Settings",{}).setdefault("authority" if net=="grpc" else "host",dest["address"])
    dest["address"]=ips[0]
    return result


def probe(url, port=None, kind="check", size=0, cap=65536, ca=None, body=False, local_fixture=False, cancelled=lambda: False, iface=None, *, timeout=15, connect_timeout=6):
    u = urlsplit(url)
    if u.scheme!="https" or not u.hostname or u.username or u.password or u.fragment:
        raise Failure("target_invalid")
    args=["curl","-q","--silent","--proto","=https","--max-time",str(timeout),"--connect-timeout",str(connect_timeout),
          "--max-filesize",str(cap),"--proxy",f"socks5h://127.0.0.1:{port.port}" if port else "",
          "--noproxy","" if port else "*","--write-out","%{json}"]
    if iface and not port:
        args+=["--interface","if!"+iface]
    if not local_fixture:
        try:
            addresses=[r[4][0] for r in socket.getaddrinfo(u.hostname,u.port or 443,type=socket.SOCK_STREAM)]
        except OSError:
            return dict(ok=False,error="target_dns",status=0,rx=0,tx=0,total_ms=0,tls_complete_ms=0,ttfb_ms=0,proxy_connect_ms=None,mbps=None)
        if not addresses or not all(ipaddress.ip_address(ip).is_global for ip in addresses):
            raise Failure("target_restricted")
        address=addresses[0]
        if ":" in address:
            address="["+address+"]"
        args+=["--connect-to",f"{u.hostname}:{u.port or 443}:{address}:{u.port or 443}"]
    if ca:
        args+=["--cacert",ca]
    with tempfile.TemporaryFile() as payload, tempfile.NamedTemporaryFile() as response, tempfile.NamedTemporaryFile() as auth:
        if port:
            auth.write(f'proxy-user = "lab:{port.password}"\n'.encode())
            auth.flush()
            args+=["--config",auth.name,"--socks5-basic"]
        if kind=="upload":
            payload.truncate(size)
            args+=["--upload-file","-","--request","POST","--header",f"Content-Length: {size}","--header","Transfer-Encoding:","--header","Expect:"]
        if cancelled():
            raise Failure("cancelled")
        p=subprocess.Popen(args+["--output",response.name,"--url",url],stdin=payload,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        deadline=time.monotonic()+timeout+3
        try:
            while True:
                try:
                    output,_=p.communicate(timeout=.25)
                    break
                except subprocess.TimeoutExpired:
                    if cancelled():
                        raise Failure("cancelled")
                    if time.monotonic()>deadline:
                        raise Failure("probe_deadline")
        finally:
            if p.poll() is None:
                p.kill()
            p.communicate()
        try:
            data=json.loads(output)
        except ValueError:
            raise Failure("curl_output") from None
        status=int(data["http_code"])
        rx,tx=int(data["size_download"]),int(data["size_upload"])
        ok=p.returncode==0 and 200<=status<300 and (kind!="download" or rx==size) and (kind!="upload" or tx==size)
        total=float(data["time_total"])
        transfer_ms=max(0,total-data["time_pretransfer"])*1000 if ok and kind!="check" else None
        code={6:"dns",7:"connect",28:"timeout",35:"tls",60:"certificate",63:"size_limit",97:"socks"}.get(p.returncode,"transport" if p.returncode else "response")
        result=dict(ok=ok,error=None if ok else code,status=status,rx=rx,tx=tx,total_ms=total*1000,
                    tls_complete_ms=data["time_appconnect"]*1000,ttfb_ms=data["time_starttransfer"]*1000,
                    response_ms=max(0,data["time_starttransfer"]-data["time_pretransfer"])*1000 if ok else None,
                    transfer_ms=transfer_ms,
                    proxy_connect_ms=data["time_connect"]*1000 if port else None,
                    mbps=(rx if kind=="download" else tx)*8/total/1e6 if ok and total and kind!="check" else None)
        if body and ok:
            try:
                result["exit_ip"]=str(ipaddress.ip_address(response.read(128).strip().decode()))
            except ValueError:
                result["exit_ip"]=None
        return result
