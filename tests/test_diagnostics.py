import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from lab.config import parse
from lab.runner import Failure,handshake,pinned,probe
from lab.subscription import fetch
from tests.test_lab import URI


class GuardTests(unittest.TestCase):
    def test_response_latency_excludes_connection_setup(self):
        with patch("lab.runner.subprocess.Popen") as popen:
            process=popen.return_value
            process.returncode=0
            process.poll.return_value=0
            timing=dict(http_code=204,size_download=0,size_upload=0,time_connect=.001,
                        time_appconnect=.5,time_pretransfer=.6,time_starttransfer=.625,time_total=.63)
            process.communicate.return_value=(json.dumps(timing).encode(),None)
            result=probe("https://localhost",local_fixture=True)
            self.assertTrue(result["ok"])
            self.assertAlmostEqual(result["response_ms"],25)
            self.assertEqual(result["ttfb_ms"],625)
            timing["http_code"]=503
            process.communicate.return_value=(json.dumps(timing).encode(),None)
            self.assertIsNone(probe("https://localhost",local_fixture=True)["response_ms"])

            timing.update(http_code=200,size_upload=100,time_posttransfer=.62)
            process.communicate.return_value=(json.dumps(timing).encode(),None)
            upload=probe("https://localhost",kind="upload",size=100,local_fixture=True,
                         timeout=60,connect_timeout=20)
            self.assertAlmostEqual(upload["transfer_ms"],30)
            self.assertAlmostEqual(upload["mbps"],800/.63/1e6)
            args=popen.call_args.args[0]
            self.assertEqual(args[args.index("--max-time")+1],"60")
            self.assertEqual(args[args.index("--connect-timeout")+1],"20")

    def test_no_unapproved_dns(self):
        with patch("socket.getaddrinfo") as dns,self.assertRaisesRegex(Failure,"peer_unapproved"):
            pinned(parse(URI),[])
        dns.assert_not_called()

    def test_dns_failure(self):
        with patch("socket.getaddrinfo",side_effect=socket.gaierror()):
            with self.assertRaisesRegex(Failure,"peer_dns"):
                pinned(parse(URI),["example.org"])
            self.assertEqual(probe("https://example.org")["error"],"target_dns")

    def test_private_addresses(self):
        with self.assertRaisesRegex(Failure,"target_restricted"):
            probe("https://127.0.0.1")
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/"url"
            f.write_text("https://127.0.0.1/token")
            f.chmod(0o600)
            with self.assertRaisesRegex(ValueError,"restricted_subscription_host"):fetch(f)

    def test_socks_failure(self):
        from io import BytesIO
        from unittest.mock import Mock
        sock=Mock()
        sock.makefile.return_value=BytesIO(b"\x05\xff")
        with self.assertRaisesRegex(Failure,"socks_handshake"):
            handshake(sock,"fixture")

    def test_cancellation_before_transfer(self):
        with self.assertRaisesRegex(Failure,"cancelled"):
            probe("https://localhost",local_fixture=True,cancelled=lambda:True)

    def test_maintenance_window(self):
        from lab.schedules import maintenance
        from datetime import datetime,timezone
        with patch.dict("os.environ",LAB_PAUSE_UTC="23:00-01:30"):
            at=lambda h,m:datetime(2026,1,2,h,m,tzinfo=timezone.utc).timestamp()
            self.assertTrue(maintenance(at(0,30)))
            self.assertFalse(maintenance(at(1,30)))
            self.assertTrue(maintenance(at(23,0)))
