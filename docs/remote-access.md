# Domainless remote access on Debian 13

The default mode is **LOCAL**. Uvicorn listens on IPv4 loopback using `LAB_PORT`, normally 8765. **SECURE_REMOTE** uses a dedicated Nginx service listening on the selected LAN IPv4 and HTTPS port. **SSH_ONLY** stops the HTTPS service and leaves the loopback dashboard available through your existing SSH account.

## Install or upgrade

For a fresh installation, use the Debian 13 installer from [README](../README.md). To upgrade from a reviewed checkout of this repository, run:

```sh
sudo ./deployment/install --start
```

The installer keeps the administrator identity, settings, database and remote-access settings. It reuses the virtual environment when dependencies match. It backs up the service definitions and CLI, starts the selected release, and restores the previous release if the health check fails. Back up the database and its administrator identity separately before upgrading. The HTTPS listener starts only after an administrator enables it.

Install a Debian-supported `/usr/sbin/nginx` before opting in. If the host already has Nginx, Mobocloud uses its binary with a dedicated config and service. On a host without an Nginx installation, you can suppress the package's stock service before installing the binary:

```sh
if ! command -v nginx >/dev/null && ! systemctl cat nginx.service >/dev/null 2>&1; then
  sudo systemctl mask nginx.service
  sudo apt-get install nginx
fi
```

The Mobocloud service runs independently of that stock unit. Use an existing package-management policy on machines that already manage Nginx.

## Plan the forwarding

Use the actual public IPv4 from your ISP and the currently assigned LAN IPv4. In this example, replace `PUBLIC_IPV4` and `LAN_IPV4` with those values.

```sh
sudo mobocloud remote plan --public-ip PUBLIC_IPV4 --lan-ip LAN_IPV4
```

The default mapping is:

| Purpose | WAN TCP port | Mac mini LAN TCP port |
|---|---:|---:|
| Safari HTTPS | 443 | 8443 |
| Let’s Encrypt HTTP-01 only | 80 | 8080 |
| Existing SSH | Keep its existing rule | Keep its existing rule |

The web backend stays on loopback. Check the WAN IPv4 in the router and compare it with the public IPv4 from your ISP. If WAN shows a private address, an address in 100.64.0.0/10, or another IPv4, confirm upstream NAT and inbound routing with your ISP before relying on these rules. The planner checks the LAN address and warns when it might change. Reserve that address for the Mac mini's MAC address through the router's DHCP settings if available, and verify the original SSH session is still reachable after any router change. Use external port 9443 if 443 is already mapped or the ISP only permits another port. Keep the selected external port distinct from the existing SSH forwarding.

### Huawei HG8245Q2

1. Sign in to the router using its LAN administration address and inspect the WAN status. Select the INTERNET WAN connection used by the existing SSH mapping.
2. Open **Forward Rules → Port Mapping Configuration**. Some provider firmware labels it **Advanced → Forward Rules → IPv4 Port Mapping**.
3. Create an enabled **Custom** mapping named `Mobocloud HTTPS`. Set protocol to TCP, internal host to the Mac mini's current LAN IPv4, external start and end to `443`, and internal start and end to `8443`. For cellular access from changing addresses, leave the external source filter blank if the firmware permits it. Save the rule after the local HTTPS check succeeds.
4. When using automatic issuance, create `Mobocloud ACME` before the first issuance. Use the same WAN and internal host, TCP external start and end `80`, internal start and end `8080`. Keep this rule for automated renewals. The temporary HTTP service answers certificate challenges while Certbot is active.
5. Keep the original SSH entry as it is. If the firmware hides the forwarding menu or uses WAN port 80 for router management, ask the ISP about inbound port 80 or use an imported certificate. [Huawei port mapping guide](https://consumer.huawei.com/en/support/content/en-us15806329/)

For UFW, review `sudo ufw status numbered` over the existing SSH session. The planner prints interface-, address-, and port-specific allow commands. Apply only the relevant rules you approve. For ACME, TCP 8080 needs to be reachable through WAN port 80 before issuance. Add the TCP 8443 rule for Safari after local verification. Keep SSH and Fail2ban rules in place.

## Enable Let’s Encrypt for a public IPv4

Certbot 5.3 or later is required for the standalone IP challenge. Debian 13's stable Certbot 4 package does not support `--ip-address`. Install a compatible Certbot distribution available to root and systemd. A dedicated Python virtual environment under `/opt` is one option. [Let’s Encrypt IP certificate requirements](https://letsencrypt.org/2026/03/11/shorter-certs-certbot/) · [Debian Certbot package](https://packages.debian.org/trixie/certbot)

```sh
sudo python3 -m venv /opt/mobocloud-certbot
sudo /opt/mobocloud-certbot/bin/pip install 'certbot>=5.3,<6'
sudo ln -s /opt/mobocloud-certbot/bin/certbot /usr/local/bin/certbot
```

After the ACME mapping is ready, run:

```sh
sudo mobocloud remote enable --approve-listener --public-ip PUBLIC_IPV4 --lan-ip LAN_IPV4 --email admin@example.org
```

Enter the existing administrator password at the prompt. The account must use a strong password with at least 12 characters for remote access. If needed, set a new password with `sudo mobocloud reset-password` and repeat enable.

The manager verifies the IP SAN, trust chain, expiry and private-key match, then checks the loopback dashboard and performs a verified HTTPS request against the LAN listener using the public IP as the TLS identity. On failure it restores any previous working configuration. Let’s Encrypt IP certificates use a short validity period of about six days. The dedicated systemd timer checks renewal every eight hours, persists across reboots and reloads the certificate by activating a validated configuration. The dashboard displays the last local verification and expiry. [Let’s Encrypt certificate profile](https://letsencrypt.org/docs/profiles/#shortlived)

## Import a public or private certificate

Use a PEM leaf and intermediate chain with an exact public IPv4 Subject Alternative Name, an unencrypted PEM private key readable by root, and a certificate valid for TLS server authentication. To validate against Debian public roots, omit `--ca`. To explicitly use a private CA, pass the root certificate as `--ca`.

```sh
sudo mobocloud remote enable --approve-listener --public-ip PUBLIC_IPV4 --lan-ip LAN_IPV4 \
  --certificate imported --chain /root/fullchain.pem --key /root/privkey.pem --ca /root/root-ca.pem
```

Keep the CA signing key offline. Transfer only the public CA certificate to your own iPhone through a trusted channel. Verify its SHA-256 fingerprint against the original using `openssl x509 -in root-ca.pem -noout -fingerprint -sha256`. Install the certificate profile in Settings. Then go to **Settings → General → About → Certificate Trust Settings** and enable full trust for that CA. A manually trusted root extends trust beyond this dashboard, so use a dedicated root you control and remove the profile when finished. Renew imported certificates before expiry and repeat the import command. [Apple certificate trust instructions](https://support.apple.com/en-us/102390)

## Safari verification and troubleshooting

Turn off Wi-Fi on the iPhone, then open `https://PUBLIC_IPV4` in Safari. With a nondefault port, use `https://PUBLIC_IPV4:9443` or your configured port. Authenticate only after Safari validates HTTPS.

- If the connection times out, check the Huawei WAN address, the TCP mapping, the LAN reservation, UFW and ISP inbound-port restrictions. If it works from cellular but fails from the same office Wi-Fi, the router may lack NAT loopback. The local check is separate from internet reachability.
- If issuance fails, confirm inbound WAN TCP 80 reaches the configured challenge port during issuance and that the server can reach Let’s Encrypt over HTTPS. Some Iranian links or provider firmware restrict either direction. Imported certificates and SSH forwarding are available when HTTP-01 cannot complete.
- Use `sudo mobocloud remote status`, `sudo mobocloud remote health`, `sudo mobocloud remote test-proxy`, `sudo mobocloud remote certificate`, and `sudo mobocloud remote logs`. After reboot, verify the proxy and timer status locally as well as from Safari.
- Restore a previous validated HTTPS configuration with `sudo mobocloud remote rollback --approve-listener`. Disable the listener and timer with `sudo mobocloud remote disable`, then remove only the HTTPS and ACME forwarding entries and associated UFW rules you added. The saved HTTPS private configuration lives under `/etc/mobohub-network-lab/remote`. After making a backup if desired, remove that dedicated directory and `/etc/mobohub-network-lab/remote-status.json` to remove all saved HTTPS configuration. Mobocloud data and existing SSH entries remain available.

## SSH fallback

Select `sudo mobocloud remote disable --ssh-only` if HTTPS is unavailable. From a computer with OpenSSH, use the already working SSH port and account:

```sh
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18765:127.0.0.1:8765 -p EXISTING_SSH_PORT sami@PUBLIC_IPV4
```

Open `http://127.0.0.1:18765` in a browser on the same device. Only use this procedure on iPhone if your particular SSH client explicitly supports local forwarding and can keep the tunnel active while Safari is open. Use the configured backend port if it differs from 8765.

## Security review

- The application listens on loopback. Nginx enforces the configured public Host, replaces client IP headers, does not serve the debug schema, health or signed-agent intake publicly, and serves the panel only over TLS. The application's reverse-proxy header trust is limited to the local connection.
- The browser API requires a session for administrative calls. The login limiter keys failures on the client IP and bounds each access mode globally. Mutations check browser origin and require the existing request header. HTTPS cookies carry Secure, HttpOnly and SameSite=Strict. Signing out invalidates the server-side session.
- Certificate files and material used for rollback are root-readable. The dashboard reads a dedicated status file without private keys, account credentials or the ACME email. HTTPS cannot be activated without the explicit admin command. Firewall, router, routes, DNS, SSH and Fail2ban changes are operator-controlled.
- Local testing and automated tests exercise certificate, authentication and rollback paths. Validate the actual router, certificate issuance over the ISP link and iPhone Safari from outside the LAN before relying on the deployment.

## راه‌اندازی سریع فارسی

1. نسخهٔ بررسی‌شده را با `sudo ./deployment/install --start` به‌روز کن. پیش‌نیازهای Nginx و Certbot را طبق همین راهنما آماده کن.
2. در `sudo mobocloud` گزینهٔ **Remote access** را باز کن. آدرس عمومی و IP فعلی شبکهٔ داخلی را وارد کن. برای گواهی خودکار، اول روی هواوی TCP ۸۰ را به TCP ۸۰۸۰ مک‌مینی وصل کن.
3. پس از تأیید HTTPS محلی، روی هواوی TCP ۴۴۳ را به TCP ۸۴۴۳ مک‌مینی وصل کن. قاعدهٔ SSH فعلی را نگه دار. در صورت نیاز، فقط قواعد UFW چاپ‌شده برای پورت‌های مربوط را اعمال کن.
4. با اینترنت موبایل در Safari آدرس HTTPS نشان‌داده‌شده را باز کن. اگر HTTPS عمومی در دسترس نیست و گوشی‌ات از تونل محلی SSH پشتیبانی می‌کند، بخش SSH را دنبال کن.
