import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock,patch
from cryptography.fernet import Fernet
from lab.store import Store
from lab.worker import Worker
from tests.test_lab import URI,parse


class NotificationTests(unittest.TestCase):
    def test_delivery_retry(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,LAB_TELEGRAM="1",LAB_BOT_TOKEN="fixture",LAB_CHAT_ID="fixture"):
            store=Store(Path(tmp)/"db",Fernet.generate_key())
            with store.db(True) as db:
                store.add(db,parse(URI),"fixture","Pro","TR")
                db.execute("insert into incidents(config,opened) values(1,0)")
                db.execute("insert into alerts(incident,event) values(1,'outage')")
            w=Worker(store)
            with patch("lab.worker.httpx.Client") as client:
                post=client.return_value.__enter__.return_value.post
                post.return_value.is_success=False
                w.notify()
                w.notify()
                self.assertEqual(post.call_count,1)
                with store.db(True) as db: db.execute("update alerts set due=0")
                post.return_value.is_success=True
                post.return_value.json.return_value={"ok":True}
                w.notify()
                w.notify()
                self.assertEqual(post.call_count,2)
                self.assertNotIn("00000001",str(post.call_args))
            self.assertEqual(store.rows("select state from alerts")[0]["state"],"sent")
