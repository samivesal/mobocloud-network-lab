from datetime import datetime,timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from cryptography.fernet import Fernet
from lab.store import Store
from lab.reports import finalize, prior_periods


class ReportTests(unittest.TestCase):
    def test_leap_month_and_idempotence(self):
        now=datetime(2024,3,1,12,tzinfo=timezone.utc).timestamp()
        periods=list(prior_periods(now))
        self.assertEqual(periods[-1][2]-periods[-1][1],29*86400)
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/"db",Fernet.generate_key())
            with store.db(True) as db:db.execute("update report_clock set observed=0")
            finalize(store,now)
            finalize(store,now)
            self.assertEqual(len(store.rows("select * from summaries")),3)
