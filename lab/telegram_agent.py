"""Provision the foreign Telegram relay and synchronize over a restricted SSH key."""
import base64
from contextlib import contextmanager
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sqlite3
import statistics
import subprocess
import tempfile
import time

from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from pydantic import BaseModel, Field, SecretStr, ValidationError

from .feeds import ACTIVE_ROUND, MAX_PERIOD_HOURS, queue_round, set_relay_schedule


REMOTE_USER = "mobocloud-relay"
ACK_ERRORS = {
    "missing": "The subscription is unavailable.",
    "interface_unavailable": "The network interface in Iran is unavailable.",
    "subscription_speed_policy_disabled": "Subscription speed testing is disabled on Mobocloud.",
    "subscription_ping_disabled": "Live ping testing is disabled on Mobocloud.",
    "subscription_ping_schedule_disabled": "Enable live testing and subscription scheduling on Mobocloud first.",
}
SETUP_ERRORS = {
    b"relay_remote_permissions", b"relay_remote_directory_conflict", b"relay_remote_health",
    b"relay_telegram_unavailable", b"relay_telegram_webhook", b"relay_telegram_chat",
}


class RelayError(ValueError):
    pass


class HostTarget(BaseModel):
    server: str = Field(min_length=1, max_length=45)
    ssh_port: int = Field(default=22, ge=1, le=65535)


class Setup(HostTarget):
    username: str = Field(default="root", pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    api_port: int = Field(default=8876, ge=1024, le=65535)
    fingerprint: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")
    password: SecretStr = Field(default=SecretStr(""), max_length=512)
    private_key: SecretStr = Field(default=SecretStr(""), max_length=32768)
    bot_token: SecretStr = Field(max_length=121)
    chat_id: int = Field(gt=0, le=2**52 - 1)


def setup(db):
    db.execute("""create table if not exists telegram_link(
        id integer primary key check(id=1), secret blob not null, last_ok real, error text)""")
    db.execute("""create table if not exists telegram_commands(
        link text, id integer, round_id integer, reply text not null, acknowledged integer default 0,
        primary key(link,id))""")
    db.execute("""create table if not exists telegram_reports(
        link text, round_id integer, primary key(link,round_id))""")


def child(args, *, payload=None, timeout=20, env=None, error="relay_unavailable"):
    try:
        call = subprocess.run(args, input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=timeout, check=True, env=env,
                              **({"stdin": subprocess.DEVNULL} if payload is None else {}))
    except (OSError, subprocess.SubprocessError):
        raise RelayError(error) from None
    return call.stdout


def public_server(server):
    try:
        address = ipaddress.ip_address(server.strip())
    except ValueError:
        raise RelayError("relay_invalid_server") from None
    if not address.is_global or address.is_multicast or "%" in str(address):
        raise RelayError("relay_invalid_server")
    return str(address)


def host_key_fingerprint(encoded):
    digest = hashlib.sha256(base64.b64decode(encoded, validate=True)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def scan_host_keys(server, port):
    keys = child(["ssh-keyscan", "-T", "6", "-p", str(port), server],
                 timeout=12, error="relay_ssh_unreachable")
    host = server if port == 22 else f"[{server}]:{port}"
    found = {}
    for line in keys.decode("ascii", errors="ignore").splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[0].startswith("#"):
            continue
        _, algorithm, encoded = fields
        if not re.fullmatch(r"[a-zA-Z0-9@._+-]+", algorithm):
            continue
        try:
            fingerprint = host_key_fingerprint(encoded)
        except ValueError:
            continue
        found[fingerprint] = (algorithm, f"{host} {algorithm} {encoded}\n")
    if not found:
        raise RelayError("relay_ssh_unreachable")
    return found


def pinned_host(server, port, fingerprint):
    found = scan_host_keys(server, port)
    if fingerprint in found:
        return found[fingerprint][1]
    raise RelayError("relay_fingerprint_mismatch")


def ssh(config, user, known_hosts):
    return ["ssh","-F","/dev/null","-T","-p",str(config["ssh_port"]),"-l",user,
            "-o","ClearAllForwardings=yes","-o","ConnectTimeout=7","-o","ServerAliveInterval=5",
            "-o","ServerAliveCountMax=2","-o","StrictHostKeyChecking=yes",
            "-o","GlobalKnownHostsFile=/dev/null","-o",f"UserKnownHostsFile={known_hosts}",
            "-o","IdentityAgent=none","-o","IdentitiesOnly=yes","-o","NumberOfPasswordPrompts=1"]


def secret_file(path, value, mode=0o600):
    with path.open("x") as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(value)
    return path


def provision(form):
    server = public_server(form.server)
    password, key = form.password.get_secret_value(), form.private_key.get_secret_value()
    if bool(password) == bool(key) or any(c in password for c in "\r\n\0"):
        raise RelayError("relay_ssh_credentials")
    token = form.bot_token.get_secret_value()
    if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", token):
        raise RelayError("relay_invalid_token")
    known = pinned_host(server, form.ssh_port, form.fingerprint)
    configuration = dict(server=server, ssh_port=form.ssh_port, api_port=form.api_port,
                         known=known, link_id=secrets.token_hex(16), secret=secrets.token_hex(32),
                         since=time.time())
    with tempfile.TemporaryDirectory(prefix="mobocloud-relay-") as tmp:
        directory = Path(tmp)
        known_hosts = secret_file(directory/"known_hosts", known)
        transport = directory/"transport"
        child(["ssh-keygen","-q","-t","ed25519","-N","","-C","mobocloud-relay","-f",str(transport)],
              error="relay_ssh_tools_unavailable")
        configuration["key"] = transport.read_text()
        command = ssh(configuration, form.username, known_hosts)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
        if key:
            identity = secret_file(directory/"admin", key.strip()+"\n")
            command += ["-o","BatchMode=yes","-i",str(identity)]
        else:
            path = secret_file(directory/"password", password+"\n")
            askpass = secret_file(directory/"askpass", '#!/bin/sh\nexec /bin/cat "$MOBO_PASSWORD_FILE"\n', 0o700)
            env.update(SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force", MOBO_PASSWORD_FILE=str(path))
            command += ["-o","PreferredAuthentications=password","-o","PubkeyAuthentication=no"]
        remote = shlex.join(["/usr/bin/python3", "-c", Path(__file__).with_name("telegram_remote.py").read_text()])
        if form.username != "root":
            remote = "sudo -n "+remote
        payload = dict(config=dict(link_id=configuration["link_id"], secret=configuration["secret"],
                                   token=token, chat=form.chat_id, port=form.api_port),
                       source=Path(__file__).with_name("telegram_bridge.py").read_text(),
                       transport_key=transport.with_suffix(".pub").read_text().strip())
        output = child(command + [server, remote], payload=json.dumps(payload).encode(), timeout=90,
                       env=env, error="relay_setup_failed")
        output = output.strip()
        if output in SETUP_ERRORS:
            raise RelayError(output.decode())
        if output != b"mobocloud-relay-ready":
            raise RelayError("relay_setup_failed")
    try:
        exchange(configuration, {"feeds": [], "reports": [], "acks": []})
    except RelayError:
        raise RelayError("relay_transport_unavailable") from None
    return configuration


def exchange(config, payload):
    with tempfile.TemporaryDirectory(prefix="mobocloud-ssh-") as tmp:
        directory = Path(tmp)
        key = secret_file(directory/"key", config["key"])
        known = secret_file(directory/"known_hosts", config["known"])
        args = ssh(config, REMOTE_USER, known) + ["-o","BatchMode=yes","-i",str(key),config["server"],"sync"]
        output = child(args, payload=json.dumps(dict(payload,secret=config["secret"])).encode())
    try:
        result = json.loads(output)
        if not all(isinstance(result[key], list) for key in ("accepted", "acknowledged", "commands")):
            raise ValueError
        return result
    except (ValueError, KeyError, TypeError):
        raise RelayError("relay_response_invalid") from None


def safe_name(value):
    return " ".join(re.sub(r"\S+://\S+", "[link hidden]", value).split())[:80] or "Subscription"


def report_text(db, row):
    observations = db.execute("""select json_extract(s.result,'$.ping_ms') ping,
        json_extract(s.result,'$.mbps') download, json_extract(s.result,'$.upload.mbps') upload,
        json_extract(s.result,'$.ok') download_ok, json_extract(s.result,'$.upload.ok') upload_ok
        from feed_jobs f join jobs j on j.id=f.job left join samples s on s.job=j.id where f.round=?""",
        (row["id"],)).fetchall()
    answered = sum(sample["ping"] is not None or bool(sample["download_ok"] or sample["upload_ok"])
                   for sample in observations)
    metrics = [("ping", "Ping", "ms", ".0f")]
    if not row["ping_only"]:
        metrics.extend([("download", "Download", "Mbps", ".1f"), ("upload", "Upload", "Mbps", ".1f")])
    values=[]
    for key, title, unit, precision in metrics:
        measured=[sample[key] for sample in observations if sample[key] is not None]
        values.append(f"{title}: {statistics.median(measured):{precision}} {unit}" if measured else f"{title}: unavailable")
    description = "Scheduled" if row["kind"] == "scheduled" else "Manual"
    mode = "ping test" if row["ping_only"] else "test"
    lines=[f"Mobocloud · {safe_name(row['name'])} · round #{row['id']}", f"{description} {mode} in Iran",
           f"{answered}/{len(observations)} configurations responded · {row['rejected']} rejected · {row['skipped']} skipped",
           "Median of measured results", *values]
    if row["error"]:
        lines.append("Round error: "+row["error"])
    return "\n".join(lines)


class Relay:
    def __init__(self, store):
        self.store = store

    def status(self):
        rows = self.store.rows("select last_ok,error from telegram_link where id=1")
        return dict(configured=bool(rows), **(rows[0] if rows else {}))

    def host_key(self, target):
        server = public_server(target.server)
        found = scan_host_keys(server, target.ssh_port)
        previous = None
        rows = self.store.rows("select secret from telegram_link where id=1")
        if rows:
            try:
                saved = json.loads(self.store.cipher.decrypt(rows[0]["secret"]))
                if (saved["server"], saved["ssh_port"]) == (server, target.ssh_port):
                    previous = host_key_fingerprint(saved["known"].split()[2])
            except (InvalidToken, ValueError, TypeError, KeyError, IndexError):
                pass

        if previous in found:
            return dict(fingerprint=previous, algorithm=found[previous][0], trusted=True, changed=False)
        fingerprint = next((fp for fp, (algorithm, _) in found.items() if algorithm == "ssh-ed25519"),
                           next(iter(found)))
        return dict(fingerprint=fingerprint, algorithm=found[fingerprint][0], trusted=False, changed=previous is not None)

    def connect(self, form):
        lock_path = self.store.path.with_name("telegram-setup.lock")
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RelayError("relay_setup_in_progress") from None
            config = provision(form)
            credential = self.store.cipher.encrypt(json.dumps(config).encode())
            with self.store.db(True) as db:
                db.execute("""insert into telegram_link values(1,?,?,null)
                    on conflict(id) do update set secret=excluded.secret,last_ok=excluded.last_ok,error=null""",
                           (credential,time.time()))
                db.execute("delete from telegram_commands")
                db.execute("delete from telegram_reports")

    def poll(self):
        rows = self.store.rows("select secret from telegram_link where id=1")
        if not rows:
            return
        credential = rows[0]["secret"]
        try:
            try:
                config = json.loads(self.store.cipher.decrypt(credential))
            except InvalidToken:
                raise RelayError("relay_credentials_invalid") from None
            link = config["link_id"]
            with self.store.db() as db:
                feeds = [dict(row, name=safe_name(row["name"])) for row in db.execute("""
                    select id,name,ping_hours,case when enabled then hours else 0 end speed_hours
                    from feeds where not deleted order by id limit 150""")]
                rounds = db.execute(f"""select r.*,f.name from feed_rounds r join feeds f on f.id=r.feed
                    where r.created>=? and not ({ACTIVE_ROUND}) and
                    (r.kind='scheduled' or exists(select 1 from telegram_commands c where c.link=? and c.round_id=r.id))
                    and not exists(select 1 from telegram_reports t where t.link=? and t.round_id=r.id)
                    order by r.id limit 8""",(config["since"],link,link)).fetchall()
                reports = [dict(id=row["id"],text=report_text(db,row)) for row in rounds]
                acks = [dict(row) for row in db.execute(
                    "select id,reply as text from telegram_commands where link=? and acknowledged=0 order by id limit 25",
                    (link,))]
            reply = exchange(config, dict(feeds=feeds,reports=reports,acks=acks,schedule_version=1))
            with self.store.db(True) as db:
                if not db.execute("update telegram_link set last_ok=?,error=null where secret=?",
                                  (time.time(),credential)).rowcount:
                    return
                accepted = set(reply["accepted"])
                for row in reports:
                    if row["id"] in accepted:
                        db.execute("insert or ignore into telegram_reports values(?,?)",(link,row["id"]))
                for command in reply["acknowledged"]:
                    db.execute("update telegram_commands set acknowledged=1 where link=? and id=?",(link,command))
                for item in reply["commands"][:25]:
                    self.queue(db,link,item)
        except (ValueError, TypeError, KeyError) as exc:
            error = str(exc) if isinstance(exc, RelayError) else "relay_response_invalid"
            with self.store.db(True) as db:
                db.execute("update telegram_link set error=? where secret=?",(error,credential))

    @staticmethod
    def queue(db, link, item):
        command, feed = item["id"], item["feed"]
        if type(command) is not int or type(feed) is not int or not (0 <= command < 2**52 and 0 < feed < 2**52):
            raise RelayError("relay_response_invalid")
        if db.execute("select 1 from telegram_commands where link=? and id=?",(link,command)).fetchone():
            return
        operation = item.get("operation", "speed")
        if operation not in ("speed", "ping", "speed_schedule", "ping_schedule"):
            raise RelayError("relay_response_invalid")
        ping_only = operation.startswith("ping")
        rid = None
        try:
            if operation.endswith("_schedule"):
                hours = item.get("hours")
                if type(hours) is not int or not 0 <= hours <= MAX_PERIOD_HOURS:
                    reply = f"Choose 1 to {MAX_PERIOD_HOURS} hours, or 0 to turn off the schedule."
                else:
                    set_relay_schedule(db,feed,ping_only=ping_only,hours=hours)
                    mode = "Ping" if ping_only else "Speed"
                    reply = f"{mode} for #{feed}: every {hours} h." if hours else f"{mode} schedule for #{feed} is off."
            else:
                result = queue_round(db,feed,cloudflare_upload=not ping_only,ping_only=ping_only)
                rid = result["id"]
                mode = db.execute("select ping_only from feed_rounds where id=?", (rid,)).fetchone()["ping_only"]
                if bool(mode) != ping_only:
                    rid = None
                    reply = "Another test is running for this subscription. Try again after it finishes."
                elif result["queued"]:
                    reply = f"Round #{rid} queued on Mobocloud in Iran."
                else:
                    reply = f"Round #{rid} is already in progress."
        except HTTPException as exc:
            reply = ACK_ERRORS.get(exc.detail, "Mobocloud could not apply this request.")
        db.execute("insert into telegram_commands(link,id,round_id,reply) values(?,?,?,?)",(link,command,rid,reply))

    def loop(self, stop):
        while not stop.is_set():
            try:
                self.poll()
            except sqlite3.Error:
                pass
            stop.wait(8)


def routes(api, relay):
    @api.get("/telegram")
    def status():
        return relay.status()

    @api.post("/telegram/host-key")
    def host_key(target:HostTarget):
        try:
            return relay.host_key(target)
        except RelayError as exc:
            raise HTTPException(422,str(exc)) from None

    @api.post("/telegram/setup")
    def connect(form:Setup):
        try:
            relay.connect(form)
        except RelayError as exc:
            raise HTTPException(422,str(exc)) from None
        return relay.status()


if __name__ == "__main__":
    from .main import DATA, KEY
    from .store import Store

    try:
        print("Mobocloud Telegram Relay")
        server = input("Foreign server public IP: ").strip()
        username = input("SSH username [root]: ").strip() or "root"
        ssh_port = input("SSH port [22]: ").strip() or "22"
        api_port = input("Loopback API port [8876]: ").strip() or "8876"
        identity = json.loads((KEY/"identity.json").read_text())
        relay = Relay(Store(DATA/"lab.sqlite",identity["key"].encode()))
        target = relay.host_key(HostTarget(server=server, ssh_port=ssh_port))
        fingerprint = target["fingerprint"]
        if not target["trusted"]:
            if target["changed"]:
                print("The saved SSH host key has changed.")
            print(f"SSH host key for {server}:{ssh_port} ({target['algorithm']}): {fingerprint}")
            if input("Compare with your server console. Use this host key? [y/N]: ").strip().lower() not in ("y", "yes"):
                raise SystemExit("Cancelled.")
        key_path = input("Admin SSH private key file (Enter for SSH password): ").strip()
        key = Path(key_path).expanduser().read_text() if key_path else ""
        password = "" if key else getpass.getpass("SSH password: ")
        token = getpass.getpass("Telegram bot token: ")
        chat = input("Numeric Telegram private chat ID: ").strip()
        form = Setup(server=server,username=username,ssh_port=ssh_port,api_port=api_port,
                     fingerprint=fingerprint,password=password,private_key=key,bot_token=token,chat_id=chat)
        relay.connect(form)
        print("Telegram relay connected. Open the bot and send /test.")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("Cancelled.") from None
    except RelayError as exc:
        raise SystemExit(f"Telegram setup failed: {exc}") from None
    except (OSError, ValidationError, ValueError):
        raise SystemExit("Telegram setup failed. Check the values and local files.") from None
