import json
import os
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from lab import main
from lab.agents import signature
from tests.test_lab import URI


class RelayTests(unittest.TestCase):
    def test_replay_and_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)
            reg=path/"registry"
            reg.write_text(json.dumps({"MCI":{"key":"fixture","configs":{"9":1}}}))
            with patch.object(main,"KEY",path),patch.object(main,"DATA",path),patch.dict(os.environ,LAB_AGENT_REGISTRY=str(reg)),patch("getpass.getpass",return_value="fixture-password-123"):
                main.initialize()
                with TestClient(main.create()) as c:
                    c.headers["x-lab-request"]="1"
                    c.post("/api/v1/login",json={"password":"fixture-password-123"})
                    c.post("/api/v1/configurations",json={"text":URI,"name":"fixture"})
                    body=json.dumps({"samples":[dict(method="0123456789abcdef",source_id=1,config=9,at=int(time.time()),ok=True,ms=25,kind="check",confidence="valid")]}).encode()
                    stamp=str(int(time.time()))
                    endpoint="/api/v1/agent-batches/MCI"
                    self.assertEqual(c.post(endpoint,content=body).status_code,403)
                    headers={"x-lab-time":stamp,"x-signature":signature("fixture",stamp,body)}
                    for _ in range(2):
                        self.assertEqual(c.post(endpoint,content=body,headers=headers).status_code,200)
                    stats=c.get("/api/v1/comparison").json()
                    self.assertEqual((stats[0]["agent"],stats[0]["checks"]),("MCI",1))
