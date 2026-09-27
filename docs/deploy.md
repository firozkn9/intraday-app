# Deploying to a VM (Phase 14)

This guide sets up one Ubuntu 24.04 VM (2 vCPU / 4 GB RAM / 40 GB disk is enough) running Docker Compose. The app runs **paper trading only**.

## 1. DNS and VM

1. Create the VM and note its public IP.
2. Add a DNS `A` record, for example `trade.example.com → <IP>`.
3. Copy the provisioning script over and run it as root:
   ```bash
   scp infrastructure/scripts/provision.sh root@<IP>:/root/
   ssh root@<IP> 'bash /root/provision.sh'
   ```
4. Add your SSH key for the `intraday` user, then disable password SSH (`PasswordAuthentication no`).

## 2. Code and configuration

```bash
ssh intraday@<IP>
git clone <your-repo-url> /opt/intraday && cd /opt/intraday
cp .env.example .env && chmod 600 .env
nano .env
```

Fill in `.env`:

- `TRADING_MODE=PAPER`, `ALPACA_BASE_URL=https://paper-api.alpaca.markets`
- The Alpaca **paper** key ID and secret (optionally `ANTHROPIC_API_KEY`)
- `PUBLIC_DOMAIN`, `PUBLIC_ORIGIN=https://<domain>`
- The database passwords and matching `DATABASE_URL_*` values, plus `SESSION_SECRET`. Generate each with `python3 -c "import secrets;print(secrets.token_urlsafe(48))"`.
- Optionally the SMTP settings and `BACKUP_REMOTE`

## 3. First deploy

```bash
infrastructure/scripts/deploy.sh            # build, migrate, start, wait for health
docker compose exec api intraday create-admin
docker compose exec engine intraday backfill --days 30
```

Open `https://<domain>`. Caddy obtains the certificate on first request. Log in and check System Health.

## 4. Backups

```bash
sudo cp infrastructure/systemd/intraday-backup.* /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now intraday-backup.timer
infrastructure/scripts/backup.sh && infrastructure/scripts/restore.sh --test
```

For off-site copies, run `rclone config` (preferably with a `crypt` remote), then set `BACKUP_REMOTE`.

## 5. Go-live checklist (paper)

- [ ] The status bar shows `TRADING MODE: PAPER` and System Health shows the broker `alpaca_paper`, account ACTIVE, stream connected.
- [ ] The last reconcile is clean and there's no RECONCILING block.
- [ ] Settings were reviewed (risk limits, watchlist); the engine is **PAUSED**.
- [ ] A test alert email was received (if SMTP is set).
- [ ] You have run one supervised session: press START after 09:35 ET, watch the first entries, confirm the brackets in the Alpaca paper dashboard, and confirm the 15:55 force close and flat-at-close.

Updates: run `infrastructure/scripts/deploy.sh` outside market hours.

## Local production-like test (Phase 13)

Set `PUBLIC_DOMAIN=localhost` and `PUBLIC_ORIGIN=https://localhost` in `.env` and run `infrastructure/scripts/deploy.sh`. Caddy serves `https://localhost` with its internal CA.
