"""Operator-only subscription export adapter. Supply the exact HTTPS URL in a private file."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from urllib.parse import urlsplit
from .config import parse_document
from .store import Store


def fetch(path):
    if path.stat().st_mode & 0o077:
        raise ValueError("url_file_must_be_private")
    url=path.read_text().strip()
    return parse_document(download(url))


def validate_url(url):
    u=urlsplit(url)
    if u.scheme!="https" or u.username or u.fragment or not u.hostname or any(ord(c)<33 or c in '"\\' for c in url):
        raise ValueError("invalid_subscription_url")
    return u


def download(url,iface=None):
    u=validate_url(url)
    port=u.port or 443
    ips=[r[4][0] for r in socket.getaddrinfo(u.hostname,port,type=socket.SOCK_STREAM)]
    if not ips or not all(ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("restricted_subscription_host")
    ip=ips[0]
    with tempfile.TemporaryFile() as body:
        proc=subprocess.run(["curl","-q",*(["--interface","if!"+iface] if iface else []),"--config","-","--proto","=https","--noproxy","*","--max-filesize","90000",
            "--max-time","15","--resolve",f"{u.hostname}:{port}:"+("["+ip+"]" if ":" in ip else ip),
            "--silent","--write-out","%{stderr}%{http_code}"],input=f'url = "{url}"\n'.encode(),stdout=body,stderr=subprocess.PIPE,timeout=20)
        if proc.returncode or proc.stderr!=b"200":
            raise ValueError("subscription_fetch_failed")
        body.seek(0)
        return body.read(90001).decode()


def main():
    from .main import DATA, KEY
    p=argparse.ArgumentParser()
    p.add_argument("url_file",type=Path)
    p.add_argument("--label",required=True)
    p.add_argument("--product",choices=["Basic","Pro","Other"],default="Other")
    args=p.parse_args()
    os.umask(0o077)
    try:
        outs=fetch(args.url_file)
        key=json.loads((KEY/"identity.json").read_text())["key"].encode()
        store=Store(DATA/"lab.sqlite",key)
        with store.db(True) as db:
            for i,out in enumerate(outs,1):
                cid=store.add(db,out,f"{args.label} {i}",args.product,"")
                db.execute("insert into audit(at,action,object_id) values(unixepoch(),'import_export',?)",(cid,))
    except Exception:
        raise SystemExit("Subscription import failed. Check file permissions, HTTPS target and supported formats.") from None
    print(f"Accepted {len(outs)} entries.")


if __name__=="__main__":main()
