# Release verification

This checkout was built and exercised in an isolated Debian 13 workspace with Python 3.12, curl 8.14.1 and OpenSSL. Development traffic stayed on loopback. The personal Debian machine, production subscriptions, Telegram chat and PasarGuard panel were not involved.

| Component | Evidence |
|---|---|
| Parser / inventory | VLESS transports, decoded WS path, SIP002 password containing a colon, invalid UUID, unsupported option, nonpublic IP rejection, constrained JSON reconstruction, duplicate imports and sanitized inventory |
| Authentication | API unauthorized response, login, missing mutation-header rejection, session logout and protected reads |
| Runner lifecycle | Real subprocess creation, authenticated SOCKS readiness, engine crash, suspended process cleanup, listener released after completion |
| Probe | Real trusted and untrusted TLS over fixture SOCKS, complete vs partial download, bounded successful upload and no final acknowledgment after 100 Continue |
| API through worker to storage | Imported a configuration through HTTP, queued it, completed a local measurement, queried raw history, backed up the live DB, restored it into a fresh directory and queried the report after reopening |
| Scheduling / budgets | Due-job claim, overlapping claim avoidance, restart state, recurring heavy reservation, budget refusal, inconclusive baseline, outage confirmation and recovery deduplication |
| Notifications / reporting | Mock Telegram retry and cooldown, signed relay, replay deduplication, invalid signature, hub ID mapping, UTC leap-month boundaries, idempotent summary generation |
| Demonstration | Launched the packaged command, signed in over loopback and retrieved three successful measured observations and one healthy fixture. Three checks transferred 27 response-body bytes. These numbers are unrelated to actual MoboHub locations. |
| Frontend / packaging | JavaScript syntax, Python byte compilation and deployment shell syntax checked. HTTP served the dashboard assets. Browser automation was attempted but Chromium startup failed at its local socket in the restricted workspace. |
| Unverified | Real Xray/VLESS and Shadowsocks tunneling, XHTTP/Reality engine compatibility, real Telegram delivery, real remote export, ISP hardware, systemd installation, clean package-index install, distributed traffic on independent gateways and target machine resource use |

Run `python -m unittest discover -v`. The real VLESS integration test automatically runs when an Xray binary is on PATH. It creates its own temporary loopback server and UUID fixture. Run `LAB_BROWSER_TEST=1 python -m unittest tests.test_ui -v` on a Linux host with Playwright and Chromium. The CI definition is included; no remote CI run has been observed.

## Work still required for the full brief

- Validate the installed Xray build and the supported transport/credential combinations with real loopback Xray and, separately, an explicitly authorized remote endpoint. Test throughput and monitor memory on the intended hardware.
- Validate Debian installation, rollback, retention policy and restricted-account permissions on a clean disposable host. Verify browser interactions on a Chromium-capable system. Finish installation-level security and operational review.
- This release has one local administrator, an environment-managed policy, per-agent spending limits and persisted observations. It lacks centrally coordinated cross-station budgets, exhaustive outage correlation and an independent offline-agent alert monitor.
- Alerts currently cover confirmed outage, recovery and optional calendar summaries. Specialized TLS, degradation and offline alert policies remain open. The dashboard charts latency and throughput by config, agent and method. A complete settings editor is pending. Some field names remain technical English in Persian mode.
- Administrative PasarGuard synchronization awaits verification against the operator's installed API. The egress lookup accepts a primary provider and a fallback. The runner does not isolate SOCKS negotiation duration, perform controlled packet-loss tests or report geography. The safety fixture does not establish service availability.
- Database and frontend choices, plus the single-process deployment boundary, are described in README.md. A dependency-locking and release-signing workflow is still outstanding.

Final workspace run: 23 tests discovered, 21 passed, 2 skipped (Xray binary and Chromium). systemd-analyze accepted the unit directives with ExecStart pointing to the available workspace interpreter. The installed service path was not exercised.

Subscription update: verified base64 mixed-format exports, unsupported-entry counting, deduplication, schedule persistence/due checks, removed members, and a real bidirectional kernel-bound loopback TCP relay. A missing device fails the connect attempt. No physical Wi-Fi/LAN switch, real subscription, or ISP speed measurement was run.
