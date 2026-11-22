"""Optional Chromium end-to-end test; use python -m unittest tests.test_ui."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen
from unittest.mock import patch
from lab import main
from lab.store import Store
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright=None


@unittest.skipUnless(os.getenv("LAB_BROWSER_TEST")=="1" and sync_playwright and shutil.which("chromium"),"Set LAB_BROWSER_TEST=1 on a Chromium-capable host")
class BrowserTests(unittest.TestCase):
    def test_operator_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            key=Path(tmp)/"keys"
            with patch.object(main,"KEY",key),patch("getpass.getpass",return_value="fixture-password-123"):
                main.initialize()
            identity = json.loads((key / "identity.json").read_text())
            store = Store(Path(tmp) / "lab.sqlite", identity["key"].encode())
            with store.db(True) as db:
                db.execute("insert into feeds(name,url,iface,hours,enabled,due) values"
                           "('Fixture',?,'lo',12,1,?)",
                           (store.cipher.encrypt(b"https://example.org/fixture"), time.time()+3600))
            with socket.socket() as sock:
                sock.bind(("127.0.0.1",0))
                port=sock.getsockname()[1]
            url=f"http://127.0.0.1:{port}"
            env=dict(os.environ,LAB_DATA=tmp,LAB_KEY_DIR=str(key),LAB_PORT=str(port),LAB_LIVE="0",LAB_TELEGRAM="0")
            proc=subprocess.Popen([sys.executable,"-m","lab.main","serve"],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    try:
                        with urlopen(url+"/health",timeout=.3):break
                    except OSError:time.sleep(.05)
                with sync_playwright() as pw:
                    b=pw.chromium.launch(executable_path=shutil.which("chromium"),args=["--no-sandbox"])
                    tab=b.new_page()
                    tab.goto(url)
                    tab.locator('input[name="password"]').fill("fixture-password-123")
                    tab.get_by_role("button", name="ورود", exact=True).click()
                    tab.get_by_role("heading", name="تست اشتراک", exact=True).wait_for()
                    tab.get_by_role("button", name="توقف تست سرعت", exact=True).click()
                    tab.get_by_role("button", name="ادامهٔ تست سرعت", exact=True).wait_for()
                    self.assertEqual(store.rows("select enabled from feeds"), [{"enabled":0}])
                    for width in (1280, 390):
                        tab.set_viewport_size({"width":width, "height":900})
                        self.assertTrue(tab.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth"))
                    tab.get_by_role("button", name="پاک‌کردن تاریخچه", exact=True).click()
                    tab.get_by_text("نتیجه پاک می‌شود.", exact=False).wait_for()
                    tab.get_by_role("button", name="انصراف", exact=True).click()
                    tab.get_by_role("button", name="حذف اشتراک", exact=True).click()
                    deletion=tab.get_by_role("dialog", name="حذف اشتراک")
                    deletion.get_by_role("button", name="حذف اشتراک", exact=True).click()
                    tab.get_by_text("اشتراک حذف شد.", exact=True).wait_for()
                    tab.get_by_role("heading", name="Fixture", exact=True).wait_for(state="detached")
                    self.assertEqual(store.rows("select deleted from feeds"), [{"deleted":1}])
                    tab.get_by_role("button", name="خروج", exact=True).click()
                    tab.get_by_role("heading", name="ورود مدیر", exact=True).wait_for()
                    b.close()
            finally:
                proc.terminate()
                proc.wait(8)
