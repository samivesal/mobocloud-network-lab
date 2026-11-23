"""Manage the dedicated HTTPS listener. Changing the router and firewall is manual."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.request

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from .manage import read_settings


CONFIG = Path("/etc/mobohub-network-lab")
ROOT = CONFIG / "remote"
STATUS = CONFIG / "remote-status.json"
RUNTIME = Path("/run/mobocloud-remote")
PROXY = "mobocloud-remote.service"
TIMER = "mobocloud-remote-renew.timer"
NGINX = "/usr/sbin/nginx"
SYSTEM_CA = Path("/etc/ssl/certs/ca-certificates.crt")


class RemoteError(ValueError):
    pass


def port(value):
    number = int(value)
    if not 1 <= number <= 65535:
        raise RemoteError("Ports must be between 1 and 65535.")
    return number


def ipv4(value, public=False):
    address = ipaddress.IPv4Address(value)
    if address.is_unspecified or address.is_multicast or address.is_loopback or address.is_link_local:
        raise RemoteError("Enter a usable IPv4 address.")
    if public and (not address.is_global or address.is_reserved):
        raise RemoteError("Enter the public IPv4 address assigned by the ISP.")
    return str(address)


def local_port():
    settings = CONFIG / "settings"
    values = read_settings(settings.read_text())[0] if settings.exists() else {}
    return port(values.get("LAB_PORT", 8765))


def local_address():
    return f"http://127.0.0.1:{local_port()}"


def origin(config):
    address, number = config["public_ip"], config["external_port"]
    return f"{address}:{number}" if number != 443 else address


def status():
    try:
        address = local_address()
    except (OSError, ValueError):
        address = "http://127.0.0.1:8765"
    try:
        state = json.loads(STATUS.read_text())
        return {**state, "local_url": address}
    except (OSError, ValueError):
        return {"mode": "LOCAL", "local_url": address, "proxy": "off",
                "external_verified": False}


def atomic_write(path, data, mode=0o600):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            os.fchmod(output.fileno(), mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def publish_status(state):
    # The application service can read this file through the group-owned config directory.
    with tempfile.NamedTemporaryFile(dir=CONFIG, delete=False) as output:
        temporary = Path(output.name)
        try:
            os.fchown(output.fileno(), 0, CONFIG.stat().st_gid)
            os.fchmod(output.fileno(), 0o640)
            output.write(json.dumps(state).encode())
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, STATUS)
        finally:
            temporary.unlink(missing_ok=True)


def run(args, message, *, timeout=15):
    try:
        return subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        raise RemoteError(message) from None


def systemctl(action, unit, *options):
    run(["/usr/bin/systemctl", action, *options, unit], f"systemd could not {action} {unit}. See remote logs.")


def service_running(unit):
    return subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", unit], check=False).returncode == 0


def bound_address(address):
    interfaces = json.loads(run(["ip", "-j", "-4", "address", "show"], "Cannot read local IPv4 interfaces."))
    for interface in interfaces:
        for info in interface.get("addr_info", []):
            if info.get("local") == address:
                return interface["ifname"], bool(info.get("dynamic") or info.get("valid_life_time") != 4294967295)
    raise RemoteError("Choose a LAN IPv4 currently assigned to this machine.")


def check_port(address, number):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((address, number))
    except OSError:
        raise RemoteError(f"TCP port {number} cannot be bound on {address}. Choose a free port.") from None


def credential_info(chain, key, address, ca):
    certificates = x509.load_pem_x509_certificates(chain.read_bytes())
    leaf = certificates[0]
    try:
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        raise RemoteError("Certificate requires an IPv4 Subject Alternative Name.") from None
    if ipaddress.IPv4Address(address) not in names.get_values_for_type(x509.IPAddress):
        raise RemoteError("Certificate IP SAN does not match the public IPv4 address.")
    now = datetime.now(timezone.utc)
    if not leaf.not_valid_before_utc <= now < leaf.not_valid_after_utc:
        raise RemoteError("Certificate is expired or not yet valid.")
    private_key = serialization.load_pem_private_key(key.read_bytes(), password=None)
    if leaf.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != (
        private_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    ):
        raise RemoteError("Certificate and private key do not match.")
    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / "leaf.pem"
        target.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        run(["openssl", "verify", "-purpose", "sslserver", "-verify_ip", address, "-CAfile", str(ca),
             "-no-CApath", "-no-CAstore", "-untrusted", str(chain), str(target)],
            "Certificate chain could not be verified against the selected trust store.")
    return leaf.not_valid_after_utc.isoformat()


def nginx_config(config, release):
    host = origin(config)
    return f"""user nobody nogroup;
worker_processes 1;
pid {RUNTIME}/nginx.pid;
error_log stderr warn;
events {{ worker_connections 128; }}
http {{
    server_tokens off;
    access_log off;
    client_max_body_size 128k;
    client_body_timeout 15s;
    client_header_timeout 10s;
    send_timeout 90s;
    client_body_temp_path {RUNTIME}/client;
    proxy_temp_path {RUNTIME}/proxy;
    fastcgi_temp_path {RUNTIME}/fastcgi;
    uwsgi_temp_path {RUNTIME}/uwsgi;
    scgi_temp_path {RUNTIME}/scgi;
    map $http_upgrade $connection_upgrade {{
        default upgrade;
        '' close;
    }}
    server {{
        listen {config['lan_ip']}:{config['listen_port']} ssl;
        server_name {config['public_ip']};
        ssl_certificate {release}/fullchain.pem;
        ssl_certificate_key {release}/privkey.pem;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_session_tickets off;
        if ($http_host != "{host}") {{ return 421; }}
        add_header X-Frame-Options DENY always;
        add_header X-Content-Type-Options nosniff always;
        add_header Referrer-Policy no-referrer always;
        location = /health {{ return 404; }}
        location = /api/v1/openapi.json {{ return 404; }}
        location ^~ /api/v1/agent-batches {{ return 404; }}
        location / {{
            proxy_pass http://127.0.0.1:{config['backend_port']};
            proxy_redirect http://{host}/ https://{host}/;
            proxy_http_version 1.1;
            proxy_set_header Host {host};
            proxy_set_header X-Mobocloud-Remote 1;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header Forwarded "";
            proxy_set_header X-Forwarded-For "";
            proxy_set_header X-Forwarded-Proto "";
            proxy_set_header X-Forwarded-Host "";
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_connect_timeout 3s;
            proxy_read_timeout 120s;
            proxy_send_timeout 90s;
            proxy_buffering off;
        }}
    }}
}}
"""


def local_health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(local_address() + "/health", timeout=3) as response:
            if json.load(response)["worker_alive"]:
                return
    except (OSError, ValueError, KeyError):
        pass
    raise RemoteError("The loopback dashboard is unhealthy. Run sudo mobocloud health and check its logs.")


def proxy_health(config, ca):
    context = ssl.create_default_context(cafile=str(ca))
    with socket.create_connection((config["lan_ip"], config["listen_port"]), timeout=4) as sock:
        with context.wrap_socket(sock, server_hostname=config["public_ip"]) as tls:
            tls.sendall(f"GET / HTTP/1.1\r\nHost: {origin(config)}\r\nConnection: close\r\n\r\n".encode())
            with tls.makefile("rb") as response:
                if not re.match(rb"HTTP/1\.[01] 200 ", response.readline(512)):
                    raise RemoteError("The listener did not serve the dashboard over HTTPS.")


def read_release(link):
    try:
        release = (ROOT / link).resolve(strict=True)
        if release.parent != ROOT or not release.name.startswith("release-"):
            raise RemoteError("Invalid remote-access release.")
        return release, json.loads((release / "config.json").read_text())
    except FileNotFoundError:
        return None, None


def set_link(link, target):
    path = ROOT / link
    if target is None:
        path.unlink(missing_ok=True)
        return
    temporary = ROOT / (link + ".next")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target.name)
    os.replace(temporary, path)


@contextmanager
def locked():
    ROOT.mkdir(mode=0o700, exist_ok=True)
    with (ROOT / "manager.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def require_nginx():
    if not os.access(NGINX, os.X_OK):
        raise RemoteError("Install the Debian Nginx binary (/usr/sbin/nginx) before enabling remote access.")
    RUNTIME.mkdir(mode=0o755, exist_ok=True)


def proxy_test(release):
    require_nginx()
    run([NGINX, "-t", "-q", "-p", f"{RUNTIME}/", "-c", str(release / "nginx.conf")],
        "The dedicated Nginx configuration failed validation. Review the port and certificate.")


def prepare(config, chain, key, ca):
    release = Path(tempfile.mkdtemp(prefix="release-", dir=ROOT))
    try:
        for source, name in ((chain, "fullchain.pem"), (key, "privkey.pem"), (ca, "trust.pem")):
            atomic_write(release / name, source.read_bytes())
        config = dict(config, expiry=credential_info(release / "fullchain.pem", release / "privkey.pem",
                                                      config["public_ip"], release / "trust.pem"))
        atomic_write(release / "config.json", json.dumps(config).encode())
        atomic_write(release / "nginx.conf", nginx_config(config, release).encode())
        proxy_test(release)
        return release, config
    except BaseException:
        shutil.rmtree(release)
        raise


def active_status(config, *, diagnostic=None, running=True):
    return dict(mode="SECURE_REMOTE" if running else "LOCAL", proxy="local_https_verified" if running else "off",
                public_ip=config["public_ip"], remote_url=f"https://{origin(config)}",
                certificate_mode=config["certificate_mode"], certificate_expires=config["expiry"],
                external_verified=False, diagnostic=diagnostic)


def activate(release, config):
    old, _ = read_release("current")
    was_running = service_running(PROXY)
    old_status = status()
    if local_port() != config["backend_port"]:
        raise RemoteError("The backend port has changed. Reconfigure remote access for the active port.")
    local_health()
    proxy_test(release)
    set_link("current", release)
    try:
        systemctl("restart", PROXY)
        for attempt in range(10):
            try:
                proxy_health(config, release / "trust.pem")
                break
            except OSError:
                if attempt == 9:
                    raise RemoteError("Local HTTPS verification failed. The previous configuration was restored.") from None
                time.sleep(.3)
        systemctl("enable", PROXY)
        publish_status(active_status(config))
    except (RemoteError, OSError, KeyboardInterrupt):
        set_link("current", old)
        if was_running and old is not None:
            systemctl("restart", PROXY)
        else:
            systemctl("disable", PROXY, "--now")
        publish_status(old_status)
        raise
    if old != release:
        set_link("previous", old)


def certbot_paths(config):
    folder = ROOT / f"acme-{config['public_ip']}"
    return folder, folder / "config" / "live" / f"mobocloud-{config['public_ip']}"


def acme(config, *, renew=False):
    # Use a dedicated Certbot instance so other certificates and hooks are untouched.
    executable = shutil.which("certbot")
    if not executable:
        raise RemoteError("Install Certbot 5.3 or later with IP certificate support.")
    version = run([executable, "--version"], "Cannot check the Certbot version.")
    match = re.search(r"(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (5, 3):
        raise RemoteError("Certbot 5.3 or later is required for IP certificates.")
    folder, live = certbot_paths(config)
    folder.mkdir(mode=0o700, exist_ok=True)
    args = [executable, "--config-dir", str(folder / "config"), "--work-dir", str(folder / "work"),
            "--logs-dir", str(folder / "logs"), "--non-interactive", "--cert-name", live.name,
            "--server", "https://acme-v02.api.letsencrypt.org/directory"]
    if renew:
        args += ["renew"]
    else:
        args += ["certonly", "--standalone", "--preferred-profile", "shortlived", "--agree-tos",
                 "--email", config["email"], "--ip-address", config["public_ip"],
                 "--http-01-address", config["lan_ip"], "--http-01-port", str(config["challenge_port"]),
                 "--keep-until-expiring"]
    run(args, "ACME failed. Check inbound TCP 80 forwarding, Certbot logs, and outbound HTTPS.", timeout=180)
    return live / "fullchain.pem", live / "privkey.pem", SYSTEM_CA


def instructions(config):
    iface, dynamic = bound_address(config["lan_ip"])
    print(f"Forward public TCP {config['external_port']} to {config['lan_ip']} TCP {config['listen_port']} ({iface}).")
    print(f"If UFW blocks the listener, review: sudo ufw allow in on {shlex.quote(iface)} to {config['lan_ip']} port {config['listen_port']} proto tcp")
    if config["certificate_mode"] == "acme":
        print(f"For ACME, forward public TCP 80 to {config['lan_ip']} TCP {config['challenge_port']}.")
        print(f"If UFW blocks HTTP-01, review: sudo ufw allow in on {shlex.quote(iface)} to {config['lan_ip']} port {config['challenge_port']} proto tcp")
    if dynamic:
        print("The LAN address may change. Set a DHCP reservation on the router before relying on this forwarding.")
    print(f"Test https://{origin(config)} from Safari on cellular data.")


def config_from_args(args):
    config = dict(public_ip=ipv4(args.public_ip, public=True), lan_ip=ipv4(args.lan_ip),
                  listen_port=args.listen_port, external_port=args.external_port, backend_port=local_port(),
                  certificate_mode=args.certificate, challenge_port=args.challenge_port, email=getattr(args, "email", None))
    if config["listen_port"] in {config["backend_port"], 22} or config["external_port"] == 22:
        raise RemoteError("Choose an HTTPS port distinct from the dashboard backend and SSH.")
    if config["certificate_mode"] == "acme" and config["challenge_port"] in {22, config["listen_port"], config["backend_port"]}:
        raise RemoteError("Choose a separate HTTP-01 challenge port.")
    if config["certificate_mode"] == "acme" and config["external_port"] == 80:
        raise RemoteError("External TCP 80 is reserved for the ACME challenge in this mode.")
    bound_address(config["lan_ip"])
    return config


def verify_administrator():
    import getpass
    import hmac
    from .main import digest, strong_password
    identity = json.loads((CONFIG / "keys" / "identity.json").read_text())
    password = getpass.getpass("Current administrator password: ")
    if not strong_password(password):
        raise RemoteError("Remote access requires a strong administrator password of at least 12 characters.")
    if not hmac.compare_digest(digest(password, bytes.fromhex(identity["salt"])).hex(), identity["password"]):
        raise RemoteError("The administrator password does not match.")


def enable(args, config):
    require_nginx()
    local_health()
    old, _ = read_release("current")
    running = service_running(PROXY)
    old_config = json.loads((old / "config.json").read_text()) if old else {}
    binding = config["lan_ip"], config["listen_port"]
    if not running or (old_config.get("lan_ip"), old_config.get("listen_port")) != binding:
        check_port(*binding)
    verify_administrator()
    if config["certificate_mode"] == "acme":
        if not args.email or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", args.email) or args.email.startswith("-"):
            raise RemoteError("Provide an ACME account email with --email.")
        check_port(config["lan_ip"], config["challenge_port"])
        paths = acme(config)
    else:
        if args.chain is None or args.key is None:
            raise RemoteError("Provide a certificate chain and private key with --chain and --key.")
        paths = args.chain, args.key, args.ca or SYSTEM_CA
        config["certificate_mode"] = "private_ca" if args.ca else "imported_public"
    release, config = prepare(config, *paths)
    activate(release, config)
    try:
        systemctl("enable", TIMER, "--now")
    except RemoteError:
        if old is None or not running:
            disable("LOCAL")
        else:
            activate(old, old_config)
        raise
    instructions(config)


def disable(mode):
    systemctl("disable", PROXY, "--now")
    publish_status(dict(mode=mode, proxy="off", external_verified=False))
    systemctl("disable", TIMER, "--now")


def renew():
    release, config = read_release("current")
    current = status()
    if release is None or not current.get("remote_url"):
        return
    if config["certificate_mode"] != "acme":
        if datetime.fromisoformat(config["expiry"]) <= datetime.now(timezone.utc):
            systemctl("stop", PROXY)
            publish_status(active_status(config, diagnostic="Certificate expired. Import a renewed certificate.", running=False))
            raise RemoteError("The imported certificate expired. The HTTPS listener is stopped.")
        return
    try:
        chain, key, ca = acme(config, renew=True)
        # A failed, previously issued renewal can be retried from the dedicated lineage.
        if chain.read_bytes() != (release / "fullchain.pem").read_bytes() or not service_running(PROXY):
            candidate, config = prepare(config, chain, key, ca)
            activate(candidate, config)
        else:
            publish_status(active_status(config))
    except (RemoteError, OSError, ValueError):
        expired = datetime.fromisoformat(config["expiry"]) <= datetime.now(timezone.utc)
        if expired:
            systemctl("stop", PROXY)
        publish_status(active_status(config, diagnostic="ACME renewal failed. Review the remote renewal logs.",
                                     running=not expired and service_running(PROXY)))
        raise RemoteError("ACME renewal failed. Review the remote renewal logs and TCP 80 forwarding.") from None


def menu():
    while True:
        print("\nRemote access\n1) Status\n2) Enable or reconfigure HTTPS\n3) Disable HTTPS\n"
              "4) Switch to SSH-only\n5) Loopback health\n6) Test Nginx configuration\n"
              "7) Certificate health\n8) Router instructions\n9) Logs\n10) Restore previous HTTPS configuration\n0) Back")
        choice = input("Select: ").strip()
        actions = {"1":["status"], "3":["disable"], "4":["disable", "--ssh-only"], "5":["health"],
                   "6":["test-proxy"], "7":["certificate"], "8":["instructions"], "9":["logs"]}
        try:
            if choice == "0":
                return
            if choice in actions:
                cli(actions[choice])
            elif choice == "10":
                if input("Restore and enable the previous HTTPS listener? [y/N] ").strip().lower() == "y":
                    cli(["rollback", "--approve-listener"])
            elif choice == "2":
                public = input("Public IPv4: ").strip()
                lan = input("Current LAN IPv4: ").strip()
                external = input("Router external HTTPS port [443]: ").strip() or "443"
                listen = input("Local HTTPS listening port [8443]: ").strip() or "8443"
                certificate = input("Certificate mode: acme or imported [acme]: ").strip() or "acme"
                args = ["--public-ip", public, "--lan-ip", lan, "--external-port", external,
                        "--listen-port", listen, "--certificate", certificate]
                if certificate == "acme":
                    args += ["--challenge-port", input("Local HTTP-01 port [8080]: ").strip() or "8080"]
                    cli(["plan", *args])
                    args += ["--email", input("ACME account email: ").strip()]
                elif certificate == "imported":
                    args += ["--chain", input("PEM certificate chain path: ").strip(),
                             "--key", input("PEM private key path: ").strip()]
                    ca = input("Private CA certificate path (empty for public trust): ").strip()
                    if ca:
                        args += ["--ca", ca]
                if input("Enable the dedicated network listener after validation? [y/N] ").strip().lower() == "y":
                    cli(["enable", "--approve-listener", *args])
        except RemoteError as exc:
            print(exc)
        except SystemExit:
            print("Review the values and try again.")


def cli(argv=None):
    parser = argparse.ArgumentParser(description="Manage Mobocloud remote HTTPS access")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("menu", "status", "health", "test-proxy", "certificate", "instructions", "logs", "renew", "cert-guard"):
        commands.add_parser(action)
    for action in ("plan", "enable"):
        command = commands.add_parser(action)
        command.add_argument("--public-ip", required=True)
        command.add_argument("--lan-ip", required=True)
        command.add_argument("--listen-port", type=port, default=8443)
        command.add_argument("--external-port", type=port, default=443)
        command.add_argument("--challenge-port", type=port, default=8080)
        command.add_argument("--certificate", choices=["acme", "imported"], default="acme")
        if action == "enable":
            command.add_argument("--approve-listener", action="store_true", required=True)
            command.add_argument("--email")
            command.add_argument("--chain", type=Path)
            command.add_argument("--key", type=Path)
            command.add_argument("--ca", type=Path)
    command = commands.add_parser("disable")
    command.add_argument("--ssh-only", action="store_true")
    commands.add_parser("rollback").add_argument("--approve-listener", action="store_true", required=True)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise RemoteError("Run sudo mobocloud remote.")
    if args.action == "menu":
        menu()
        return
    if args.action == "plan":
        instructions(config_from_args(args))
        return
    if args.action == "status":
        state = status()
        if state.get("remote_url"):
            state["proxy_running"] = service_running(PROXY)
        print(json.dumps(state, indent=2))
        return
    if args.action == "health":
        local_health()
        print("Loopback dashboard is healthy.")
        return
    if args.action == "cert-guard":
        # Called by systemd while the manager may hold the update lock.
        release, config = read_release("current")
        if release is None:
            raise RemoteError("The HTTPS listener has no saved certificate configuration.")
        credential_info(release / "fullchain.pem", release / "privkey.pem",
                        config["public_ip"], release / "trust.pem")
        return
    if args.action == "logs":
        subprocess.run(["journalctl", "--no-pager", "-n", "80", "-u", PROXY, "-u", "mobocloud-remote-renew.service"],
                       check=True)
        return
    with locked():
        if args.action == "enable":
            enable(args, config_from_args(args))
        elif args.action == "disable":
            disable("SSH_ONLY" if args.ssh_only else "LOCAL")
        elif args.action == "renew":
            renew()
        else:
            release, config = read_release("previous" if args.action == "rollback" else "current")
            if release is None:
                raise RemoteError("There is no saved configuration for this command.")
            if args.action == "instructions":
                instructions(config)
            elif args.action == "test-proxy":
                proxy_test(release)
                print("Nginx configuration is valid.")
            else:
                expiry = credential_info(release / "fullchain.pem", release / "privkey.pem",
                                         config["public_ip"], release / "trust.pem")
                if args.action == "certificate":
                    print(f"{config['certificate_mode']} certificate valid for {config['public_ip']} until {expiry}.")
                else:
                    verify_administrator()
                    activate(release, config)
                    systemctl("enable", TIMER, "--now")


if __name__ == "__main__":
    try:
        cli()
    except (RemoteError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, RemoteError) else "Remote access failed. Check paths, settings, and service logs."
        raise SystemExit(message) from None
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("Cancelled.") from None
