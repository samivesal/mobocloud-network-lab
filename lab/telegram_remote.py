"""Remote bootstrap executed with administrative SSH for the selected server."""
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import time
from urllib.request import build_opener, ProxyHandler


SERVICE = "mobocloud-telegram.service"
ACCOUNT = "mobocloud-relay"
HOME = Path("/var/lib/mobocloud-telegram-ssh")
APP = Path("/opt/mobocloud-telegram/bridge.py")
CONFIG = Path("/etc/mobocloud-telegram/relay.json")
UNIT = Path("/etc/systemd/system") / SERVICE
UNIT_TEXT = """[Unit]
Description=Mobocloud Telegram relay
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=/usr/bin/python3 /opt/mobocloud-telegram/bridge.py serve
LoadCredential=relay.json:/etc/mobocloud-telegram/relay.json
DynamicUser=yes
StateDirectory=mobocloud-telegram
StateDirectoryMode=0700
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
Restart=on-failure
RestartSec=4
[Install]
WantedBy=multi-user.target
"""


class SetupError(ValueError):
    pass


def systemctl(*args, check=True):
    return subprocess.run(["/usr/bin/systemctl", *args], check=check, timeout=20,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def put(path, body, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as staged:
        temporary = Path(staged.name)
        try:
            staged.write(body)
            staged.flush()
            os.fchmod(staged.fileno(), mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def managed_directory(path, mode):
    path.mkdir(mode=mode, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SetupError("relay_remote_directory_conflict")


def install(data):
    if os.geteuid() != 0:
        raise SetupError("relay_remote_permissions")
    config = data["config"]
    source = data["source"]
    transport_key = data["transport_key"]
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+ mobocloud-relay", transport_key):
        raise ValueError("Invalid transport key.")
    namespace = {"__name__": "mobocloud_remote_bridge"}
    exec(compile(source, str(APP), "exec"), namespace)
    telegram = namespace["telegram"]
    try:
        telegram(config["token"], "getMe", {})
        webhook = telegram(config["token"], "getWebhookInfo", {})
    except (OSError, ValueError, KeyError):
        raise SetupError("relay_telegram_unavailable") from None
    if webhook.get("url"):
        raise SetupError("relay_telegram_webhook")
    try:
        chat = telegram(config["token"], "getChat", {"chat_id": config["chat"]})
    except (OSError, ValueError, KeyError):
        raise SetupError("relay_telegram_chat") from None
    if chat.get("type") != "private" or chat.get("id") != config["chat"]:
        raise SetupError("relay_telegram_chat")

    try:
        account = pwd.getpwnam(ACCOUNT)
    except KeyError:
        subprocess.run(["/usr/sbin/useradd", "--system", "--user-group", "--home-dir", str(HOME),
                        "--no-create-home", "--shell", "/bin/sh", "--password", "*", ACCOUNT],
                       check=True, timeout=10)
        account = pwd.getpwnam(ACCOUNT)
    if account.pw_dir != str(HOME) or account.pw_shell != "/bin/sh":
        raise ValueError("Conflicting relay user.")
    managed_directory(HOME, 0o755)
    keys = HOME / ".ssh"
    managed_directory(keys, 0o755)
    auth = keys / "authorized_keys"
    managed_directory(APP.parent, 0o755)
    managed_directory(CONFIG.parent, 0o700)
    port = int(config["port"])
    files = {
        APP: (source.encode(), 0o644),
        CONFIG: (json.dumps(config).encode(), 0o600),
        UNIT: (UNIT_TEXT.encode(), 0o644),
        auth: (f'restrict,command="/usr/bin/python3 {APP} client {port}" {transport_key}\n'.encode(), 0o644),
    }
    if any(path.is_symlink() or (path.exists() and not path.is_file()) for path in files):
        raise SetupError("relay_remote_directory_conflict")
    previous = {path: path.read_bytes() if path.exists() else None for path in files}
    opener = build_opener(ProxyHandler({}))
    try:
        for path, (body, mode) in files.items():
            put(path, body, mode)
        systemctl("daemon-reload")
        systemctl("restart", SERVICE)
        for _ in range(20):
            try:
                with opener.open(f"http://127.0.0.1:{port}/health", timeout=1) as reply:
                    active = json.load(reply)
                if active.get("link_id") == config["link_id"]:
                    systemctl("enable", SERVICE)
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.25)
        raise SetupError("relay_remote_health")
    except Exception:
        systemctl("stop", SERVICE, check=False)
        for path, body in previous.items():
            if body is None:
                path.unlink(missing_ok=True)
            else:
                put(path, body, files[path][1])
        systemctl("daemon-reload", check=False)
        if previous[UNIT] is not None:
            systemctl("start", SERVICE, check=False)
        raise


if __name__ == "__main__":
    try:
        install(json.load(sys.stdin))
        print("mobocloud-relay-ready")
    except SetupError as exc:
        print(exc)
    except (OSError, RuntimeError, subprocess.SubprocessError, KeyError, ValueError):
        raise SystemExit("Mobocloud relay setup failed. Check SSH permissions, Python/systemd, port and Telegram chat.") from None
