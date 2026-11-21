"""Measure a disposable local HTTPS service, then show those observations in the dashboard."""
import json
import os
from pathlib import Path
import tempfile
import time


def main():
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="mobohub-demo-") as tmp:
        os.environ.update(LAB_DATA=tmp,LAB_KEY_DIR=tmp,LAB_LIVE="0",LAB_SCHEDULE="0",LAB_SPEED="0",LAB_TELEGRAM="0",LAB_AGENT="loopback-fixture",LAB_ISP="loopback")
        from . import main as api
        from .config import parse
        from .runner import proxy
        from .store import Store
        from tests.test_lab import TransportTests,URI,ENGINE,probe
        import uvicorn
        api.initialize()
        store=Store(Path(tmp)/"lab.sqlite",json.loads((Path(tmp)/"identity.json").read_text())["key"].encode(),"loopback-fixture")
        out=parse(URI)
        with store.db(True) as db:
            cid=store.add(db,out,"Loopback fixture","Other","")
        TransportTests.setUpClass()
        try:
            with proxy(out,ENGINE) as endpoint:
                for _ in range(3):
                    base=probe(TransportTests.url,ca=TransportTests.cert)
                    result=probe(TransportTests.url,endpoint,ca=TransportTests.cert)
                    with store.db(True) as db:
                        jid=db.execute("insert into jobs(config,agent,kind,state,created) values(?,'loopback-fixture','check','done',?)",(cid,int(time.time()))).lastrowid
                        store.record(db,dict(id=jid,config=cid,kind="check"),base,result)
                    time.sleep(1)
        finally:
            TransportTests.tearDownClass()
        print("Three local fixture checks recorded. Sign in at http://127.0.0.1:"+os.getenv("LAB_PORT","8765"),flush=True)
        uvicorn.run(api.create(),host="127.0.0.1",port=int(os.getenv("LAB_PORT","8765")),access_log=False)


if __name__=="__main__":main()
