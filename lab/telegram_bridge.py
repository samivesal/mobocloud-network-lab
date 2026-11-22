"""Standalone service installed on the foreign server by Mobocloud."""
import argparse
from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler


MAX_REQUEST = 131072
SCHEMA = """
create table if not exists state(key text primary key, value text not null);
create table if not exists commands(id integer primary key, feed integer not null, answered integer not null default 0,
  operation text not null default 'speed', hours integer);
create table if not exists outgoing(id text primary key, text text not null, markup text,
  attempts integer not null default 0, due real not null default 0, sent integer not null default 0);
"""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def json_request(url, data, timeout=8):
    request = Request(url, json.dumps(data).encode(), {"Content-Type": "application/json"})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        body = response.read(MAX_REQUEST + 1)
    if len(body) > MAX_REQUEST:
        raise ValueError("response_too_large")
    return json.loads(body)


def telegram(token, method, data, timeout=8):
    response = json_request(f"https://api.telegram.org/bot{token}/{method}", data, timeout)
    if not response.get("ok"):
        raise ValueError("telegram_api_error")
    return response["result"]


@contextmanager
def database(path):
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def set_state(db, key, value):
    db.execute("insert into state values(?,?) on conflict(key) do update set value=excluded.value",
               (key, json.dumps(value)))


def get_state(db, key, default):
    row = db.execute("select value from state where key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def initialize(db):
    db.executescript(SCHEMA)
    columns = {row[1] for row in db.execute("pragma table_info(commands)")}
    if "operation" not in columns:
        db.execute("alter table commands add column operation text not null default 'speed'")
    if "hours" not in columns:
        db.execute("alter table commands add column hours integer")


def enqueue_message(db, key, text, markup=None):
    db.execute("insert or ignore into outgoing(id,text,markup) values(?,?,?)",
               (key, text[:3500], json.dumps(markup) if markup else None))


def synchronize(db, payload):
    db.execute("begin immediate")
    set_state(db, "feeds", payload["feeds"])
    set_state(db, "agent_seen", int(time.time()))
    scheduling = payload.get("schedule_version") == 1
    set_state(db, "scheduling", scheduling)
    accepted = []
    for report in payload["reports"]:
        key = report["id"]
        enqueue_message(db, f"round:{key}", report["text"])
        accepted.append(key)
    acknowledged = []
    for ack in payload["acks"]:
        command = ack["id"]
        if db.execute("update commands set answered=1 where id=? and answered=0", (command,)).rowcount:
            enqueue_message(db, f"ack:{command}", ack["text"])
        acknowledged.append(command)
    pending = db.execute("""select id,feed,operation,hours from commands
        where answered=0 and (operation='speed' or ?) order by id limit 25""", (scheduling,))
    return {"accepted": accepted, "acknowledged": acknowledged, "commands": [dict(row) for row in pending]}


def schedule_label(hours):
    return f"Every {hours} h" if hours else "Off"


def button(text, action):
    return {"text": text, "callback_data": action}


def subscriptions_menu(feeds, action):
    return [[button(f"#{feed['id']} {feed['name'][:42]}", f"{action}:{feed['id']}")] for feed in feeds[:20]]


def scheduling_menu(feed):
    fid = feed["id"]
    return [[button(f"Ping · {schedule_label(feed.get('ping_hours', 0))}", f"period:ping:{fid}")],
            [button(f"Speed · {schedule_label(feed.get('speed_hours', 0))}", f"period:speed:{fid}")],
            [button("Back to subscriptions", "schedule")]]


def period_menu(mode, fid):
    options = [button(f"{hours} h", f"set:{mode}:{fid}:{hours}") for hours in (1, 2, 4, 6, 12, 24)]
    return [options[:3], options[3:], [button("Off", f"set:{mode}:{fid}:0"), button("Back", f"schedule:{fid}")]]


def period_command(text):
    callback = re.fullmatch(r"set:(ping|speed):([0-9]{1,12}):([0-9]{1,3})", text)
    typed = re.fullmatch(r"/(ping|speed)_every ([0-9]{1,12}) ([0-9]{1,3})", text)
    match = callback or typed
    return match.groups() if match else None


def apply_update(db, update, chat):
    """Commit the authorized command and offset together before acknowledging it."""
    update_id = update["update_id"]
    callback = update.get("callback_query")
    message = callback.get("message", {}) if callback else update.get("message", {})
    sender = callback.get("from", {}) if callback else message.get("from", {})
    source = message.get("chat", {})
    permitted = (source.get("type") == "private" and source.get("id") == chat
                 and sender.get("id") == chat)
    db.execute("begin immediate")
    if update_id < get_state(db, "offset", 0):
        return None
    set_state(db, "offset", update_id + 1)
    if not permitted:
        return None
    text = callback.get("data", "") if callback else message.get("text", "")
    feeds = get_state(db, "feeds", [])
    scheduling = get_state(db, "scheduling", False)

    def send(message, buttons=None):
        enqueue_message(db, f"update:{update_id}", message, {"inline_keyboard": buttons} if buttons else None)

    if text == "/start" and scheduling:
        send("Choose an action.", [[button("Speed test", "test"), button("Ping", "ping")],
                                  [button("Schedules", "schedule")]])
    elif text in ("/start", "/subscriptions", "/test", "test", "/ping", "ping", "/schedule", "schedule"):
        action = text.strip("/")
        action = action if action in ("ping", "schedule") else "test"
        if action != "test" and not scheduling:
            send("Update the Mobocloud panel and reconnect this relay to use ping and schedules.")
        else:
            buttons = subscriptions_menu(feeds, action)
            send("Choose a subscription." if buttons else "No subscriptions have synchronized from Mobocloud yet.", buttons)
    elif text == "/status":
        active = time.time() - get_state(db, "agent_seen", 0) < 90
        send("Mobocloud in Iran is connected." if active
             else "The last connection from Mobocloud in Iran is older than 90 seconds.")
    else:
        menu = re.fullmatch(r"(?:schedule:|period:(ping|speed):)([0-9]{1,12})", text)
        run = re.fullmatch(r"/?(test|ping)(?::| )([0-9]{1,12})", text)
        set_period = period_command(text)
        if menu or run or set_period:
            mode, id_text = set_period[:2] if set_period else (menu or run).groups()
            feed = next((feed for feed in feeds if feed["id"] == int(id_text)), None)
            if not scheduling and (not run or mode != "test"):
                send("Update the Mobocloud panel and reconnect this relay to use ping and schedules.")
            elif not feed:
                send("Subscription unavailable. Open /test or /schedule to refresh the list.")
            elif menu:
                if mode:
                    send(f"Set {mode} interval. Use /{mode}_every {feed['id']} HOURS for any whole number from 1 to 168. Use 0 to turn it off.",
                         period_menu(mode, feed["id"]))
                else:
                    send(f"Schedules for #{feed['id']} {feed['name']}", scheduling_menu(feed))
            else:
                hours = int(set_period[2]) if set_period else None
                if hours is not None and hours > 168:
                    send("Choose 1 to 168 hours, or 0 to turn off the schedule.")
                else:
                    operation = f"{mode}_schedule" if set_period else ("speed" if mode == "test" else "ping")
                    db.execute("insert or ignore into commands(id,feed,operation,hours) values(?,?,?,?)",
                               (update_id,feed["id"],operation,hours))
    return callback["id"] if callback else None


def bot_loop(path, config):
    while True:
        try:
            with database(path) as db:
                pending = db.execute("select * from outgoing where sent=0 and due<=? order by rowid limit 1",
                                     (time.time(),)).fetchone()
                offset = get_state(db, "offset", 0)
            if pending:
                try:
                    data = {"chat_id": config["chat"], "text": pending["text"]}
                    if pending["markup"]:
                        data["reply_markup"] = json.loads(pending["markup"])
                    telegram(config["token"], "sendMessage", data)
                except (OSError, URLError, ValueError):
                    with database(path) as db:
                        db.execute("update outgoing set attempts=attempts+1,due=? where id=?",
                                   (time.time()+min(3600, 20 * 2**min(8, pending["attempts"])),pending["id"]))
                else:
                    with database(path) as db:
                        db.execute("update outgoing set sent=1 where id=?", (pending["id"],))
            updates = telegram(config["token"], "getUpdates",
                               {"offset": offset, "timeout": 5, "limit": 25,
                                "allowed_updates": ["message", "callback_query"]}, timeout=12)
            for update in updates:
                with database(path) as db:
                    callback = apply_update(db, update, config["chat"])
                if callback:
                    telegram(config["token"], "answerCallbackQuery", {"callback_query_id": callback})
        except (OSError, URLError, ValueError, TypeError, KeyError, sqlite3.Error):
            time.sleep(5)


def serve(config):
    path = Path(os.environ["STATE_DIRECTORY"]) / (config["link_id"] + ".sqlite")
    with database(path) as db:
        initialize(db)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, data):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self.reply(200, {"ok": True, "link_id": config["link_id"]})
            else:
                self.reply(404, {})

        def do_POST(self):
            if self.path != "/sync":
                self.reply(404, {})
                return
            self.connection.settimeout(5)
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 < length <= MAX_REQUEST:
                    raise ValueError
                payload = json.loads(self.rfile.read(length))
                if not hmac.compare_digest(payload.pop("secret"), config["secret"]):
                    self.reply(403, {})
                    return
                with database(path) as db:
                    result = synchronize(db, payload)
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                self.reply(400, {})
                return
            self.reply(200, result)

    with HTTPServer(("127.0.0.1", config["port"]), Handler) as server:
        threading.Thread(target=bot_loop, args=(path, config), daemon=True).start()
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["serve", "client"])
    parser.add_argument("port", type=int, nargs="?")
    args = parser.parse_args()
    try:
        if args.mode == "serve":
            config = json.loads((Path(os.environ["CREDENTIALS_DIRECTORY"]) / "relay.json").read_text())
            serve(config)
        else:
            payload = sys.stdin.buffer.read(MAX_REQUEST + 1)
            if len(payload) > MAX_REQUEST:
                raise ValueError("request_too_large")
            result = json_request(f"http://127.0.0.1:{args.port}/sync", json.loads(payload))
            print(json.dumps(result))
    except (OSError, URLError, ValueError, TypeError, KeyError):
        raise SystemExit("Mobocloud Telegram relay unavailable.") from None


if __name__ == "__main__":
    main()
