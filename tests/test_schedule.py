import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from cryptography.fernet import Fernet
from lab.schedules import configure,dispatch
from lab.store import Store
from tests.test_lab import URI,parse


class HeavyScheduleTests(unittest.TestCase):
    @patch.dict(os.environ,LAB_SCHEDULE="1",LAB_LIVE="1",LAB_SPEED="1",LAB_UPLOAD_URL="https://example.org")
    def test_single_reserved_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/"db",Fernet.generate_key())
            with store.db(True) as db:
                cid=store.add(db,parse(URI),"fixture","Other","")
                configure(db,cid,"upload",86400)
                db.execute("update heavy_schedules set due=0")
                dispatch(store,db,5)
                dispatch(store,db,6)
                configure(db,cid,"upload",0)
            jobs=store.rows("select * from jobs")
            self.assertEqual(len(jobs),1)
            self.assertGreater(jobs[0]["reserved"],1000000)
