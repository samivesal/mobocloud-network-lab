import base64
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import quote

from cryptography.fernet import Fernet

from lab.config import parse
from lab.feeds import refresh
from lab.store import Store
from lab import telegram_remote
from lab.telegram_agent import HostTarget, Relay, RelayError, Setup, pinned_host, provision
from lab.telegram_bridge import SCHEMA, apply_update, database, initialize, synchronize
from tests.test_lab import URI


def update(number, chat, text):
    return {"update_id": number, "message": {"chat": {"id": chat, "type": "private"},
                                             "from": {"id": chat}, "text": text}}


def callback(number, chat, text):
    return {"update_id": number, "callback_query": {"id": str(number), "data": text, "from": {"id": chat},
            "message": {"chat": {"id": chat, "type": "private"}}}}


class TelegramRelayTests(unittest.TestCase):
    @patch.dict(os.environ, LAB_LIVE="1", LAB_SPEED="1", LAB_SCHEDULE="1", LAB_PAUSE_UTC="",
                LAB_CHECK_URL="https://example.org/check", LAB_DOWNLOAD_URL="https://example.org/down")
    def test_detailed_reports_keep_all_pages_after_a_lost_acknowledgement(self):
        report_names = [f"Node {i:03d}" for i in range(240)]
        lines = [URI.replace("example.org", f"node{i}.example.org") + "#" + quote(name + " " + "🌐" * 30)
                 for i, name in enumerate(report_names)]
        disabled = URI.replace("example.org", "disabled.example.org")
        lines.extend([disabled + "#Skipped%20node", URI.replace("example.org", "127.0.0.1") + "#Rejected%20node",
                      URI + "#" + quote("Redacted node https://example.org/private-subscription")])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "local.sqlite", Fernet.generate_key())
            bridge = root / "bridge.sqlite"
            with database(bridge) as db:
                initialize(db)
            with store.db(True) as db:
                db.execute("insert into feeds(name,url,iface,hours,enabled,due) values('Fixture',?,'lo',12,1,0)",
                           (store.cipher.encrypt(b"fixture"),))
                db.execute("insert into telegram_link(id,secret) values(1,?)",
                           (store.cipher.encrypt(b'{"link_id":"fixture","since":0}'),))
                cid = store.add(db, parse(disabled), "Disabled", "Other", "")
                db.execute("update configs set enabled=0 where id=?", (cid,))
            with patch("lab.feeds.require_link"), patch("lab.feeds.download", return_value="\n".join(lines)):
                refresh(store)
            with store.db(True) as db:
                for job in db.execute("select * from jobs order by id").fetchall():
                    if job["id"] == 2:
                        result = {"ok":False, "error":"timeout", "ping_error":"timeout", "upload":{"error":"timeout"}}
                    else:
                        result = {"ok":True,"ping_ms":30,"mbps":12,"upload":{"ok":True,"mbps":6}}
                    store.record(db, job, {"ok": True}, result)
                db.execute("update jobs set state='done'")

            sent_batches = []

            def exchange(_, payload):
                sent_batches.append([report["id"] for report in payload["reports"]])
                self.assertLess(len(json.dumps(payload)), 120_000)
                with database(bridge) as db:
                    response = synchronize(db, payload)
                if len(sent_batches) == 1:
                    raise RelayError("relay_unavailable")
                return response

            relay = Relay(store)
            with patch("lab.telegram_agent.exchange", side_effect=exchange):
                relay.poll()
                self.assertEqual(store.rows("select round_id from telegram_reports"), [])
                relay.poll()
                self.assertEqual(sent_batches[0], sent_batches[1])
                for _ in range(6):
                    relay.poll()
            self.assertEqual(store.rows("select round_id from telegram_reports"), [{"round_id": 1}])
            self.assertEqual(store.rows("select part from telegram_report_outbox"), [])
            self.assertEqual(sent_batches[-1], [])
            with database(bridge) as db:
                pages = [row["text"] for row in db.execute("select text from outgoing order by rowid")]
            self.assertGreater(len(pages), 8)
            self.assertTrue(all(len(page.encode("utf-16-le")) // 2 < 3500 for page in pages))
            report = "\n".join(pages)
            for name in report_names:
                self.assertEqual(report.count(name), 1)
            self.assertIn("VLESS / WS", report)
            self.assertIn("Ping: 30 ms · Download: 12.0 Mbps · Upload: 6.0 Mbps", report)
            self.assertIn("Ping: timeout · Download: timeout · Upload: timeout", report)
            self.assertIn("Skipped node\nSkipped", report)
            self.assertIn("Rejected node\nRejected", report)
            self.assertNotIn("private-subscription", report)

    @patch.dict(os.environ, LAB_LIVE="1", LAB_SPEED="1", LAB_SCHEDULE="1", LAB_PAUSE_UTC="",
                LAB_CHECK_URL="https://example.org/check", LAB_DOWNLOAD_URL="https://example.org/down")
    def test_bot_sets_independent_schedules_and_reports_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "local.sqlite", Fernet.generate_key())
            bridge = root / "bridge.sqlite"
            with database(bridge) as db:
                initialize(db)
            with store.db(True) as db:
                db.execute("insert into feeds(name,url,iface,hours,enabled,due) values(?,?,'lo',12,0,0)",
                           ("Fixture", store.cipher.encrypt(b"fixture")))
                db.execute("insert into telegram_link(id,secret) values(1,?)",
                           (store.cipher.encrypt(b'{"link_id":"fixture","since":0}'),))

            def exchange(_, payload):
                with database(bridge) as db:
                    return synchronize(db, payload)

            relay = Relay(store)
            now = int(time.time())
            with (patch("lab.telegram_agent.exchange", side_effect=exchange), patch("lab.feeds.require_link"),
                  patch("lab.feeds.download", return_value=URI), patch("lab.feeds.time.time", return_value=now) as clock):
                relay.poll()
                with database(bridge) as db:
                    apply_update(db, update(1, 42, "/schedule"), 42)
                with database(bridge) as db:
                    shown = db.execute("select markup from outgoing where id='update:1'").fetchone()["markup"]
                    self.assertIn("schedule:1", shown)
                    apply_update(db, callback(2, 42, "period:ping:1"), 42)
                with database(bridge) as db:
                    shown = db.execute("select markup from outgoing where id='update:2'").fetchone()["markup"]
                    self.assertIn("set:ping:1:1", shown)
                    apply_update(db, callback(3, 43, "set:ping:1:1"), 42)
                relay.poll()
                self.assertEqual(store.rows("select ping_hours from feeds"), [{"ping_hours": 0}])
                with database(bridge) as db:
                    apply_update(db, callback(4, 42, "set:ping:1:1"), 42)
                with database(bridge) as db:
                    apply_update(db, update(5, 42, "/speed_every 1 3"), 42)
                relay.poll()
                clock.return_value = now + 20
                relay.poll()
                periods = store.rows("select ping_hours,ping_due,hours,due,enabled from feeds")[0]
                self.assertEqual(periods, dict(ping_hours=1, ping_due=now+3600, hours=3, due=now+10800, enabled=1))
                refresh(store)
                self.assertEqual(store.rows("select id from feed_rounds"), [])

                clock.return_value = now + 3600
                refresh(store)
                refresh(store)
                self.assertEqual(store.rows("select kind,ping_only from feed_rounds"), [{"kind":"scheduled", "ping_only":1}])
                with store.db(True) as db:
                    job = db.execute("select * from jobs").fetchone()
                    self.assertEqual(job["kind"], "ping")
                    store.record(db, job, {"ok":True}, {"ok":True,"ping_ms":39.7})
                    db.execute("update jobs set state='done'")
                relay.poll()
                relay.poll()
                with database(bridge) as db:
                    reports = db.execute("select text from outgoing where id like 'round:%'").fetchall()
                    self.assertEqual(len(reports), 1)
                    self.assertIn("Ping: 40 ms", reports[0]["text"])
                    self.assertNotRegex(reports[0]["text"], "Download|Upload")
                    apply_update(db, update(6, 42, "/ping_every 1 0"), 42)
                relay.poll()
                self.assertEqual(store.rows("select ping_hours,due from feeds"), [{"ping_hours":0,"due":now+10800}])

                clock.return_value = now + 10800
                refresh(store)
                self.assertEqual(store.rows("select kind from jobs order by id"), [{"kind":"ping"}, {"kind":"download"}])
                with store.db(True) as db:
                    job = db.execute("select * from jobs where kind='download'").fetchone()
                    store.record(db, job, {"ok":True}, {"ok":True,"ping_ms":30,"mbps":12,"upload":{"ok":True,"mbps":6}})
                    db.execute("update jobs set state='done'")
                relay.poll()
                with database(bridge) as db:
                    report = db.execute("select text from outgoing where id='round:2'").fetchone()["text"]
                    self.assertIn("Download: 12.0 Mbps", report)
                    self.assertIn("Upload: 6.0 Mbps", report)

    def test_legacy_bridge_commands_migrate_without_becoming_schedule_requests(self):
        with tempfile.TemporaryDirectory() as tmp, database(Path(tmp) / "bridge.sqlite") as db:
            db.executescript("""create table commands(id integer primary key, feed integer not null,
                answered integer not null default 0); insert into commands(id,feed) values(1,9);""")
            initialize(db)
            db.execute("insert into commands(id,feed,operation,hours) values(2,9,'ping_schedule',1)")
            db.commit()
            synced = synchronize(db, dict(feeds=[], reports=[], acks=[]))
            self.assertEqual(synced["commands"], [dict(id=1, feed=9, operation="speed", hours=None)])

    @patch.dict(os.environ, LAB_LIVE="1", LAB_SPEED="0", LAB_SCHEDULE="0",
                LAB_CHECK_URL="https://example.org/check", LAB_DOWNLOAD_URL="")
    def test_ping_schedule_obeys_the_global_schedule_flag_but_needs_no_speed_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "local.sqlite", Fernet.generate_key())
            with store.db(True) as db:
                db.execute("insert into feeds(name,url,iface,hours,enabled,due) values('Fixture',?,'lo',12,0,0)",
                           (store.cipher.encrypt(b"fixture"),))
                request = dict(id=1, feed=1, operation="ping_schedule", hours=1)
                Relay.queue(db, "fixture", request)
                self.assertIn("Enable live testing", db.execute("select reply from telegram_commands").fetchone()["reply"])
                with patch.dict(os.environ, LAB_SCHEDULE="1"):
                    Relay.queue(db, "fixture", dict(request, id=2, hours=169))
                    self.assertEqual(db.execute("select ping_hours from feeds").fetchone()["ping_hours"], 0)
                    Relay.queue(db, "fixture", dict(request, id=3))
                    self.assertEqual(db.execute("select ping_hours,enabled from feeds").fetchone()[:], (1, 0))

    def test_ssh_fingerprint_pins_only_matching_key(self):
        key = b"fixture-key"
        encoded = base64.b64encode(key).decode()
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key).digest()).decode().rstrip("=")
        with patch("lab.telegram_agent.child", return_value=f"host ssh-ed25519 {encoded}\n".encode()):
            self.assertEqual(pinned_host("198.51.100.4",2222,fingerprint),
                             f"[198.51.100.4]:2222 ssh-ed25519 {encoded}\n")
            with self.assertRaisesRegex(RelayError, "relay_fingerprint_mismatch"):
                pinned_host("198.51.100.4",2222,"SHA256:"+"A"*43)

    def test_host_discovery_prefers_ed25519_and_reuses_only_matching_saved_key(self):
        target = HostTarget(server="198.51.100.4", ssh_port=2222)
        rsa_key = base64.b64encode(b"fixture-rsa").decode()
        ed_key = base64.b64encode(b"fixture-ed").decode()
        ed_fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(b"fixture-ed").digest()).decode().rstrip("=")
        with (tempfile.TemporaryDirectory() as tmp,
              patch("lab.telegram_agent.public_server", return_value=target.server),
              patch("lab.telegram_agent.child") as run):
            store = Store(Path(tmp) / "db", Fernet.generate_key())
            relay = Relay(store)
            run.return_value = f"host ssh-rsa {rsa_key}\nhost ssh-ed25519 {ed_key}\n".encode()
            expected = dict(fingerprint=ed_fingerprint, algorithm="ssh-ed25519", trusted=False, changed=False)
            self.assertEqual(relay.host_key(target), expected)
            run.assert_called_once_with(["ssh-keyscan", "-T", "6", "-p", "2222", target.server],
                                        timeout=12, error="relay_ssh_unreachable")
            saved = dict(server=target.server, ssh_port=target.ssh_port,
                         known=f"[{target.server}]:2222 ssh-ed25519 {ed_key}\n")
            with store.db(True) as db:
                db.execute("insert into telegram_link(id,secret) values(1,?)",
                           (store.cipher.encrypt(json.dumps(saved).encode()),))
            self.assertEqual(relay.host_key(target), dict(expected, trusted=True))

            run.return_value = f"host ssh-rsa {rsa_key}\n".encode()
            known = relay.host_key(target)
            self.assertEqual((known["algorithm"], known["trusted"], known["changed"]), ("ssh-rsa", False, True))
            run.reset_mock()
            form = Setup(**target.model_dump(), fingerprint=ed_fingerprint, password="fixture-admin",
                         bot_token="12345:fixtureBotToken0123456", chat_id=42)
            with self.assertRaisesRegex(RelayError, "relay_fingerprint_mismatch"):
                provision(form)
            self.assertEqual([call.args[0][0] for call in run.call_args_list], ["ssh-keyscan"])

    def test_setup_sends_secrets_through_ssh_and_checks_restricted_connection(self):
        form = Setup(server="198.51.100.4", fingerprint="SHA256:" + "A" * 43,
                     password="fixture-admin", bot_token="12345:fixtureBotToken0123456", chat_id=42)

        def run(args, **options):
            if args[0] == "ssh-keygen":
                transport = Path(args[-1])
                transport.write_text("fixture-transport-key")
                transport.with_suffix(".pub").write_text("ssh-ed25519 Zml4dHVyZQ== mobocloud-relay")
                return b""
            self.assertEqual(args[0], "ssh")
            self.assertNotIn("fixture-admin", " ".join(args))
            self.assertNotIn("fixtureBotToken", " ".join(args))
            self.assertEqual(Path(options["env"]["MOBO_PASSWORD_FILE"]).read_text(), "fixture-admin\n")
            self.assertEqual(json.loads(options["payload"])["config"]["token"], form.bot_token.get_secret_value())
            return b"mobocloud-relay-ready"

        with (patch("lab.telegram_agent.ipaddress.ip_address") as address,
              patch("lab.telegram_agent.pinned_host", return_value="fixture-host-key"),
              patch("lab.telegram_agent.child", side_effect=run),
              patch("lab.telegram_agent.exchange") as synchronize_relay):
            address.return_value.is_global = True
            address.return_value.is_multicast = False
            address.return_value.__str__.return_value = form.server
            config = provision(form)
            self.assertEqual(config["key"], "fixture-transport-key")
            self.assertNotIn("fixtureBotToken", str(config))
            synchronize_relay.assert_called_once_with(config, {"feeds": [], "reports": [], "acks": []})
            synchronize_relay.side_effect = RelayError("relay_unavailable")
            with self.assertRaisesRegex(RelayError, "relay_transport_unavailable"):
                provision(form)

    def test_setup_rejects_private_servers_before_network_access(self):
        form = Setup(server="127.0.0.1", fingerprint="SHA256:" + "A" * 43,
                     password="fixture-admin", bot_token="12345:fixtureBotToken0123456", chat_id=42)
        with patch("lab.telegram_agent.child") as run:
            with self.assertRaisesRegex(RelayError, "relay_invalid_server"):
                provision(form)
        run.assert_not_called()

    def test_remote_setup_restores_previous_files_if_health_check_fails(self):
        source = """def telegram(token, method, data):
    if method == 'getChat':
        return {'id': 42, 'type': 'private'}
    return {}
"""
        config = {"token": "fixture", "chat": 42, "port": 8876, "link_id": "new"}
        data = dict(config=config, source=source, transport_key="ssh-ed25519 Zml4dHVyZQ== mobocloud-relay")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, credentials, unit, home = (root / name for name in ("app", "config", "unit", "home"))
            for path in (app, credentials, unit):
                path.write_text("previous")

            def directory(path, mode):
                path.mkdir(mode=mode, exist_ok=True)

            with (patch.multiple(telegram_remote, HOME=home, APP=app, CONFIG=credentials, UNIT=unit),
                  patch.object(telegram_remote.os, "geteuid", return_value=0),
                  patch.object(telegram_remote.pwd, "getpwnam", return_value=SimpleNamespace(pw_dir=str(home), pw_shell="/bin/sh")),
                  patch.object(telegram_remote, "managed_directory", side_effect=directory),
                  patch.object(telegram_remote, "systemctl") as systemctl,
                  patch.object(telegram_remote, "build_opener") as opener,
                  patch.object(telegram_remote.time, "sleep")):
                request = opener.return_value.open
                request.side_effect = lambda *args, **kwargs: BytesIO(b'{"link_id":"previous"}')
                with self.assertRaisesRegex(telegram_remote.SetupError, "relay_remote_health"):
                    telegram_remote.install(data)
                for path in (app, credentials, unit):
                    self.assertEqual(path.read_text(), "previous")
                self.assertFalse((home / ".ssh" / "authorized_keys").exists())
                systemctl.assert_any_call("start", telegram_remote.SERVICE, check=False)

                request.side_effect = lambda *args, **kwargs: BytesIO(b'{"link_id":"new"}')
                telegram_remote.install(data)
                systemctl.assert_called_with("enable", telegram_remote.SERVICE)
                self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)
                self.assertIn('restrict,command=', (home / ".ssh" / "authorized_keys").read_text())

    @patch.dict(os.environ, LAB_LIVE="1", LAB_SPEED="1", LAB_SCHEDULE="1",
                LAB_CHECK_URL="https://example.org/check", LAB_DOWNLOAD_URL="https://example.org/down")
    def test_authorized_commands_and_durable_round_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root/"local.sqlite",Fernet.generate_key())
            relay = Relay(store)
            bridge = root/"bridge.sqlite"
            with database(bridge) as db:
                db.executescript(SCHEMA)
            with store.db(True) as db:
                db.execute("""insert into feeds(name,url,iface,hours,enabled,due) values(?,?,?,?,0,0)""",
                           ("Fixture https://example.org/private-feed",store.cipher.encrypt(b"fixture"),"lo",12))
            form = Setup(server="198.51.100.4",fingerprint="SHA256:"+"A"*43,
                         password="fixture-admin",bot_token="12345:fixtureBotToken0123456",chat_id=42)
            config = {"link_id":"fixture","since":0,"key":"fixture-transport-key"}
            with patch("lab.telegram_agent.provision",return_value=config):
                relay.connect(form)
            stored = store.rows("select secret from telegram_link")[0]["secret"]
            self.assertNotIn(b"fixture-transport-key", stored)
            self.assertNotIn(b"fixtureBotToken", store.cipher.decrypt(stored))

            def exchange(_, payload):
                with database(bridge) as db:
                    return synchronize(db, payload)

            with (patch("lab.telegram_agent.exchange", side_effect=exchange),
                  patch("lab.feeds.require_link"), patch("lab.feeds.download", return_value=URI)):
                relay.poll()
                with database(bridge) as db:
                    apply_update(db,update(1,43,"/test 1"),42)
                with database(bridge) as db:
                    self.assertIsNone(db.execute("select id from commands").fetchone())
                    apply_update(db,update(2,42,"/test"),42)
                with database(bridge) as db:
                    button = json.loads(db.execute("select markup from outgoing").fetchone()["markup"])
                    self.assertEqual(button["inline_keyboard"][0][0]["callback_data"],"test:1")
                    self.assertNotIn("private-feed",json.dumps(button))
                    apply_update(db,update(3,42,"/test 1"),42)
                relay.poll()
                relay.poll()
                rounds = store.rows("select id,cloudflare_upload from feed_rounds")
                self.assertEqual(len(rounds),1)
                self.assertEqual(rounds[0]["cloudflare_upload"],1)
                self.assertEqual(store.rows("select acknowledged from telegram_commands"),[{"acknowledged":1}])
                refresh(store)
                with store.db(True) as db:
                    job = db.execute("select * from jobs").fetchone()
                    store.record(db,job,{"ok":True},{"ok":True,"ping_ms":25,"mbps":12,
                                                       "upload":{"ok":True,"mbps":4}})
                    db.execute("update jobs set state='done'")
                    db.execute("""insert into feed_rounds(feed,iface,created,kind,state)
                        values(1,'lo',?,'scheduled','ready')""",(time.time(),))
                relay.poll()
                relay.poll()
            with database(bridge) as db:
                reports = db.execute("select text from outgoing where id like 'round:%' order by id").fetchall()
                self.assertEqual(len(reports),2)
                self.assertIn("Upload: 4.0 Mbps",reports[0]["text"])
                self.assertIn("Scheduled test in Iran",reports[1]["text"])
                self.assertNotIn("private-feed",str([row["text"] for row in reports]))
            with patch("lab.telegram_agent.exchange",side_effect=RelayError("relay_unavailable")):
                relay.poll()
            self.assertEqual(relay.status()["error"],"relay_unavailable")
