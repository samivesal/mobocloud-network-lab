# Operating the station

## Installation and release management

Use a separately approved Linux host. The installer targets Debian 13 with systemd, python3-venv, curl 8.4+, openssl, useradd, runuser and a reachable Python package index. Supply a trusted Xray executable separately for real VPN testing. Verify its provenance and version under the service account. The application requires Linux /proc visibility of its own sockets and children. No route, resolver, modem or firewall configuration is needed.

1. Review deployment/install and deployment/mobohub-lab.service. From the extracted tree, run `sudo ./deployment/install` on the approved machine. It creates a dedicated account, timestamped release and virtualenv. It prompts for an administrator password on first installation, preserves existing settings and keys, and prepares the systemd unit. Service startup creates the WAL database.
2. Inspect /etc/mobohub-network-lab/settings. Start the service with `sudo systemctl start mobohub-lab`. Run `./deployment/validate` and check `journalctl -u mobohub-lab`. Enable boot startup separately with `sudo systemctl enable mobohub-lab`.
3. For remote administration, forward loopback over SSH, for example `ssh -N -L 8765:127.0.0.1:8765 operator@station`. A separately reviewed HTTPS reverse proxy can terminate TLS; set LAB_HTTPS=1 and preserve same-origin requests. Restrict ingress and the administrative path at that proxy.
4. One application process owns the local scheduler lock. Use one Uvicorn worker. Run a separate installation per routed gateway. The default service memory ceiling is 384 MiB; measure actual peaks before adjusting it. Check `systemctl show mobohub-lab -p MemoryCurrent -p TasksCurrent -p CPUUsageNSec`, and inspect elapsed times during one approved lightweight job. Titan hardware performance is unmeasured.

### Backup, upgrade and recovery

From the release root with its virtualenv active, use the Python interpreter and service account that can read the database:

```sh
python -m lab.maintenance backup /private/backup.sqlite --database /var/lib/mobohub-network-lab/lab.sqlite
python -m lab.maintenance restore /private/backup.sqlite --database /private/recovery/lab.sqlite
```

Both commands require a new destination. The backup uses SQLite's online backup API and integrity check. Independently protect a backup of identity.json and settings; the encrypted database alone cannot reconstruct VPN credentials. Treat the identity file as equivalent to access to all stored credentials.

Record the old target of /opt/mobohub-lab/current. Stop the service after backing it up. Run deployment/upgrade from the new release, then start the service and validate it. To roll back, stop the unit, repoint the current symlink to the recorded release and restore the matching database into a new directory. Set the service LAB_DATA override to that directory and restore its service-user ownership and private permissions before restarting. Validate the recovered inventory and reports. deployment/uninstall --remove-unit disables and removes only the service unit. Application data, accounts, configuration and releases are retained.

## Authorizing traffic

Set LAB_LIVE=1, LAB_PEERS to a comma-separated list of approved lowercase peer hostnames or IPs, and LAB_CHECK_URL to a small, fixed HTTPS endpoint returning a 2xx response. The peer hostname is allowlisted for all ports, so use controlled hosts. A target is selected only by server-side configuration. DNS resolution must yield public IPs; the chosen address is pinned in the request. Redirects are never followed. LAB_IP_URL optionally selects a small plain-text exit-IP service. LAB_IP_FALLBACK_URL selects an optional second provider. Traffic to these targets reveals the station or VPN exit address to the chosen provider. Restart after editing the environment.

A manual check generates one direct baseline and one VPN request. Optional egress lookups add one request. A private temporary directory holds the Xray JSON. A random SOCKS password is used per job. The supervisor closes its reserved port just before launch, retries at most three times and confirms the actual child owns the TCP listener before authenticating. The child wrapper registers a Linux parent-death signal. SIGCONT, TERM and any final kill apply to that owned child alone. Interrupted jobs are marked at restart. The deployment unit also cleans its cgroup. Hard power loss or SIGKILL can leave a temporary config on disk; protect /tmp and use PrivateTmp in production. Runtime logs are suppressed to avoid leaking subscription data.

For lightweight schedules, also set LAB_SCHEDULE=1, then set the interval in Configurations. Zero suspends scheduling. Minimum is 60 seconds; 300 seconds is a reasonable initial value only with permission. Each schedule adds jitter. Disable a configuration to suspend future jobs. Cancel queued or running jobs in Test console. Active curl transfers check cancellation approximately every 250 ms. OS DNS resolution can delay acknowledgment.

For an explicitly approved speed measurement, set LAB_SPEED=1 and configure HTTPS download and upload endpoints that you control. The download URL must return exactly LAB_DOWNLOAD_BYTES. The upload server must accept POST binary data and return a final success status. Default download is 5,000,000 bytes and upload is 1,000,000 bytes. The hard maximum per payload is 10,000,000. Downloads stream to temporary files. Uploads stream from a sparse local file. Curl has a 6-second connection and 15-second transfer timeout. DNS resolution by the host resolver before curl follows OS timeouts.

Each standalone heavy job reserves twice its payload plus 262,144 bytes in a single SQLite write transaction on enqueue. The per-config and per-station caps apply to the UTC day of reservation. Interrupted, cancelled or failed jobs retain the reservation. Recheck the size before execution. This is an estimated heavy-test allowance, not a billing meter, and does not bound all lower-layer traffic, retransmissions, lightweight checks or another agent's traffic. Reserve a separate allowance per ISP station. For recurring speed jobs use PUT /api/v1/configurations/{id}/schedules/download or /upload with query seconds=86400. Zero removes the schedule. Deactivating LAB_SPEED and restarting suspends heavy dispatch.

## Measurements and diagnoses

- All stored timestamps are UTC Unix seconds. Curl timing values are converted to milliseconds from request start. proxy_connect_ms measures the local SOCKS socket connection. tls_complete_ms is cumulative through application TLS; ttfb_ms and total_ms are cumulative through first byte and completion. These fields do not measure remote-server ping or isolate SOCKS negotiation time.
- A completed 2xx response within the byte and time bounds is a successful observation. A final server response is required for uploads. The payload transfer rate is decimal Mbps over the complete request duration. A failed or incomplete test has null Mbps.
- The baseline uses the same check endpoint directly. LAB_INTERFACE optionally reads operstate, counters and speed under /sys/class/net. LAB_BASELINE_MAX_MS > 0 also marks slow direct responses as degraded. DNS failure of a measurement target is recorded separately. No ICMP or packet capture runs.
- Default incident thresholds are three consecutive failed connectivity jobs on a valid baseline and two consecutive successes for recovery. Set LAB_CONFIRM and LAB_RECOVER to positive integers. An unconfirmed failure is degraded; ambiguous baselines break the failure streak. Heavy tests do not alter availability state. An engine startup error is a job error. A job stuck in running after a restart becomes interrupted. Job IDs and states are visible in Test console.
- Reports summarize observed connectivity jobs in the requested 1–31 day window. Median and P95 use successful request durations; P95 requires 20 successes. Periods without samples do not enter the success percentage. CSV exports include raw observations. Print the overview with the browser print dialog.
- LAB_TELEGRAM=1 plus LAB_BOT_TOKEN and LAB_CHAT_ID enables outage/recovery notifications. Keep credentials in a restricted service environment file. The transactional outbox deduplicates transitions and backs off retries. Delivery is at-least-once; a crash after remote acceptance can repeat a notification.

## Independent agents

Use one station per gateway. LAB_AGENT is its stable unique identity and LAB_ISP labels its access line. Run the local monitoring service on each agent. A central installation can accept summaries over a separately approved HTTPS endpoint. Allow the agent-batches path through that proxy. Give each agent an independent random 32-byte secret through a private file or service environment. Protect both registry and relay credentials.

On the hub, LAB_AGENT_REGISTRY points to a private JSON object shaped like
`{"MCI":{"key":"REPLACE_WITH_RANDOM_SECRET","configs":{"9":1}}}`.
This maps remote inventory ID 9 to already-created hub config ID 1. On the station set LAB_AGENT=MCI, LAB_HUB to the approved HTTPS origin, LAB_RELAY_KEY to the agent-specific secret, and LAB_DATA to the local database directory. Run `python -m lab.agents --once` for one authorized delivery or `python -m lab.agents` as a separately supervised relay. It emits heartbeats, signs batches, limits them to 100 records, and advances a local cursor after acknowledgment. Run one relay per station. Keep clocks within 60 seconds. Replays cannot duplicate samples. Use a new agent ID after rebuilding a station with reset sample IDs. The relay avoids transmitting VPN configuration secrets.

Comparison groups successful-baseline check observations by configuration, agent and method fingerprint (check implementation and target URL). Match fingerprints before comparing networks. Hardware, time of day and unobserved access congestion still affect comparisons. The remote agent is a trusted measurement source and could falsify its own readings.

## Troubleshooting and risk boundaries

- **engine_missing / engine_start**: check the service user's executable path, runtime compatibility, /proc access and generated transport support. Use dedicated test credentials. Do not inspect another VPN client's process or print raw configs.
- **peer_unapproved / peer_restricted**: check the exact imported hostname against LAB_PEERS and verify it resolves publicly. The monitoring interface cannot choose arbitrary probe destinations.
- **budget_exhausted**: inspect reservations for this UTC day; adjust limits only against an approved allowance. **live_disabled** is expected in the demo.
- **worker health false**: check service logs, SQLite permissions, available disk and blocked name resolution. A supervisor restart recovers incomplete jobs. Health returns the worker status in JSON; deployment/validate checks the value.
- **Authorization**: local single-admin scrypt credential, expiring in-memory sessions, HttpOnly SameSite cookies, mutation-header CSRF protection, no cross-origin API permissions. Restart invalidates sessions. The ingestion endpoint uses HMAC and a short time window. Protect the host and OS account. Encryption does not protect data from an attacker controlling that account. Use neutral display labels; operator-entered names are visible.
- Automatic retention cleanup is disabled by default. Monitor disk usage and back up before rotating databases. Restore tests exercise the schema, measurements and report path. Live deployment and unattended upgrades require separate review.

## Calendar summaries and retention

The scheduler finalizes the most recently closed UTC day, ISO week and calendar month during its hourly housekeeping pass. GET /api/v1/summaries returns these stored reports. Empty periods within monitoring history retain a zero check count. After a long station outage the current implementation does not backfill every historical period. LAB_SEND_SUMMARIES=1 additionally enables summary delivery when Telegram itself is enabled. Set LAB_RETENTION_DAYS to a positive integer to expire old raw local and remote samples hourly. This leaves job metadata, alerts, audit entries, incidents and finalized summaries intact. Default zero preserves raw history. A rolling report covers only retained samples.

For an optional recurring maintenance window set LAB_PAUSE_UTC to a pair such as 23:00-01:30. The station pauses new job claims during that UTC window. In-flight work can be cancelled in the console.

## Subscription download rounds on Wi-Fi or Ethernet

In **تست اشتراک / Subscription speed tests**, name the subscription, paste its exact HTTPS export URL, select an already-connected Linux interface, and choose 12 or 24 hours. Start schedules the first round immediately on the monitoring station. Edit the interface or interval with the row controls. Pausing cancels this feed's waiting jobs and requests cancellation for the active job. Resuming schedules a fresh import. Restarting the service retains the plan.

The station needs Xray, LAB_LIVE=1, LAB_SPEED=1, LAB_SCHEDULE=1, a small LAB_CHECK_URL and a LAB_DOWNLOAD_URL. Each subscription test shows ping, download and upload separately. Ping measures HTTPS response latency through the tested proxy, from connection readiness to the first byte. Older results retain the complete time to first byte. Download uses LAB_DOWNLOAD_BYTES and requires an exact-sized response. Upload uses LAB_UPLOAD_BYTES with an LAB_UPLOAD_URL endpoint that accepts POST. The manual Cloudflare button uses Cloudflare for all three speed measurements. It takes the median of five HTTPS response latency samples. For downloads and uploads, the worker starts with at most 250 kB and tries larger payloads up to the configured directional size when the previous transfer takes less than 1.5 seconds. It reports the median of up to three successful samples at the selected size, excluding connection setup and waiting for the response that confirms each upload. The result also counts all successful and attempted samples, including sizing requests. Each request allows up to 20 seconds to connect, with 30 seconds total for a latency request and 60 seconds total for a transfer. A round can take several minutes per configuration on a slow connection. Scheduled rounds use the server-side endpoints. Subscription tests do not consume the UTC-day allowances for standalone speed tests.

Deleting a subscription removes its saved URL from the active record, stops its schedule and cancels pending work. Past results stay in history.

The feed URL is encrypted at rest. Starting a plan authorizes the public peers returned by that specific feed. Each round fetches the current export bound to the chosen link and deduplicates supported outbounds. Removed entries are excluded from later rounds; history is retained. Limits are 90 KB/export and 1,000 entries. Invalid entries are counted separately. The single worker runs one download at a time and records config name, interface, outcome and Mbps. The results table displays the latest 200 jobs. A failed refresh retries in five minutes. Failed transfers await the next scheduled round.

The selector operates on connected interfaces of the station hosting the panel, including the adapter connected to an external modem. It does not join an SSID. The subscription HTTP request and direct baseline use curl's strict interface binding. The Xray peer connection goes through a local TCP relay whose upstream socket must bind with SO_BINDTODEVICE before connecting. The kernel and existing routes must permit that device. Failure stops that connection. DNS lookups use the host resolver. HTTP/3-only transport is unsupported. No physical link switching, new route, DNS changes or network-manager permissions are required.
