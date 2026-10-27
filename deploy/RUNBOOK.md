# Production runbook: OCI VM (Phase 1.3)

From an empty Oracle Cloud account to the Placement Engine running behind a Cloudflare
named tunnel, verified, backed up and surviving reboots, then the switch to the shadow run.
Backups, restores and rolling back a deploy are in `deploy/BACKUP.md`.

**Target:** OCI Ampere A1 Flex (arm64) on Ubuntu 24.04. Fallback: VM.Standard.E2.1.Micro
(amd64, 1 GB RAM) with a 2 GB swapfile (§7). Everything else is identical on both.

**End state**

| Item | Value |
|---|---|
| Login | `deploy` only, SSH key only; `ubuntu` locked; root login off |
| Open inbound ports | 22/tcp only (OCI security list **and** UFW). Nothing else listens publicly |
| Public URL | `https://<hostname>` → Cloudflare tunnel → `api:8000` on an internal Docker network |
| Host layout | `/srv/placement/app` (checkout, `deploy`), `secrets/` (`deploy`, 0700, files 0600), `data/` `backups/` `telegram/` (UID 10001, 0700) |
| Boot | `placement-stack.service` runs `docker compose up -d`; `placement-backup.timer` daily at 03:00 IST |
| Updates | Ubuntu security updates automatic, reboot at 04:00 IST when needed |

Placeholders: `<public-ip>` is the instance's public IPv4, `<hostname>` your tunnel hostname
(e.g. `placement.example.com`), `<you>@<laptop>` your own machine. Commands run on the VM
unless they say *laptop*.

---

## 1. Create the instance (OCI console)

1. *Laptop:* create a key pair for the VM: `ssh-keygen -t ed25519 -f ~/.ssh/oci_placement`
   (works in PowerShell too). Give it a passphrase.
2. **Compute → Instances → Create instance**
   - Name `placement-vm`; image **Canonical Ubuntu 24.04** (the full image, not Minimal).
   - Shape **VM.Standard.A1.Flex**, 2 OCPU / 12 GB is plenty (Always Free allows 4 / 24 in
     total). If A1 is out of capacity, use **VM.Standard.E2.1.Micro** and do §7.
   - Networking: a new VCN with a public subnet, **assign a public IPv4 address**.
   - SSH keys: paste `~/.ssh/oci_placement.pub`.
   - Boot volume: the default (≈ 47–50 GB). The database lives here, on the local ext4
     filesystem. Never move `data/` to a network filesystem or block volume shared over NFS.
3. **Security list** (Networking → Virtual cloud networks → your VCN → Security → Default
   security list):
   - Ingress: keep **TCP 22** (restrict the source to your own IP/CIDR if it is stable) and
     the two default ICMP rules (type 3 code 4, and type 3 from the VCN). **Delete any other
     ingress rule** (80, 443, 8000 …): the tunnel only connects outbound.
   - Egress: keep **all traffic to 0.0.0.0/0** (apt, Docker, Telegram, Groq, Cloudflare).
4. Note `<public-ip>`. SSH is the only thing that uses it; the tunnel does not.

> Oracle may reclaim Always Free instances that stay idle on free-tier accounts, and this
> workload is light. Upgrading the account to Pay As You Go keeps Always Free resources free
> and removes that risk. Either way, keep off-VM backup copies (BACKUP.md §3).

## 2. First login and updates

```bash
ssh -i ~/.ssh/oci_placement ubuntu@<public-ip>          # laptop
sudo apt-get update && sudo apt-get full-upgrade -y
timedatectl                                             # "System clock synchronized: yes", Time zone: UTC
sudo reboot
```

Keep the VM on **UTC**: the backup timer and the automatic reboot time below assume it.
Telegram rejects clients whose clock is far off, so the clock must be synchronized.

## 3. The `deploy` user

```bash
sudo adduser --gecos "" deploy        # set a strong password: used for sudo and the OCI serial console
sudo usermod -aG sudo deploy
sudo install -d -m 0700 -o deploy -g deploy /home/deploy/.ssh
sudo install -m 0600 -o deploy -g deploy ~/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
```

*Laptop, in a second terminal* — keep the `ubuntu` session open until this works:

```bash
ssh -i ~/.ssh/oci_placement deploy@<public-ip>
sudo -v                               # asks for deploy's password
```

From here on, work as `deploy`.

## 4. SSH hardening

```bash
sudo tee /etc/ssh/sshd_config.d/00-placement.conf >/dev/null <<'EOF'
# Read before the cloud image's own drop-ins: the first value wins.
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AuthenticationMethods publickey
AllowUsers deploy
MaxAuthTries 3
LoginGraceTime 30
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding no
EOF
sudo sshd -t && sudo systemctl restart ssh
sudo sshd -T | grep -Ei '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication|allowusers) '
```

Expect `permitrootlogin no`, `passwordauthentication no`, `kbdinteractiveauthentication no`,
`allowusers deploy`. Open a **new** SSH session as `deploy` to confirm, then lock `ubuntu`:

```bash
sudo passwd -l ubuntu
sudo rm -f /etc/sudoers.d/90-cloud-init-users        # cloud-init's passwordless sudo for ubuntu
ssh -i ~/.ssh/oci_placement ubuntu@<public-ip>       # laptop: must now be refused
```

## 5. Firewall: replace Oracle's iptables rules with UFW

Do this **before installing Docker**: flushing iptables later would delete Docker's chains.

Oracle's Ubuntu images load their own rules (`/etc/iptables/rules.v4` through
`netfilter-persistent`), which conflict with UFW. Replace them. Setting the policies to
ACCEPT before flushing means your SSH session is never cut:

```bash
sudo cp /etc/iptables/rules.v4 /root/rules.v4.oci 2>/dev/null; sudo cp /etc/iptables/rules.v6 /root/rules.v6.oci 2>/dev/null
for t in iptables ip6tables; do
  sudo $t -P INPUT ACCEPT; sudo $t -P FORWARD ACCEPT; sudo $t -P OUTPUT ACCEPT
  sudo $t -F; sudo $t -X
done
sudo apt-get purge -y iptables-persistent netfilter-persistent

sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw limit OpenSSH                 # allow 22/tcp, rate-limited
sudo ufw enable
sudo ufw status verbose
```

Expect `Status: active`, `Default: deny (incoming), allow (outgoing)` and `22/tcp (OpenSSH) LIMIT IN`.

- Outgoing traffic stays allowed, which keeps the OCI services Oracle's rules protected
  (metadata, NTP and DNS on 169.254.0.0/16; iSCSI to the boot volume on some shapes) reachable.
- Docker bypasses UFW for **published** ports. The compose file publishes none
  (`verify_deploy.py` enforces it), so UFW and the security list are the whole inbound surface.
- Check in a **new** SSH session that you can still log in, and after the reboot in §9 that
  `sudo iptables -S | grep -c InstanceServices` is 0: none of Oracle's rules came back.

## 6. Automatic security updates

```bash
sudo apt-get install -y unattended-upgrades
sudo dpkg-reconfigure -f noninteractive unattended-upgrades
sudo tee /etc/apt/apt.conf.d/52placement-upgrades >/dev/null <<'EOF'
// Reboot when an update needs it, at 22:30 UTC = 04:00 IST (after the 03:00 IST backup).
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "22:30";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
EOF
cat /etc/apt/apt.conf.d/20auto-upgrades     # both lines "1"
sudo unattended-upgrade --dry-run 2>&1 | tail -3
```

Only Ubuntu's security updates are automatic. Docker's packages come from Docker's
repository and are upgraded by hand (§18).

## 7. Swap (E2.1.Micro only)

Skip on A1. On the 1 GB E2.1.Micro, the image build and the three containers need swap:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-placement-swap.conf && sudo sysctl --system
free -h                                     # Swap: 2.0Gi
```

## 8. Docker from Docker's repository

Not Ubuntu's `docker.io`. The Compose v2 plugin is required (`docker compose`, not `docker-compose`).

```bash
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
gpg --show-keys /etc/apt/keyrings/docker.asc   # fingerprint 9DC8 5822 9FC7 DD38 854A  E2D8 8D81 803C 0EBF CD88
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker deploy
```

Log out and back in (group membership), then:

```bash
docker version --format '{{.Server.Version}} {{.Server.Arch}}'   # arm64 on A1, amd64 on E2
docker compose version
systemctl is-enabled docker                                      # enabled
```

The `docker` group is root-equivalent: only `deploy` is in it.

## 9. Directories

```bash
sudo install -d -o deploy -g deploy -m 0755 /srv/placement
sudo install -d -o deploy -g deploy -m 0700 /srv/placement/secrets
sudo install -d -o 10001 -g 10001 -m 0700 /srv/placement/data /srv/placement/backups /srv/placement/telegram
ls -ln /srv/placement
findmnt -no FSTYPE -T /srv/placement        # ext4 (a local filesystem; never NFS)
```

| Path | Owner | Mode | Holds |
|---|---|---|---|
| `/srv/placement/app` | deploy | (git) | The checkout (§10) |
| `/srv/placement/secrets` | deploy | 0700, files 0600 | `api.env`, `worker.env`, `cloudflared.env` (read by the docker CLI) |
| `/srv/placement/data` | 10001 | 0700 | `placement.db` + `-wal`/`-shm` |
| `/srv/placement/backups` | 10001 | 0700 | `placement-<UTC>.db.gz` |
| `/srv/placement/telegram` | 10001 | 0700 | The Telethon session (worker only) |

UID 10001 is the image's `placement` user. The compose file never creates a missing
directory (it would be root-owned and unwritable), so a missing one fails `docker compose` loudly.

Then reboot once (`sudo reboot`) and check the firewall and SSH came back as in §5.

## 10. Code and image

Push the branch or tag you deploy to GitHub first (owner, from the laptop). Then give the VM
read-only access with a deploy key:

```bash
ssh-keygen -t ed25519 -C placement-vm -f ~/.ssh/github_placement -N ""
cat ~/.ssh/github_placement.pub     # GitHub → scn5/placement-engine → Settings → Deploy keys → Add (read-only)
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_placement
  IdentitiesOnly yes
EOF
ssh -T git@github.com               # compare the host key fingerprint with GitHub's published ones, then "yes"
git clone git@github.com:scn5/placement-engine.git /srv/placement/app
cd /srv/placement/app
git checkout <branch or tag>        # e.g. feature/phase-1-infra until it is merged
```

Build the image, then run every test suite inside it (Python 3.12, this CPU):

```bash
docker compose build                # placement-engine:local
docker build --target test .        # all verify_*.py suites; must end with every suite green
docker image prune -f               # drop the test stage's dangling image
```

The first build takes a few minutes on A1 and much longer on E2. If a wheel is missing
for this CPU, the build fails rather than compiling: report it, don't work around it.

## 11. Secrets

Never paste secret values into chats, tickets or shared terminals. Keep a copy of each value
in your password manager: none of them are in a backup.

**API key** (the app's `X-API-Key`; at least 32 characters):

```bash
umask 077
KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')
printf 'API_KEY=%s\n' "$KEY" > /srv/placement/secrets/api.env
echo "$KEY"; unset KEY              # copy it into your password manager now; you type it into the app in §15
```

**Worker** settings from the template:

```bash
install -m 0600 deploy/templates/worker.env.example /srv/placement/secrets/worker.env
nano /srv/placement/secrets/worker.env
```

Uncomment and fill in:

- `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` (my.telegram.org → API development tools).
- `LLM_API_KEY` (Groq).
- `TEST_FEED_CHANNEL_ID` (the `-100…` ID of your TEST root feed).
- Optionally `MAIN_FEED_CHANNEL_ID` already; nothing reads MAIN until §17.
- **Recommended:** `DEADMAN_URL`, a dead-man's-switch check URL such as a healthchecks.io
  check, which the worker pings every 5 minutes while Telegram is connected. Nothing else
  alerts you when ingestion stops.

Leave `INGEST_ENVIRONMENTS=TEST`.

**Tunnel token** (create the tunnel in §12 first, then come back):

```bash
install -m 0600 deploy/templates/cloudflared.env.example /srv/placement/secrets/cloudflared.env
nano /srv/placement/secrets/cloudflared.env           # TUNNEL_TOKEN=<token>
ls -l /srv/placement/secrets                          # three files, -rw------- deploy deploy
```

## 12. Cloudflare named tunnel (token mode)

In the Cloudflare dashboard, with your domain already on Cloudflare:

1. **Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared**, name it `placement-vm`.
2. On the install screen choose **Docker** and copy only the token, the long string after
   `--token`. Paste it into `cloudflared.env` (§11). Do not run the command shown; the
   compose file runs cloudflared.
3. Add a **public hostname** (called a published application route in newer dashboards):
   subdomain and domain → `<hostname>`; service type **HTTP**; URL **`api:8000`**.
4. Leave Cloudflare Access off for this hostname: the app authenticates with the API key and
   cannot pass an Access login. If the app later gets HTML challenge pages instead of JSON,
   check Security → Bots (Bot Fight Mode) for this hostname.

The tunnel connects outbound only (port 7844), so no inbound rule is needed. Cloudflare
terminates HTTPS, and nothing on the VM handles TLS.

## 13. Telegram login (once, interactive)

This creates a **new** session for the VM, distinct from the one on your laptop. Never copy
`placement_session.session` to the VM.

```bash
cd /srv/placement/app
docker compose run --rm -it worker python scripts/login_telegram.py
```

- `migrate` runs first and creates the empty database.
- Enter the phone number in international format, the code Telegram sends to your app, and
  your 2FA password if set. Telegram shows a "new login" notice for the VM; that is this.
- Expect `Telegram authentication successful!` and `Session file : /var/lib/placement/telegram/placement_session.session`.

```bash
sudo ls -ln /srv/placement/telegram         # placement_session.session, owner 10001, -rw-------
```

From now on, keep your **laptop's** worker offline (`$env:TELEGRAM_API_ID="0"`): two
workers on two sessions of one account would both join channels and both spend LLM calls.

## 14. First boot (TEST only)

Install the boot unit, which also starts the stack now:

```bash
cd /srv/placement/app
sudo cp deploy/systemd/placement-stack.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now placement-stack
docker compose ps -a
```

Expect:

- `migrate` `Exited (0)`
- `api` `Up … (healthy)`
- `worker` `Up`
- `cloudflared` `Up`

Then the logs:

```bash
docker compose logs migrate | tail -3                         # alembic: ... upgrade -> head (or nothing to do)
docker compose logs worker | grep -E "Ingestion worker active|ERROR"
#   Ingestion worker active for TEST. Root feeds: Main=..., Test=-100...; N subchannel(s).
docker compose logs cloudflared | grep -c "Registered tunnel connection"   # 4
```

**Why a unit and not just `restart:` policies:** at shutdown Docker stops the worker with
SIGTERM, the worker exits 0 after flushing, and `restart: on-failure` would then leave it
down after every reboot, including the automatic ones from §6. `placement-stack.service`
runs `docker compose up -d` at boot and `docker compose stop` at shutdown, giving the
worker its full 45 s to flush. A Docker restart, such as a docker-ce upgrade, restarts the
unit with it.

**Backups:** set up the timer now, following `deploy/BACKUP.md` §2 (`backups/` already
exists from §9). Expect `backup ok` in the journal.

**Day-to-day commands** (from `/srv/placement/app`):

| Purpose | Command |
|---|---|
| State | `docker compose ps -a` |
| Worker logs | `docker compose logs -f --since 30m worker` |
| Restart one service | `docker compose restart worker` |
| Apply an edited `*.env` | `docker compose up -d --force-recreate <service>` |
| Stop everything | `sudo systemctl stop placement-stack` (`start` to bring it back) |

## 15. Verification

Work through every row. Do not start the shadow run (§17) until all of them pass.

**From the laptop** (bash or Git Bash; in PowerShell use `curl.exe`). `read -rs KEY` keeps the key out of your
shell history.

```bash
H=https://<hostname>; read -rs KEY
curl -sS $H/health                                              # {"status":"ok"}
curl -sS -o /dev/null -w '%{http_code}\n' $H/status             # 401
curl -sS -H "X-API-Key: $KEY" $H/status                         # "status":"ok", "telegram_state":"CONNECTED"
curl -sS -o /dev/null -w '%{http_code}\n' $H/docs               # 404 (docs are off in production)
nc -vz -w 3 <public-ip> 8000          # must fail (PowerShell: Test-NetConnection <public-ip> -Port 8000 → False)
```

`/status` can say `unknown` for the first ~30 s, until the first heartbeat.

**On the VM**

```bash
sudo ss -tlnp            # only sshd on :22 (and systemd-resolved on 127.0.0.53/54): nothing on 8000
sudo ufw status          # Status: active
systemctl is-active placement-stack docker                     # active, active
systemctl list-timers placement-backup.timer                   # next run tonight 21:30 UTC (+ up to 15 min)
journalctl -u placement-backup -n 5 --no-pager                 # backup ok: placement-...
deploy/restore.sh --list                                       # that archive
```

**App pairing.** The new EAS build has secure storage and pairing.

1. Open the app. The pairing screen asks for the server URL and the API key: enter
   `https://<hostname>` and the key.
2. **Verify** must show *ingestion running*. *Ingestion not started yet* means no heartbeat yet
   (wait 30 s); *ingestion degraded* means check the worker logs.
3. With the environment toggle on **TEST**, post a test placement announcement in your TEST
   feed. It shows up in the TEST timeline within about 2–3 minutes (90 s debounce plus
   extraction). `docker compose logs --since 10m worker` shows the message being processed.

**Reboot persistence.** This is the check that `placement-stack.service` exists for.

```bash
sudo reboot
# reconnect after a minute
docker compose -f /srv/placement/app/docker-compose.yml ps -a   # all four as in §14, worker "Up"
journalctl -b -1 -u placement-stack --no-pager | tail -3        # the previous boot stopped the stack cleanly
docker compose -f /srv/placement/app/docker-compose.yml logs --since 15m worker | grep -E "Stop requested|Ingestion worker active"
sudo ufw status | head -1; sudo iptables -S | grep -c InstanceServices   # active; 0 (no Oracle rules came back)
```

Then repeat the `/status` call from the laptop (`ok`) and refresh the app.

**Recorded result:** note the date, the `git rev-parse --short HEAD` that is deployed, and
"all checks passed" in your own log or PROJECT_STATE.

## 16. Running TEST for a while

Keep TEST only for at least a day. Check `/status` in the app, the dead-man's switch, and
that one nightly backup ran (BACKUP.md §3). Post a few TEST announcements of the kinds you
see in real feeds: deadline changes, links, replies.

## 17. Start the shadow run: TEST → MAIN,TEST

**What changes:** the worker starts reading the real MAIN root feed and the company
channels linked from it. Under the join policy, it **joins** channels and supergroups linked
from MAIN root-feed posts. It never joins persons, bots, basic groups or approval-only
invites. Joining is the only change it makes to the Telegram account; it never sends,
leaves or deletes anything.

**Before:**

- All §15 checks pass and §16 ran clean.
- The account is a member of the MAIN root feed, and you know its `-100…` ID.
- `DEADMAN_URL` is set (recommended).
- The Groq account has room: the boot catch-up runs up to 72 h of MAIN history through the LLM.

**Switch:**

```bash
cd /srv/placement/app
deploy/backup.sh --tag pre-shadow                          # a restore point without MAIN data
nano /srv/placement/secrets/worker.env
#   MAIN_FEED_CHANNEL_ID=-100...      (or set it in the app's Settings screen; both are merged)
#   INGEST_ENVIRONMENTS=MAIN,TEST
docker compose up -d --force-recreate worker               # 45 s graceful stop, then the new settings
docker compose logs -f worker
#   Ingestion worker active for MAIN,TEST. Root feeds: Main=-100..., Test=-100...; N subchannel(s).
#   Catch-up routed ... for chat_id=...      (the MAIN backlog, up to 72 h)
```

Watch the first catch-up finish: no `ERROR` lines and no repeated rate-limit warnings. Then
switch the app's toggle to **MAIN**: companies and events from the real feed appear. Review
the triage queue.

**During the shadow run (1–2 weeks).** The app is not trusted yet and no reminders are
scheduled yet (`command-center/PROJECT_STATE.md` §10–§12), so keep tracking placements the way you do today and use
the app alongside.

- **Daily:**
  - The app shows *ingestion running*, and the dead-man's switch is green.
  - Compare each real announcement with the app: is it missing, is the date or role
    wrong, is it duplicated? Note each miss with the message link and time. This data
    decides the next fixes and the `LLM_REASONING_EFFORT` setting.
  - `docker compose logs --since 24h worker | grep -cE "ERROR|WARNING"`: look at anything new.
- **Weekly:** the backup checks in BACKUP.md §3, `df -h /` (disk), and `docker system df`.

**Exit criteria (owner's call), suggested:** 14 days in which every announcement with a
deadline reached the app with the right date, with no unexplained *degraded* periods and a
backup every night.

**Pause or end the shadow run:** set `INGEST_ENVIRONMENTS=TEST` again and
`docker compose up -d --force-recreate worker`.

- MAIN data stays in the database, marked `MAIN` and visible only with the MAIN toggle.
- Channels already joined stay joined. Leave them in Telegram yourself if you want; the
  system never leaves chats.
- To discard the MAIN data entirely, restore the `pre-shadow` backup (BACKUP.md §4).

## 18. Operations

**Deploy an update**

```bash
cd /srv/placement/app
deploy/backup.sh --tag pre-deploy
git fetch && git checkout <commit or tag>          # or: git pull on the deployed branch
docker build --target test .                       # every suite on the VM first
docker compose up -d --build                       # rebuild; migrate runs before api and worker
docker compose ps -a && docker image prune -f
```

Then repeat the `/status` check. If the new version misbehaves, follow BACKUP.md §6.

**Docker upgrades** (monthly, by hand):

```bash
sudo apt-get update && sudo apt-get upgrade        # restarts dockerd → placement-stack restarts the stack
docker compose -f /srv/placement/app/docker-compose.yml ps -a
```

**Rotate the API key:** generate a new key into `api.env` (§11),
`docker compose up -d --force-recreate api`, then pair the app again (a 401 sends it back to
the pairing screen).

**Rotate the tunnel token:** refresh or recreate the token in the tunnel's settings, put the new
one in `cloudflared.env`, then `docker compose up -d --force-recreate cloudflared`.

**Telegram session revoked or logged out** (`/status` is degraded with `telegram_state`
`UNAUTHORIZED` or `SESSION_INVALID`; the worker retries every 10 minutes):

```bash
docker compose stop worker
sudo rm /srv/placement/telegram/placement_session.session
docker compose run --rm -it worker python scripts/login_telegram.py
docker compose up -d worker
```

**Lost SSH access:** OCI console → the instance → **Console connection** → launch the Cloud
Shell connection, and log in as `deploy` with its password.

**Never:**

- run two workers on one session;
- copy the laptop session to the VM;
- publish a port;
- run a plain `VACUUM` or `cp` on the live database (BACKUP.md §1);
- put secrets in the checkout.

## 19. Troubleshooting

| Symptom | Cause, fix |
|---|---|
| `env file /srv/placement/secrets/….env not found` | Create it (§11). Compose reads all three on every command |
| `bind source path does not exist: /srv/placement/…` | Create the directory with the right owner (§9) |
| `migrate` exits 1, `unable to open database file` | `data/` is not owned by 10001 or not mode 0700 (§9) |
| `migrate` exits 1, `Can't locate revision` | The database is newer than the code: BACKUP.md §6 |
| `api` restarting, log mentions `API_KEY` | `api.env` is missing a key of at least 32 characters (§11) |
| `worker` exits at once: `Telegram is disabled` | `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` are not set in `worker.env` |
| `/status` degraded, `telegram_state` `UNAUTHORIZED` | No or invalid session: §13, or the revoked-session steps in §18 |
| `cloudflared`: `Unauthorized` / invalid token | Wrong `TUNNEL_TOKEN` (§12) |
| `https://<hostname>` returns 502 | `api` not healthy (`docker compose ps`), or the public hostname's service is not `http://api:8000` |
| The app gets an HTML page instead of JSON | Cloudflare Access or a bot challenge on the hostname (§12, step 4) |
| `permission denied … docker.sock` | Log out and in again after `usermod -aG docker deploy` (§8) |
| Worker not running after a reboot | `systemctl status placement-stack`, `journalctl -u placement-stack -b` |
| No outbound connections (apt, Telegram) | `sudo ufw status verbose` must say `allow (outgoing)`; the security list's egress rule must be present |
