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

from cryptography.fernet import Fernet

from lab.config import parse
from lab.feeds import refresh
from lab.store import Store
from lab import telegram_remote
from lab.telegram_agent import Relay, RelayError, Setup, pinned_host, provision
from lab.telegram_bridge import SCHEMA, apply_update, database, synchronize
from tests.test_lab import URI


def update(number, chat, text):
    return {"update_id": number, "message": {"chat": {"id": chat, "type": "private"},
                                             "from": {"id": chat}, "text": text}}


class TelegramRelayTests(unittest.TestCase):
    def test_ssh_fingerprint_pins_only_matching_key(self):
        key = b"fixture-key"
        encoded = base64.b64encode(key).decode()
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key).digest()).decode().rstrip("=")
        with patch("lab.telegram_agent.child", return_value=f"host ssh-ed25519 {encoded}\n".encode()):
            self.assertEqual(pinned_host("198.51.100.4",2222,fingerprint),
                             f"[198.51.100.4]:2222 ssh-ed25519 {encoded}\n")
            with self.assertRaisesRegex(RelayError, "relay_fingerprint_mismatch"):
                pinned_host("198.51.100.4",2222,"SHA256:"+"A"*43)

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
