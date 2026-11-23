import argparse
import asyncio
import ipaddress
from pathlib import Path
import ssl
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import httpx

from lab import main
from lab import remote_access as remote


def sample_config(**changes):
    return dict(dict(public_ip="192.0.2.10", lan_ip="192.168.20.22", external_port=9443,
                     listen_port=8443, challenge_port=8080, backend_port=8765,
                     certificate_mode="private_ca", expiry="2080-01-01T00:00:00+00:00"), **changes)


class RemoteAccessTests(unittest.TestCase):
    def test_default_has_no_public_url(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(remote, "CONFIG", Path(folder)), \
                patch.object(remote, "STATUS", Path(folder) / "missing"):
            self.assertEqual(remote.status(), {"mode": "LOCAL", "local_url": "http://127.0.0.1:8765",
                                              "proxy": "off", "external_verified": False})

    def test_address_port_validation_and_conflict(self):
        for value in ("127.0.0.1", "10.1.2.3", "100.64.1.1", "224.1.2.3", "::1"):
            with self.subTest(address=value), self.assertRaises(ValueError):
                remote.ipv4(value, public=True)
        for value in ("0", "99999", "443;"):
            with self.subTest(port=value), self.assertRaises(ValueError):
                remote.port(value)
        args = argparse.Namespace(public_ip=str(ipaddress.IPv4Address("8.8.8.8")), lan_ip="192.168.20.22",
                                  external_port=443, listen_port=8765, certificate="acme", challenge_port=8080)
        with patch.object(remote, "local_port", return_value=8765), self.assertRaisesRegex(remote.RemoteError, "distinct"):
            remote.config_from_args(args)
        with patch.object(remote.socket, "socket") as socket:
            socket.return_value.__enter__.return_value.bind.side_effect = OSError
            with self.assertRaisesRegex(remote.RemoteError, "cannot be bound"):
                remote.check_port("127.0.0.1", 8443)
        with patch.object(remote.os, "access", return_value=False):
            with self.assertRaisesRegex(remote.RemoteError, "Nginx"):
                remote.require_nginx()
        with patch.object(remote.shutil, "which", return_value=None):
            with self.assertRaisesRegex(remote.RemoteError, "Certbot"):
                remote.acme(sample_config())

    def test_proxy_only_listens_on_lan_https_and_replaces_client_headers(self):
        config = remote.nginx_config(sample_config(), Path("/managed/release-123"))
        self.assertIn("listen 192.168.20.22:8443 ssl;", config)
        self.assertIn('if ($http_host != "192.0.2.10:9443")', config)
        self.assertIn("proxy_pass http://127.0.0.1:8765;", config)
        self.assertIn('proxy_set_header X-Forwarded-For "";', config)
        self.assertIn("proxy_set_header X-Real-IP $remote_addr;", config)
        self.assertIn("location ^~ /api/v1/agent-batches", config)
        self.assertNotIn("listen 0.0.0.0", config)

    def test_certificate_requires_matching_ip_and_trusted_chain(self):
        with tempfile.TemporaryDirectory() as folder:
            ca, key, cert, csr = (Path(folder) / name for name in ("ca.pem", "key.pem", "leaf.pem", "request.pem"))
            ca_key = Path(folder) / "ca.key"
            extension = Path(folder) / "ext.cnf"
            extension.write_text("subjectAltName=IP:192.0.2.10\nextendedKeyUsage=serverAuth\n")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3",
                            "-subj", "/CN=Test CA", "-out", ca, "-keyout", ca_key], capture_output=True, check=True)
            subprocess.run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=Fixture",
                            "-out", csr, "-keyout", key], capture_output=True, check=True)
            subprocess.run(["openssl", "x509", "-req", "-days", "2", "-in", csr, "-CA", ca, "-CAkey",
                            ca_key, "-extfile", extension, "-out", cert], capture_output=True, check=True)
            self.assertTrue(remote.credential_info(cert, key, "192.0.2.10", ca))
            with self.assertRaisesRegex(remote.RemoteError, "IP SAN"):
                remote.credential_info(cert, key, "192.0.2.11", ca)
            with self.assertRaisesRegex(remote.RemoteError, "do not match"):
                remote.credential_info(cert, ca_key, "192.0.2.10", ca)
            with self.assertRaisesRegex(remote.RemoteError, "chain"):
                remote.credential_info(cert, key, "192.0.2.10", remote.SYSTEM_CA)

    def test_failed_activation_restores_previous_release(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(remote, "ROOT", Path(folder)), \
                patch.object(remote, "local_port", return_value=8765), patch.object(remote, "local_health"), \
                patch.object(remote, "proxy_test"), patch.object(remote, "service_running", return_value=True), \
                patch.object(remote, "systemctl") as manager, patch.object(remote, "publish_status") as save:
            old, candidate = (Path(folder) / name for name in ("release-old", "release-candidate"))
            old.mkdir()
            (old / "config.json").write_text("{}")
            candidate.mkdir()
            remote.set_link("current", old)
            with patch.object(remote, "proxy_health", side_effect=ssl.SSLCertVerificationError), \
                    patch.object(remote.time, "sleep"), self.assertRaisesRegex(remote.RemoteError, "restored"):
                remote.activate(candidate, sample_config())
            self.assertEqual(remote.read_release("current")[0], old)
            self.assertEqual(manager.call_count, 2)
            self.assertEqual(save.call_count, 1)
            with patch.object(remote, "proxy_health"):
                remote.activate(candidate, sample_config())
            self.assertEqual((Path(folder) / "previous").resolve(), old)

    def test_renewal_keeps_a_valid_listener_and_stops_it_after_expiry(self):
        expired = sample_config(certificate_mode="acme", expiry="2000-01-01T00:00:00+00:00")
        valid = sample_config(certificate_mode="acme")
        with patch.object(remote, "read_release") as saved, \
                patch.object(remote, "status", return_value={"remote_url":"https://192.0.2.10"}), \
                patch.object(remote, "acme", side_effect=remote.RemoteError("challenge failed")), \
                patch.object(remote, "service_running", return_value=True), \
                patch.object(remote, "systemctl") as manager, patch.object(remote, "publish_status") as publish:
            saved.return_value = Path("/managed/release"), valid
            with self.assertRaisesRegex(remote.RemoteError, "ACME renewal failed"):
                remote.renew()
            manager.assert_not_called()
            self.assertEqual(publish.call_args.args[0]["mode"], "SECURE_REMOTE")
            saved.return_value = Path("/managed/release"), expired
            with self.assertRaisesRegex(remote.RemoteError, "ACME renewal failed"):
                remote.renew()
            manager.assert_called_once_with("stop", remote.PROXY)
            self.assertEqual(publish.call_args.args[0]["proxy"], "off")


class RemoteAuthTests(unittest.TestCase):
    def test_tls_sessions_csrf_authorization_and_limiter(self):
        async def run_inline(callback, *args, **kwargs):
            return callback(*args)

        with tempfile.TemporaryDirectory() as folder, patch.object(main, "DATA", Path(folder)), \
                patch.object(main, "KEY", Path(folder) / "keys"), patch("getpass.getpass", return_value="Fixture-Phrase!29"), \
                patch("anyio.to_thread.run_sync", side_effect=run_inline):
            main.initialize()
            app = main.create()
            asyncio.run(self.exercise(app))

    async def exercise(self, app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 9000))
        markers = {"X-Lab-Request": "1", "X-Mobocloud-Remote": "1", "X-Real-IP": "192.0.2.20"}
        async with httpx.AsyncClient(transport=transport, base_url="https://192.0.2.10", headers=markers) as browser:
            self.assertEqual((await browser.get("/api/v1/remote-access")).status_code, 401)
            self.assertEqual((await browser.get("/health")).status_code, 404)
            secret = {"password": "Fixture-Phrase!29"}
            endpoint = "/api/v1/login"
            self.assertEqual((await browser.post(endpoint, json=secret,
                                                  headers={"Origin": "https://example.org"})).status_code, 403)
            for _ in range(8):
                self.assertEqual((await browser.post(endpoint, json={"password": "incorrect"})).status_code, 401)
            self.assertEqual((await browser.post(endpoint, json=secret)).status_code, 429)
            login = await browser.post(endpoint, json=secret, headers={"X-Real-IP": "192.0.2.21",
                                                                      "Origin": "https://192.0.2.10"})
            self.assertEqual(login.status_code, 200)
            for flag in ("Secure", "HttpOnly", "SameSite=strict"):
                self.assertIn(flag, login.headers["set-cookie"])
            self.assertEqual((await browser.get("/api/v1/remote-access")).status_code, 200)
            self.assertEqual((await browser.get("/api/v1/remote-access", headers={"X-Mobocloud-Remote":"0"})).status_code, 401)
            self.assertEqual((await browser.post("/api/v1/logout", json={})).status_code, 200)
            self.assertEqual((await browser.get("/api/v1/remote-access")).status_code, 401)


class InstallerTests(unittest.TestCase):
    def test_upgrade_health_failure_restores_release_and_keeps_user_files(self):
        import os
        import shutil

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, commands, system, config, data, base, bin_dir = (
                root / name for name in ("source", "commands", "system", "config", "data", "releases", "localbin"))
            for path in (source, commands, system, config / "keys", data, base, bin_dir):
                path.mkdir(parents=True, exist_ok=True)
            project = Path(__file__).resolve().parents[1]
            for path in ("frontend", "lab", "deployment"):
                shutil.copytree(project / path, source / path, ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copyfile(project / "requirements.txt", source / "requirements.txt")
            script = source / "deployment" / "install"
            fixture = script.read_text()
            replacements = {
                "/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin": f"{commands}:/usr/bin:/bin",
                "/etc/os-release": str(root / "os-release"),
                "/opt/mobohub-lab": str(base),
                "/etc/mobohub-network-lab": str(config),
                "/var/lib/mobohub-network-lab": str(data),
                "/etc/systemd/system": str(system),
                "/usr/local/bin": str(bin_dir),
                " -g mobohub-lab": "",
            }
            for pattern, replacement in replacements.items():
                fixture = fixture.replace(pattern, replacement)
            script.write_text(fixture)
            (root / "os-release").write_text("ID=debian\nVERSION_ID=13\n")
            owned_files = {config / "settings": 'LAB_XRAY="/existing/fixture"\n',
                           config / "keys" / "identity.json": "fixture identity",
                           data / "lab.sqlite": "fixture database"}
            for path, body in owned_files.items():
                path.write_text(body)
            shims = {
                "apt-get": "exit 0",
                "id": "if [ \"${1-}\" = -u ]; then echo 0; fi",
                "runuser": "exit 0",
                "useradd": "exit 0",
                "sleep": "exit 0",
                "systemctl": 'printf "%s\\n" "$*" >> "$INSTALLER_SYSTEM_LOG"',
                "python3": 'mkdir -p "$3/bin" && cp "$INSTALLER_PYTHON_STUB" "$3/bin/python" && cp "$INSTALLER_PYTHON_STUB" "$3/bin/pip"',
            }
            stub = root / "python-stub"
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
            for name, code in shims.items():
                path = commands / name
                path.write_text(f"#!/bin/sh\n{code}\n")
                path.chmod(0o755)
            cli = source / "deployment" / "mobohub"
            cli.write_text('#!/bin/sh\n[ "${INSTALLER_FAIL_HEALTH:-0}" = 0 ]\n')
            log = root / "system.log"
            env = dict(os.environ, INSTALLER_SYSTEM_LOG=str(log), INSTALLER_PYTHON_STUB=str(stub))

            def install(**overrides):
                return subprocess.run(["sh", str(script), "--start"], env=dict(env, **overrides),
                                      capture_output=True, text=True, timeout=15)

            result = install()
            self.assertEqual(result.returncode, 0, result.stderr)
            previous = (base / "current").resolve()
            result = install(INSTALLER_FAIL_HEALTH="1")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((base / "current").resolve(), previous)
            result = install()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((base / "current" / ".venv").resolve(), previous / ".venv")
            for path, body in owned_files.items():
                self.assertEqual(path.read_text(), body)
            self.assertNotIn("enable mobocloud-remote", log.read_text())
