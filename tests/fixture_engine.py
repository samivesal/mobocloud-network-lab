#!/usr/bin/env python3
"""Test-only SOCKS5 relay. All requests can reach loopback only."""
import json
import os
import select
import socket
import socketserver
import struct
import sys


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        hello=self.rfile.read(2)
        if len(hello)!=2 or hello[0]!=5 or 2 not in self.rfile.read(hello[1]):
            return
        self.wfile.write(b"\x05\x02")
        if self.rfile.read(1)!=b"\x01":
            return
        user=self.rfile.read(self.rfile.read(1)[0])
        password=self.rfile.read(self.rfile.read(1)[0])
        if user!=b"lab" or password!=self.server.password:
            self.wfile.write(b"\x01\x01")
            return
        self.wfile.write(b"\x01\x00")
        req=self.rfile.read(4)
        if not req:
            return
        if req[3]==3:
            self.rfile.read(self.rfile.read(1)[0])
        else:
            self.rfile.read(4 if req[3]==1 else 16)
        port=struct.unpack("!H",self.rfile.read(2))[0]
        with socket.create_connection(("127.0.0.1",port)) as target:
            self.wfile.write(b"\x05\x00\x00\x01"+bytes(6))
            while True:
                readable,_,_=select.select([target,self.connection],[],[],4)
                if not readable:
                    return
                for src in readable:
                    data=src.recv(8192)
                    if not data:
                        return
                    (target if src==self.connection else self.connection).sendall(data)


if __name__=="__main__":
    cfg=json.load(open(sys.argv[-1]))
    server=socketserver.ThreadingTCPServer(("127.0.0.1",cfg["inbounds"][0]["port"]),Handler)
    server.password=cfg["inbounds"][0]["settings"]["accounts"][0]["pass"].encode()
    server.daemon_threads=True
    server.serve_forever()
