import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from lab import main
from lab.maintenance import copy_db
from lab.runner import proxy
from tests import test_lab as fixtures
from tests.test_lab import URI,ENGINE,probe


class PipelineTests(unittest.TestCase):
    def test_job_to_report_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)
            fixtures.TransportTests.setUpClass()
            self.addCleanup(fixtures.TransportTests.tearDownClass)
            local=lambda url,*args,**kwargs:probe(url,*args,ca=fixtures.TransportTests.cert,**kwargs)
            with patch.object(main,"KEY",path),patch.object(main,"DATA",path),patch("getpass.getpass",return_value="fixture-password-123"),patch.dict(os.environ,LAB_LIVE="1",LAB_XRAY=ENGINE,LAB_CHECK_URL=fixtures.TransportTests.url),patch("lab.worker.pinned",side_effect=lambda out,_:out),patch("lab.worker.probe",side_effect=local):
                main.initialize()
                def login(c):
                    c.headers["x-lab-request"]="1"
                    c.post("/api/v1/login",json={"password":"fixture-password-123"})
                with TestClient(main.create()) as c:
                    login(c)
                    cid=c.post("/api/v1/configurations",json={"text":URI,"name":"test"}).json()["ids"][0]
                    jid=c.post("/api/v1/tests",json={"config":cid}).json()["id"]
                    for _ in range(80):
                        job=c.get("/api/v1/tests").json()[0]
                        if job["state"]=="done":break
                        time.sleep(.05)
                    self.assertEqual((job["id"],job["state"]),(jid,"done"))
                    self.assertEqual(c.get("/api/v1/measurements").json()[0]["confidence"],"valid")
                    copy_db(path/"lab.sqlite",path/"snapshot.db")
                restore=path/"restore"
                restore.mkdir()
                copy_db(path/"snapshot.db",restore/"lab.sqlite")
                with self.assertRaises(FileExistsError):copy_db(path/"snapshot.db",restore/"lab.sqlite")
                with patch.object(main,"DATA",restore),TestClient(main.create()) as c:
                    login(c)
                    self.assertEqual(c.get("/api/v1/reports").json()["successes"],1)


@unittest.skipUnless(shutil.which("xray"),"Xray binary required")
class XrayTests(unittest.TestCase):
    def test_real_loopback_vless(self):
        import socket
        from lab.config import parse,peer
        out=parse(URI.split("?")[0])
        with socket.socket() as s:
            s.bind(("127.0.0.1",0))
            port=s.getsockname()[1]
        dest=peer(out)
        dest.update(address="127.0.0.1",port=port)
        server={"inbounds":[{"listen":"127.0.0.1","port":port,"protocol":"vless","settings":{"clients":[{"id":dest["users"][0]["id"]}],"decryption":"none"}}],"outbounds":[{"protocol":"freedom"}]}
        with tempfile.TemporaryDirectory() as tmp:
            cfg=Path(tmp)/"server.json"
            cfg.write_text(json.dumps(server))
            p=subprocess.Popen(["xray","run","-c",str(cfg)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                fixtures.TransportTests.setUpClass()
                self.addCleanup(fixtures.TransportTests.tearDownClass)
                with proxy(out) as socks:
                    self.assertTrue(probe(fixtures.TransportTests.url.replace("localhost","127.0.0.1"),socks,ca=fixtures.TransportTests.cert)["ok"])
            finally:
                p.terminate()
                p.wait(5)
