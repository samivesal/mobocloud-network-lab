import base64
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from lab import main
from lab.config import parse,parse_document
from lab.runner import Failure,probe as live_probe,proxy
from functools import partial
probe=partial(live_probe,local_fixture=True)
from lab.store import Store
from lab.worker import Worker

URI="vless://00000000-0000-4000-8000-000000000001@example.org:443?type=ws&security=tls&path=%2Ftest"
ENGINE=str(Path(__file__).with_name("fixture_engine.py"))


class ConfigTests(unittest.TestCase):
    def test_transports(self):
        for net in ("ws","grpc","xhttp","tcp"):
            s=URI.split("?")[0]+"?type="+net
            self.assertEqual(parse(s)["streamSettings"]["network"],net)
        self.assertEqual(parse(URI)["streamSettings"]["wsSettings"]["path"],"/test")
    def test_invalid(self):
        for s in (URI.replace("00000001","bad"),URI+"&danger=1",URI.replace("ws","udp"),URI.replace("example.org","0.0.0.0")):
            with self.assertRaises(ValueError): parse(s)
    def test_ss(self):
        auth=base64.urlsafe_b64encode(b"aes-128-gcm:fake:pw").decode().rstrip("=")
        self.assertEqual(parse("ss://"+auth+"@example.org:443")["settings"]["servers"][0]["password"],"fake:pw")
    def test_json(self):
        out=parse(URI)
        self.assertEqual(parse_document(json.dumps({"outbounds":[out]})),[out])
        out["proxySettings"]={"tag":"unexpected"}
        with self.assertRaises(ValueError):parse_document(json.dumps(out))


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/"test.db",Fernet.generate_key())
        with self.store.db(True) as db:
            self.cid=self.store.add(db,parse(URI),"test","Pro","TR")
    @patch.dict(os.environ,{"LAB_LIVE":"1","LAB_SPEED":"1","LAB_DOWNLOAD_URL":"https://example.org","LAB_CONFIG_CAP":"1"})
    def test_budget(self):
        with self.assertRaisesRegex(ValueError,"budget_exhausted"),self.store.db(True) as db:
            self.store.enqueue(db,self.cid,"download")
    @patch.dict(os.environ,{"LAB_LIVE":"1","LAB_SCHEDULE":"1","LAB_TELEGRAM":"1"})
    def test_scheduler_incidents_and_recovery(self):
        w=Worker(self.store)
        with self.store.db(True) as db:
            db.execute("update configs set interval=300")
        job=w.claim()
        self.assertIsNone(w.claim())
        with self.store.db(True) as db:
            for base,success in [(False,False)]*4+[(True,False)]*4+[(True,True)]*3:
                self.store.record(db,job,{"ok":base},{"ok":success})
        inc=self.store.rows("select * from incidents")
        self.assertEqual(len(inc),1)
        self.assertIsNotNone(inc[0]["closed"])
        self.assertEqual(len(self.store.rows("select * from alerts")),2)
        w.stop.set()
        w.loop()
        self.assertEqual(self.store.rows("select state from jobs")[0]["state"],"interrupted")
    def test_api(self):
        path=Path(self.tmp.name)
        with patch.object(main,"DATA",path/"data"),patch.object(main,"KEY",path),patch("getpass.getpass",return_value="local-test-only-password"):
            main.initialize()
            with TestClient(main.create()) as c:
                prefix="/api/v1/"
                self.assertEqual(c.get(prefix+"tests").status_code,401)
                self.assertEqual(c.post(prefix+"login",json={}).status_code,403)
                c.headers.update({"X-Lab-Request":"1"})
                self.assertEqual(c.post(prefix+"login",json={"password":"local-test-only-password"}).status_code,200)
                data={"text":URI,"name":"Local fixture"}
                for _ in range(2):
                    self.assertEqual(c.post(prefix+"configurations",json=data).json()["ids"],[1])
                inventory=c.get(prefix+"configurations").text
                self.assertNotIn("00000001",inventory)
                self.assertNotIn("secret",inventory)
                self.assertEqual(c.post(prefix+"tests",json={"config":1}).status_code,409)
                self.assertEqual(c.get(prefix+"reports").json()["checks"],0)
                self.assertEqual(c.get(prefix+"reports?format=csv").status_code,200)
                self.assertTrue(c.get("/").is_success)
                self.assertTrue(c.post(prefix+"logout").is_success)
                self.assertEqual(c.get(prefix+"tests").status_code,401)


class Target(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        body=b"127.0.0.1"
        self.send_response(200)
        self.send_header("Content-Length",str(len(body)+(2 if self.path=="/partial" else 0)))
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        if self.path=="/interrupted":
            self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        else:
            self.send_response(204)
            self.end_headers()


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        p=Path(cls.tmp.name)
        cls.cert=str(p/"cert.pem")
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost","-addext","subjectAltName=DNS:localhost,IP:127.0.0.1","-keyout",str(p/"key"),"-out",cls.cert],check=True,capture_output=True)
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),Target)
        sslctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sslctx.load_cert_chain(cls.cert,p/"key")
        cls.server.socket=sslctx.wrap_socket(cls.server.socket,server_side=True)
        cls.url="https://localhost:"+str(cls.server.server_port)
        cls.thread=threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()
        cls.tmp.cleanup()
    def test_measurements(self):
        with proxy(parse(URI),ENGINE) as port:
            self.assertTrue(probe(self.url,port,ca=self.cert,body=True)["exit_ip"])
            self.assertFalse(probe(self.url,port)["ok"])
            self.assertTrue(probe(self.url,port,"download",9,9,self.cert)["mbps"])
            self.assertFalse(probe(self.url+"/partial",port,"download",11,11,self.cert)["ok"])
            self.assertTrue(probe(self.url,port,"upload",120,ca=self.cert)["ok"])
            result=probe(self.url+"/interrupted",port,"upload",120,ca=self.cert)
            self.assertFalse(result["ok"])
            self.assertIsNone(result["mbps"])
        with self.assertRaises(OSError):socket.create_connection(("127.0.0.1",port.port),.1)
    def test_crashed_engine(self):
        with self.assertRaisesRegex(Failure,"engine_start"),proxy(parse(URI),"/bin/false"):pass
    def test_suspended_cleanup(self):
        start_process = subprocess.Popen
        children = []

        def start_fixture(*args, **kwargs):
            child = start_process(*args, **kwargs)
            children.append(child)
            return child

        with patch("lab.runner.subprocess.Popen", side_effect=start_fixture):
            with proxy(parse(URI), ENGINE):
                child = children[-1]
                child.send_signal(signal.SIGSTOP)
        self.assertIsNotNone(child.poll())
