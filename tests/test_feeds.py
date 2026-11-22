import base64
from contextlib import nullcontext
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from cryptography.fernet import Fernet
from lab.store import Store
from lab.feeds import refresh,entries
from lab.uplink import connect,tunnel
from lab.config import parse,parse_document,peer
from lab.worker import Worker
from lab.runner import Failure
from tests.test_lab import URI


class FeedTests(unittest.TestCase):
    def test_mixed_list(self):
        body=base64.b64encode((URI+"#Turkey\nv mess\n"+URI+"#Sweden").encode()).decode()
        result,rejected=entries(body)
        self.assertEqual(([name for name,_ in result],rejected),(["Turkey","Sweden"],1))

    def test_tcp_http_shadowsocks_2022_and_reality_feed(self):
        vless = URI.split("?")[0]
        method = "2022-blake3-aes-256-gcm"
        password = base64.b64encode(bytes(range(32))).decode()
        auth = base64.urlsafe_b64encode(f"{method}:{password}".encode()).decode()
        reality_key = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
        lines = [
            vless + "?type=tcp&headerType=http&host=example.org&path=%2Fprobe",
            f"ss://{method}:{password}@example.org:443",
            f"ss://{auth}@example.org:8443",
            vless + "?type=tcp&headerType=none&security=reality&alpn=h2&fp=chrome"
            f"&pbk={reality_key}&sid=abcd&sni=example.org",
        ]
        lines.extend(vless.replace("example.org", address)
                     for address in ("10.0.0.1", "127.0.0.1", "192.168.1.4"))
        accepted, rejected = entries("\n".join(lines))
        self.assertEqual(rejected, 3)
        tcp, ss_inline, ss_encoded, reality = [out for _, out in accepted]
        self.assertEqual(tcp["streamSettings"]["tcpSettings"]["header"],
                         {"type": "http", "request": {"headers": {"Host": ["example.org"]},
                                                      "path": ["/probe"]}})
        for out in (ss_inline, ss_encoded):
            self.assertEqual((peer(out)["method"], peer(out)["password"]), (method, password))
        self.assertEqual(reality["streamSettings"]["realitySettings"],
                         {"serverName": "example.org", "fingerprint": "chrome",
                          "publicKey": reality_key, "shortId": "abcd"})
        for out in (tcp, ss_inline, ss_encoded, reality):
            self.assertEqual(parse_document(json.dumps(out)), [out])
        self.assertEqual(entries(vless + "?type=ws&headerType=http"), ([], 1))
        self.assertEqual(entries(vless + "?security=none&pbk=" + reality_key), ([], 1))

    @patch.dict(os.environ,LAB_SPEED="1",LAB_LIVE="1",LAB_SCHEDULE="1",LAB_DOWNLOAD_BYTES="1000",LAB_DOWNLOAD_URL="https://example.org/download",LAB_CHECK_URL="https://example.org/check")
    def test_refresh_is_periodic_and_replaces_members(self):
        with tempfile.TemporaryDirectory() as tmp,patch("lab.feeds.require_link"),patch("lab.feeds.download") as dl:
            store=Store(Path(tmp)/"db",Fernet.generate_key())
            with store.db(True) as db:
                db.execute("insert into feeds(name,url,iface,hours,enabled,due) values('test',?,'wlan0',12,1,0)",(store.cipher.encrypt(b"https://example.org/token"),))
            dl.return_value=URI+"#One\n"+URI.replace("example.org","vpn.example.org")+"#Two\n"+URI+"#Duplicate\nnot-supported"
            refresh(store)
            refresh(store)
            dl.assert_called_once_with("https://example.org/token","wlan0")
            self.assertEqual(len(store.rows("select * from feed_jobs")),2)
            self.assertEqual(store.rows("select rejected from feeds")[0]["rejected"],1)
            with store.db(True) as db:
                db.execute("update jobs set state='done'")
                db.execute("update feeds set due=0")
            dl.return_value=URI+"#One"
            refresh(store)
            self.assertEqual([x["label"] for x in store.rows("select r.label from feed_jobs r join jobs j on j.id=r.job where j.state='queued'")],["One"])

    def test_relay_through_kernel_bound_loopback(self):
        out=parse(URI)
        with socket.socket() as server,patch("lab.uplink.require_link"):
            server.bind(("127.0.0.1",0))
            server.listen(1)
            peer(out).update(address="127.0.0.1",port=server.getsockname()[1])
            with tunnel(out,"lo") as bound:
                dst=peer(bound)
                with socket.create_connection((dst["address"],dst["port"]),timeout=2) as client:
                    server.settimeout(2)
                    upstream,_=server.accept()
                    with upstream:
                        client.sendall(b"test")
                        self.assertEqual(upstream.recv(4),b"test")
                        upstream.sendall(b"back")
                        self.assertEqual(client.recv(4),b"back")

    def test_missing_interface_fails_closed(self):
        with self.assertRaises(OSError):
            connect("127.0.0.1",443,"missing123")


class RoundTests(unittest.IsolatedAsyncioTestCase):
    @patch.dict(os.environ, LAB_SPEED="1", LAB_LIVE="1", LAB_SCHEDULE="1",
                LAB_DOWNLOAD_URL="https://example.org/download", LAB_UPLOAD_URL="https://example.org/upload",
                LAB_CHECK_URL="https://example.org/check", LAB_DAY_CAP="0", LAB_CONFIG_CAP="0",
                LAB_DOWNLOAD_BYTES="100000", LAB_UPLOAD_BYTES="200000")
    def test_subscription_ignores_daily_caps_and_can_be_deleted(self):
        from fastapi import APIRouter, HTTPException
        from lab.feeds import Plan, routes

        with tempfile.TemporaryDirectory() as tmp, patch("lab.feeds.require_link"), \
                patch("lab.feeds.download", return_value=URI + "#Fixture"):
            store = Store(Path(tmp) / "db", Fernet.generate_key())
            api = APIRouter()
            routes(api, store)
            endpoints = {route.endpoint.__name__: route.endpoint for route in api.routes}
            fid = endpoints["save"](Plan(iface="lo", hours=12, url="https://example.org/fixture"))["id"]
            endpoints["run_now"](fid)

            refresh(store)
            worker = Worker(store)
            with store.db(True) as db:
                db.execute("update jobs set created=0")
            job = worker.claim()
            self.assertEqual((job["reserved"], job["subscription_test"]), (0, 1))
            with patch("lab.uplink.require_link"), patch("lab.worker.pinned", return_value=parse(URI)), \
                    patch("lab.uplink.tunnel", return_value=nullcontext(parse(URI))), \
                    patch("lab.worker.proxy", return_value=nullcontext(None)), \
                    patch("lab.worker.probe", side_effect=[
                        Failure("fixture_baseline_failure"), {"ok": True, "ttfb_ms": 523.5, "response_ms": 23.5},
                        {"ok": True, "mbps": 12.5},
                        {"ok": True, "mbps": 6.5}, Failure("fixture_exit_failure"),
                        {"ok": True},
                        *[Failure("fixture_ping_failure") for _ in range(5)],
                        {"ok": True, "transfer_ms": 50},
                        {"ok": True, "transfer_ms": 100},
                        Failure("timeout"),
                        *[Failure("fixture_upload_failure") for _ in range(3)],
                        Failure("fixture_exit_failure"),
                    ]) as probe, patch.dict(os.environ, LAB_IP_URL="https://example.org/ip",
                                              LAB_IP_FALLBACK_URL=""):
                worker.test(job)
                endpoints["run_now"](fid, cloudflare_upload=True)
                refresh(store)
                worker.test(worker.claim())
            ping, down, up = probe.call_args_list[1:4]
            self.assertEqual(ping.args, ("https://example.org/check", None))
            self.assertEqual(down.args[2:], ("download", 100000, 100000))
            self.assertEqual(up.args[2:4], ("upload", 200000))
            self.assertEqual(up.args[0], "https://example.org/upload")
            baseline, ping, down, up = [probe.call_args_list[i] for i in (5, 6, 11, 14)]
            self.assertEqual(baseline.args, ("https://speed.cloudflare.com/__down?bytes=0",))
            self.assertEqual(ping.args, ("https://speed.cloudflare.com/__down?bytes=0", None))
            self.assertEqual(down.args, ("https://speed.cloudflare.com/__down?bytes=100000", None, "download", 100000, 100000))
            self.assertEqual(up.args, ("https://speed.cloudflare.com/__up", None, "upload", 200000, 65536))
            self.assertEqual((ping.kwargs["timeout"], down.kwargs["timeout"], up.kwargs["timeout"]), (30, 60, 60))
            self.assertEqual(down.kwargs["connect_timeout"], 20)
            failed, succeeded = (batch["results"][0] for batch in endpoints["rounds"]()["rounds"])
            self.assertEqual((succeeded["ping_ms"], succeeded["mbps"], succeeded["upload_mbps"]), (23.5, 12.5, 6.5))
            self.assertEqual(succeeded["ping_method"], "https_response")
            self.assertEqual((failed["ping_ms"], failed["ping_error"], failed["mbps"],
                              failed["upload_mbps"], failed["upload_error"]),
                             (None, "fixture_ping_failure", 12, None, "fixture_upload_failure"))
            self.assertEqual((failed["ping_sample_ok"], failed["ping_sample_total"],
                              failed["download_sample_ok"], failed["download_sample_total"],
                              failed["upload_sample_ok"], failed["upload_sample_total"]), (0, 5, 2, 3, 0, 3))

            endpoints["run_now"](fid)
            refresh(store)
            self.assertEqual(store.rows("select cloudflare_upload from feed_rounds order by id"),
                             [{"cloudflare_upload":0}, {"cloudflare_upload":1}, {"cloudflare_upload":0}])
            delete_route = next(route for route in api.routes if route.endpoint is endpoints["delete_feed"])
            self.assertEqual((delete_route.path, delete_route.methods), ("/subscriptions/{fid}", {"DELETE"}))
            self.assertEqual(delete_route.endpoint(fid), {"ok":True})
            self.assertEqual(delete_route.endpoint(fid), {"ok":True})
            self.assertEqual(endpoints["list_feeds"](summary=True)["feeds"], [])
            self.assertEqual(store.rows("select length(url) size,enabled from feeds"), [{"size":0,"enabled":0}])
            self.assertEqual(store.rows("select state,reserved from jobs order by id"), [
                {"state":"done","reserved":0}, {"state":"done","reserved":0}, {"state":"cancelled","reserved":0}
            ])
            self.assertEqual(len(endpoints["rounds"]()["rounds"]), 3)
            with self.assertRaises(HTTPException) as missing:
                endpoints["run_now"](fid)
            self.assertEqual(missing.exception.status_code, 404)

    @patch.dict(os.environ, LAB_SPEED="1", LAB_LIVE="1", LAB_SCHEDULE="1",
                LAB_DOWNLOAD_BYTES="1000", LAB_DOWNLOAD_URL="https://example.org/download",
                LAB_CHECK_URL="https://example.org/check")
    async def test_manual_round_schedule_progress_and_history(self):
        import asyncio
        import sqlite3
        import time
        import httpx
        from lab import main

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            password = "fixture-password-123"
            with patch.object(main, "DATA", path), patch.object(main, "KEY", path), \
                    patch("getpass.getpass", return_value=password):
                main.initialize()
                app = main.create()
            identity = json.loads((path / "identity.json").read_text())
            store = Store(path / "lab.sqlite", identity["key"].encode())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://localhost",
                                         headers={"X-Lab-Request":"1"}) as client:
                root = "/api/v1/subscriptions"
                self.assertEqual((await client.get(root)).status_code, 401)
                self.assertEqual((await client.post("/api/v1/login", json={"password":password})).status_code, 200)
                with patch("lab.feeds.require_link"), patch("lab.feeds.download") as download:
                    saved = (await client.post(root, json={"iface":"test0", "hours":12,
                                   "url":"https://example.org/fixture"})).json()
                    f = root + "/" + str(saved["id"])
                    self.assertGreater(saved["due"], time.time() + 43190)
                    requests = await asyncio.gather(client.post(f + "/run", json={}),
                                                    client.post(f + "/run", json={}))
                    replies = [response.json() for response in requests]
                    self.assertEqual(sum(reply["queued"] for reply in replies), 1)
                    self.assertEqual(replies[0]["id"], replies[1]["id"])
                    first = replies[0]["id"]
                    download.return_value = URI + "#One\n" + URI.replace("example.org", "two.example.org") + "#Two"
                    refresh(store)
                    self.assertFalse((await client.post(f + "/run", json={})).json()["queued"])
                    self.assertEqual(store.rows("select due from feeds")[0]["due"], saved["due"])
                    self.assertEqual(download.call_count, 1)
                    with store.db(True) as db:
                        job = db.execute("select * from jobs order by id limit 1").fetchone()
                        store.record(db, job, {"ok":True}, {"ok":True, "mbps":12.5})
                        db.execute("update jobs set state='done' where id=?", (job["id"],))
                    progress = (await client.get(root + "?summary=true")).json()["feeds"][0]["progress"]
                    self.assertEqual((progress["done"], progress["total"]), (1, 2))
                    self.assertEqual((await client.get(root + "/history")).json()["count"], 0)
                    with store.db(True) as db:
                        job = db.execute("select * from jobs where state='queued'").fetchone()
                        store.record(db, job, {"ok":True}, {"ok":False, "error":"fixture_failure"})
                        db.execute("update jobs set state='done' where id=?", (job["id"],))
                    results = (await client.get(root + "/rounds")).json()["rounds"][0]["results"]
                    self.assertEqual([(row["mbps"],row["error"]) for row in results],
                                     [(12.5,None),(None,"fixture_failure")])

                    self.assertEqual((await client.patch(f + "?enabled=false", json={})).status_code, 200)
                    self.assertEqual(store.rows("select enabled from feeds"), [{"enabled":0}])
                    self.assertEqual((await client.put(f + "/period", json={"hours":24})).status_code, 200)
                    self.assertEqual((await client.patch(f + "?enabled=true", json={})).status_code, 200)
                    resumed = store.rows("select due,hours,enabled from feeds")[0]
                    self.assertEqual((resumed["hours"],resumed["enabled"]), (24,1))
                    self.assertGreater(resumed["due"], time.time() + 86390)
                    second = (await client.post(f + "/run", json={})).json()["id"]
                    refresh(store)
                    other = (await client.post(root, json={"iface":"test0","hours":24,"name":"Other",
                                   "url":"https://example.org/other"})).json()["id"]
                    with store.db(True) as db:
                        other_round = db.execute("insert into feed_rounds(feed,iface,created,kind,state) values"
                                                 "(?,'test0',1,'manual','ready')",(other,)).lastrowid
                        job = db.execute("insert into jobs(agent,kind,state,created,reserved,error) values"
                                         "('local','download','error',?,123,'fixture_failure')",
                                         (int(time.time()),)).lastrowid
                        db.execute("insert into feed_jobs(job,feed,iface,label,round) values"
                                   "(?,?,'test0','Other config',?)",(job,other,other_round))
                    keep = store.rows("select * from feeds order by id")
                    reservation = store.rows("select id,reserved from jobs order by id")
                    preview = await client.get(root + f"/history?fid={saved['id']}")
                    self.assertEqual(preview.json(), {"count":2})
                    self.assertEqual((await client.request("DELETE", root + "/history?count=1", json={})).status_code, 409)
                    cleared = await client.request("DELETE", root + f"/history?fid={saved['id']}&count=2", json={})
                    self.assertEqual(cleared.json(), {"deleted":2})
                    ids = [batch["id"] for batch in (await client.get(root + "/rounds")).json()["rounds"]]
                    self.assertNotIn(first, ids)
                    self.assertIn(second, ids)
                    self.assertEqual(len(ids), 2)
                    self.assertEqual(store.rows("select * from feeds order by id"), keep)
                    self.assertEqual(store.rows("select id,reserved from jobs order by id"), reservation)
                    self.assertEqual(store.rows("select count(*) n from samples"), [{"n":0}])
                    self.assertEqual(store.rows("select count(*) n from jobs where state='queued'"), [{"n":2}])
                    self.assertEqual((await client.get(root + "/history")).json(), {"count":1})
                    cleared = await client.request("DELETE", root + "/history?count=1", json={})
                    self.assertEqual(cleared.json(), {"deleted":1})
                    self.assertEqual(Store(path / "lab.sqlite", identity["key"].encode())
                                     .rows("select id,reserved from jobs order by id"), reservation)
                    with sqlite3.connect(path / "lab.sqlite") as db:
                        self.assertEqual(db.execute("select id from feed_rounds").fetchall(), [(second,)])
                        self.assertEqual(db.execute("select distinct round from feed_jobs").fetchall(), [(second,)])

    def test_legacy_jobs_get_round_ids_on_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db"
            key = Fernet.generate_key()
            store = Store(path, key)
            with store.db(True) as db:
                db.execute("drop index feed_jobs_round")
                db.execute("alter table feed_jobs drop column round")
                feed = db.execute("insert into feeds(name,url,iface,hours,enabled,due) values"
                                  "('Fixture',?,'lo',12,1,0)", (b"fixture",)).lastrowid
                for created in (100, 101, 1000):
                    job = db.execute("insert into jobs(agent,kind,state,created) values"
                                     "('local','download','done',?)", (created,)).lastrowid
                    db.execute("insert into feed_jobs(job,feed,iface,label) values(?,?,'lo','Fixture')",
                               (job,feed))
            for _ in range(2):
                reopened = Store(path, key)
                self.assertEqual(reopened.rows("select seen from feed_rounds order by id"), [{"seen":2},{"seen":1}])

    @patch.dict(os.environ, LAB_SPEED="1", LAB_LIVE="1", LAB_SCHEDULE="1",
                LAB_DOWNLOAD_BYTES="1000", LAB_DOWNLOAD_URL="https://example.org/download",
                LAB_CHECK_URL="https://example.org/check")
    def test_skipped_results_are_visible_and_counted(self):
        from fastapi import APIRouter
        from lab.feeds import routes
        with tempfile.TemporaryDirectory() as tmp, patch("lab.feeds.require_link"), \
                patch("lab.feeds.download", return_value=URI + "#Fixture"):
            store = Store(Path(tmp) / "db", Fernet.generate_key())
            with store.db(True) as db:
                db.execute("insert into feeds(name,url,iface,hours,enabled,due) values"
                           "('Fixture',?,'lo',12,1,0)",
                           (store.cipher.encrypt(b"https://example.org/fixture"),))
                cid = store.add(db, parse(URI), "Fixture", "Other", "")
                db.execute("update configs set enabled=0 where id=?", (cid,))
            refresh(store)
            api = APIRouter()
            routes(api, store)
            endpoints = {(route.path, next(iter(route.methods))):route.endpoint for route in api.routes}
            result = endpoints["/subscriptions/rounds","GET"]()["rounds"][0]["results"][0]
            self.assertEqual((result["label"],result["state"],result["error"]),
                             ("Fixture","skipped","config_disabled_or_missing"))
            self.assertEqual(endpoints["/subscriptions/history","GET"](), {"count":1})
            self.assertEqual(endpoints["/subscriptions/history","DELETE"](count=1), {"deleted":1})
            self.assertEqual(store.rows("select * from feed_skips"), [])
