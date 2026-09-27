# Security review (Phase 13)

| Item | Scope |
|---|---|
| Reviewed | 2026-09-27 |
| Scope | The single-VM deployment described in docs/design/11 |
| Result | No open high-severity findings. Remaining risks are listed at the end. |

## Checks

| Area | Status | How it's enforced |
|---|---|---|
| Paper only | ✅ | Three guards: engine boot (`core/safety.py`), the broker factory, and the `AlpacaPaperBroker` constructor. `FutureLiveBroker` raises `NotImplementedError`. `deploy.sh` refuses a non-paper `.env`. There is no API endpoint that changes trading mode. |
| Broker credentials are not in the API | ✅ | In compose, only the `engine` service receives `ALPACA_*` and `ANTHROPIC_API_KEY` (a test asserts this). import-linter forbids `intraday.api` from importing `trading.broker`, `marketdata.alpaca` or `engine`. |
| Secrets in git | ✅ | `.env` is git-ignored and only `.env.example` (placeholders) is tracked. `check_secrets.py` runs in CI and also scans untracked files. |
| Authentication | ✅ | argon2id hashes, server-side sessions (httpOnly, Secure, SameSite=Strict), lockout after 10 failures, rate limits on login and mutations, audit log. |
| CSRF | ✅ | Double-submit token on every unsafe method, plus SameSite=Strict cookies. |
| Dangerous actions | ✅ | Emergency stop, close-all and close-position need a typed confirmation. Latch resets need the password. Everything is audited. |
| LLM data minimisation | ✅ | A whitelist DTO (`ai/providers.py`) has no balance, position, P&L, account or order fields. News is wrapped as untrusted data. The LLM can only remove trades. |
| SSRF surfaces | ✅ | The API makes no outbound requests. The engine calls only fixed hosts (Alpaca paper/data, Anthropic). News URLs are stored and never fetched. |
| TLS and headers | ✅ | Caddy provides automatic HTTPS, HSTS, a strict CSP, `nosniff`, `no-referrer` and `frame-ancestors 'none'`. `/api/v1/metrics` is not proxied. |
| Containers | ✅ | Non-root (uid 10001), read-only root filesystems, `no-new-privileges`, memory and CPU limits. Postgres and Redis sit on an internal network with no egress. Only the engine has internet egress. |
| Database roles | ✅ | Separate owner (migrations), api and engine roles. The API role has no rights to modify trading tables. |
| Host | ✅ | `provision.sh` sets up ufw (22/80/443 only), fail2ban, unattended-upgrades, and a non-root deploy user. |
| Dependencies | ✅ | `pip-audit` found no known vulnerabilities and `npm audit --omit=dev` found 0 on 2026-09-27. Both now run in CI. |
| Backups | ✅ | A nightly `pg_dump` (mode 600, 700 directory), 14-day retention, an optional rclone off-site copy, and a scripted restore test. |

## Remaining risks

- **SSH:** use key-only login. `provision.sh` does not change `sshd_config`; set `PasswordAuthentication no` yourself after adding your key.
- **Off-site backups** depend on how rclone is configured. Encrypt the remote (rclone `crypt`).
- **Single admin:** there's no 2FA in V1. Use a long unique password and keep the domain private.
- **Alert email** goes over SMTP with STARTTLS. Alert text contains symbols and prices, never credentials.
