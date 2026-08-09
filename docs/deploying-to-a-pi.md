# Moving to a new Pi

Notes for putting the bot and the warehouse on one fresh Raspberry Pi. Written
against the current setup: bot on the x86_64 workstation, Postgres 13 on
`thegoldenunasinn` (192.168.1.165), a 32-bit Raspberry Pi OS bullseye box.

The headline is that a new Pi lets you run both machines at once. Dump, restore,
verify, *then* switch. Nothing here has to be done under pressure, and the old Pi
stays a working rollback for as long as you want one.

**The old Pi is not being retired.** It stays in service for other projects and
keeps the name `thegoldenunasinn`. The new board gets a name of its own and keeps
it — there is no rename step anywhere in this guide, and never a moment when two
machines answer to one name. The new name is written `newpi` throughout; that is
the one string to find-and-replace once you've picked it.

That decision costs exactly one thing, and it is the sharpest edge in the move: a
config left pointing at `thegoldenunasinn` **will not fail**. It will connect —
to the old warehouse — and the two copies will drift apart with nothing to tell
you. A retired Pi would have given you a connection error within seconds. Steps 6
and 8 are what replace that missing safety net, so don't skip either.

---

## The three machines

Three boxes are involved and every command below belongs to exactly one of them.
Each command block is tagged with the box it runs on.

| tag | machine | what it is | reached by |
|---|---|---|---|
| **[ WORKSTATION ]** | the x86_64 desktop | where the bot runs today, and where this repo lives at `/home/weezy/activity/WojBot` | you're sitting at it |
| **[ OLD PI ]** | `thegoldenunasinn` | 192.168.1.165 — Postgres 13.23, bullseye, 32-bit. Stays in service. | `ssh thegoldenunasinn` (currently aliased `raspi`) |
| **[ NEW PI ]** | `newpi` | the fresh 64-bit board — ends up running *both* Postgres and the bot | `ssh newpi`, once step 2 sets it up |

When in doubt, `hostname` settles it. The dangerous confusions are all
old-Pi-versus-new-Pi, and both answer to `sudo -u postgres psql` with a
plausible-looking `wojbot_db`.

Anything below tagged **[ WORKSTATION ]** that mentions a path is relative to
`/home/weezy/activity/WojBot` unless it says otherwise; the copies on both Pis
live at `~/WojBot`.

## The move at a glance

| step | runs on | what happens |
|---|---|---|
| 1 | OLD PI → WORKSTATION | dump roles + data, record counts, check password hashes, copy the dumps off |
| 2 | NEW PI + WORKSTATION | image, hostname, OS account, packages, DHCP reservation, ssh alias |
| 3 | WORKSTATION → NEW PI | ship the dump over, restore it, re-set passwords, verify |
| 4 | NEW PI | clone the repo, build the venv from the lock |
| 5 | WORKSTATION | rsync the gitignored state the clone doesn't carry |
| 6 | NEW PI | repoint three config files at localhost |
| 7 | NEW PI | systemd unit |
| 8 | everywhere | cut over, prove the writes land on the new box, close the route to the old one |

Steps 1–7 are all reversible and none of them touch the running bot. Step 8 is
the only one that changes what's live.

---

## Why the old Pi can't just take the bot

```
PostgreSQL 13.23 (Raspbian 13.23-0+deb11u3) on arm-unknown-linux-gnueabihf, 32-bit
```

- **bullseye ships Python 3.9**, and `pyproject.toml` requires `>=3.11`.
- **32-bit armhf** has no wheels for `numpy 2.5.1` / `pandas 3.0.5`. piwheels
  builds armv7l wheels, but only for the distro's default Python, so a
  hand-built 3.11 wouldn't match them and you'd compile pandas from source on a
  Pi. Hours, and it usually runs out of memory.

So the fresh install has to be **64-bit**. That's the whole reason this is a
migration rather than a copy.

## Before you start: image choice

| | Python | Postgres |
|---|---|---|
| Raspberry Pi OS **Bookworm** 64-bit | 3.11 | 15 |
| Raspberry Pi OS **Trixie** 64-bit | 3.13 | 17 |

Either works. Trixie is the longer runway; Bookworm is the better-trodden path
on Pi hardware, and is the floor for a Pi 5 regardless.

On a **Pi 5 the 32-bit trap can't happen** — Raspberry Pi OS is 64-bit only for
that board, so there is no wrong image to flash. `uname -m` should still say
`aarch64`; check it once and move on. (On a Pi 4 the check matters, because both
images exist and the 32-bit one is what put the old box where it is.)

**Boot from an SSD, not an SD card.** Postgres writes constantly; SD cards wear
out under it and stall for seconds at a time doing wear-levelling, which from the
outside looks exactly like the host disappearing. Worth doing on its own merits,
and a plausible contributor to the flapping (see the last section).

No SD card is needed at all on a Pi 4 or 5 — the bootloader lives in onboard SPI
EEPROM, so the whole system can sit on USB. (That's a Pi 4-and-later thing; on a
Pi 3 and earlier the first-stage bootloader could only read the SD slot, which is
where the "a Pi needs an SD card" rule comes from.) On a Pi 5 an NVMe drive on
the PCIe connector via an M.2 HAT is better still.

Boot order defaults to SD first, then USB, so with no card inserted it falls
through on its own; set it explicitly with `raspi-config` → Advanced → Boot
Order, or `rpi-eeprom-config`. Run `sudo rpi-eeprom-update` once on a new board
regardless.

## Accounts: what's an OS user and what's a Postgres role

Worth getting straight before step 2, because the two get conflated and only one
of them is your problem.

**Postgres roles — you never create these by hand.** `pylot` (the bot),
`piders994` (owns the tables), `moderator`, `power_user`, `manager` and
`consumer` all live inside the cluster, and `pg_dumpall` recreates every one of
them, passwords and role memberships included. Pre-creating them on the new Pi
doesn't help and actively hurts — see the warning in step 3.

**OS users — you need exactly one, `weezy`.** The systemd unit in step 7 runs as
`User=weezy` out of `/home/weezy/WojBot`, and the rsync in step 5 targets
`newpi:WojBot/`. Name the primary account `weezy` in Raspberry Pi Imager's "Set
username and password" panel and the whole guide lines up as written; call it
anything else and you own the job of editing both. `postgres` arrives with the
package. **Nothing needs an OS account named `pylot`, `piders994` or
`moderator`** — that is exactly why step 6 connects over `127.0.0.1` instead of
the Unix socket, since a `local ... peer` rule *would* demand one.

---

## 1. Dump everything

Roles matter as much as data here: `pylot` (the bot), `moderator`, `consumer`,
`power_user` and the ownership by `piders994` all have to come across, or the
bot's grants silently differ. `pg_dumpall` captures roles; `pg_dump` alone
does not.

**[ OLD PI ]**

```bash
sudo -u postgres pg_dumpall > wojbot-full-$(date +%F).sql
sudo -u postgres pg_dump -Fc wojbot_db > wojbot_db-$(date +%F).dump   # belt and braces
```

Run `pg_dumpall` **as a superuser** (which `sudo -u postgres` gets you) and
without `--no-role-passwords`, or the role passwords are silently left out of the
dump and `pylot` restores as a login role that can't log in.

A Postgres 13 client dumping a Postgres 13 server, restored into 15 or 17, is the
supported direction and needs nothing special.

Record what you expect to see on the other side:

**[ OLD PI ]** — `sudo -u postgres psql wojbot_db`

```sql
SELECT 'dim_league' t, count(*) FROM fantasy_sports.dim_league
UNION ALL SELECT 'dim_manager',          count(*) FROM fantasy_sports.dim_manager
UNION ALL SELECT 'dim_manager_platform', count(*) FROM fantasy_sports.dim_manager_platform
UNION ALL SELECT 'dim_team',             count(*) FROM fantasy_sports.dim_team
UNION ALL SELECT 'dim_online_league',    count(*) FROM fantasy_sports.dim_online_league
UNION ALL SELECT 'dim_source_type',      count(*) FROM fantasy_sports.dim_source_type
UNION ALL SELECT 'dim_rumor_form',       count(*) FROM fantasy_sports.dim_rumor_form
UNION ALL SELECT 'dim_release_type',     count(*) FROM fantasy_sports.dim_release_type
UNION ALL SELECT 'fact_rumor',           count(*) FROM fantasy_sports.fact_rumor;
```

At time of writing: source types **14**, rumor forms **14**, release types **10**,
leagues **2**.

While you're on the old box, check **how the passwords are hashed**, because it
decides whether step 3 needs its extra move:

**[ OLD PI ]**

```bash
sudo -u postgres psql -Atc \
  "SELECT rolname, left(rolpassword, 4) FROM pg_authid WHERE rolcanlogin"
```

Postgres 13 defaults `password_encryption` to **md5**; the default only flipped
to `scram-sha-256` in 14. So expect `md5…` here, and see the warning in step 3 —
those hashes restore fine and then fail to authenticate against a fresh 15/17
`pg_hba.conf`.

Now copy both dumps **off the Pi** before you touch anything:

**[ WORKSTATION ]**

```bash
mkdir -p ~/backups
scp thegoldenunasinn:wojbot-*.sql thegoldenunasinn:wojbot_db-*.dump ~/backups/
```

## 2. Base system on the new Pi

Give it its **own hostname** — `newpi` here — and leave it that way.
`thegoldenunasinn` belongs to the old board and stays with it. Set the primary
user to `weezy` while you're in the imager, per the accounts section above.

**[ NEW PI ]**

```bash
sudo apt update && sudo apt install postgresql git python3-venv rsync
uname -m        # must print aarch64
python3 -V      # must be >= 3.11
hostname        # must NOT print thegoldenunasinn
psql --version  # 15 on bookworm, 17 on trixie — note it, you need it for paths below
```

Installing the `postgresql` package leaves you with a running, empty cluster.
That is precisely the state step 3 wants: **don't create the database, the
schemas or the roles yet.**

Its config files live under a version-numbered directory, which is the one thing
that differs between the two images:

**[ NEW PI ]**

```bash
pg_lsclusters                        # confirms version + data/config paths
ls /etc/postgresql/*/main/           # postgresql.conf, pg_hba.conf live here
```

Then, off the Pi:

**[ WORKSTATION ]**

- Set a **DHCP reservation** on the FiOS gateway for the new board so its
  address never moves. The old one keeps `192.168.1.165`, and now needs a
  reservation of its own if it never had one — two Pis competing for leases is a
  new problem.
- Add an ssh alias for the new box to `~/.ssh/config`. It currently has a single
  `raspi` → `thegoldenunasinn.local`, which stops being an unambiguous name for
  "the Pi" the moment there are two of them. Give the new one the literal name
  `newpi`, since every remote command below assumes it.
- `ssh-copy-id newpi` so the rest of the guide's `scp`/`rsync` calls don't prompt.

```bash
ssh newpi hostname     # prints the new name → alias and key are both good
```

## 3. Restore

First get the dump onto the new box — it's currently only on the old Pi and in
`~/backups`:

**[ WORKSTATION ]**

```bash
scp ~/backups/wojbot-full-2026-07-29.sql ~/backups/wojbot_db-2026-07-29.dump newpi:
```

> **Do not run the `leagueSQL` scripts first.** `pg_dumpall`'s globals section
> emits `CREATE ROLE` for every role in the cluster, so a role you created by
> hand collides with the dump's — and `psql -f` doesn't stop on error by
> default, which leaves you a half-applied globals section that looks like it
> worked. Restore into the empty cluster from step 2 and let the dump do it.
> `sql/databases/wojbot_db/01_`–`05_` in that repo are the *fallback* for a dump
> that turns out to be unusable, not a prerequisite.

**[ NEW PI ]**

```bash
sudo -u postgres psql -v ON_ERROR_STOP=1 -f wojbot-full-2026-07-29.sql 2>&1 \
  | tee ~/restore.log
```

`ON_ERROR_STOP=1` is what turns a silent partial restore into an obvious one.
Against a genuinely fresh cluster this should run clean — `pg_dumpall` emits
`ALTER ROLE` rather than `CREATE ROLE` for the bootstrap `postgres` superuser, so
there's nothing legitimate for it to trip over.

### Re-set the login passwords if they were md5

If the check at the end of step 1 printed `md5…`, do this now. The hash copies
across verbatim, so `pylot` restores with every grant intact and then fails
authentication with `password authentication failed` against a fresh 15/17
`pg_hba.conf`, which only offers `scram-sha-256`. It is the most confusing
failure in the whole move, because nothing about the role *looks* wrong.

**[ NEW PI ]** — `sudo -u postgres psql`

```sql
\password pylot
```

Enter the **same password** the bot already uses; only the stored verifier
changes, so `.env` needs no edit. Repeat for any other login role you care about
(`piders994` if you connect DataGrip as it). Then confirm:

```sql
SELECT rolname, left(rolpassword, 6) FROM pg_authid WHERE rolcanlogin;
```

Every row should now read `SCRAM-`.

### Verify before trusting it

**[ NEW PI ]** — `sudo -u postgres psql wojbot_db`

- Re-run the count query from step 1 and compare every number.
- Check the roles all arrived, with their memberships:
  ```sql
  \du
  ```
  Expect `pylot`, `piders994`, `moderator`, `power_user`, `manager`, `consumer`.
- Check ownership and grants survived:
  ```sql
  SELECT tablename, tableowner FROM pg_tables WHERE schemaname='fantasy_sports';
  SELECT grantee, privilege_type FROM information_schema.table_privileges
   WHERE table_schema='fantasy_sports' AND table_name='dim_league';
  ```
  Expect owner `piders994`, and `moderator` holding SELECT/INSERT/UPDATE/DELETE.
- Confirm `pylot` can actually log in and read — this is the step that catches
  the md5 problem above:
  ```bash
  psql "postgresql://pylot:PASSWORD@127.0.0.1:5432/wojbot_db" -c \
    "SELECT count(*) FROM fantasy_sports.dim_source_type"
  ```

If anything is missing, the `leagueSQL` repo rebuilds the schema from scratch —
`sql/databases/wojbot_db/01_`–`05_`, then `sql/ddl/schema.sql`, then
`sql/ddl/rumors.sql`, then the three `sql/dml/insert_*.sql` seeds, all as
`piders994`. That gets you an empty but correct warehouse; the dump is still the
only source for the actual data.

> **`pg_hba.conf` is not in the dump.** A fresh install allows loopback with
> `scram-sha-256` and nothing else, which is all the bot needs now that it lives
> on the same box. If you also want DataGrip reaching it from the workstation,
> add a `host all all 192.168.1.0/24 scram-sha-256` line to
> `/etc/postgresql/<version>/main/pg_hba.conf` yourself, then
> `sudo systemctl reload postgresql`. (You'd also need to relax
> `listen_addresses` in step 6, which is a real trade — decide once.)

## 4. Install the bot

Cloning over ssh needs a key on the Pi that GitHub knows about — either generate
one (`ssh-keygen -t ed25519`) and add it as a deploy key on `riders994/WojBot`,
or clone the HTTPS URL if you'd rather not.

**[ NEW PI ]**

```bash
git clone git@github.com:riders994/WojBot.git && cd WojBot
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install -e . --no-deps
```

This has to land at `/home/weezy/WojBot` for the unit file in step 7 and the
rsync in step 5 to match — which it does if the account is `weezy` and you
cloned from the home directory.

**Install from `requirements.lock`, not from `pyproject.toml`.** The constraints
in `pyproject.toml` are ranges, so a plain `pip install -e .` resolves them
against whatever is newest the day you run it — and the whole point of the
exercise is that the Pi runs what you've been testing. The lock pins all 24
runtime packages, including the transitive ones nothing in `pyproject.toml`
constrains at all (`numpy`, `pandas`, `aiohttp`, `yarl`). `--no-deps` on the
second line is what stops pip resolving the ranges again on top.

The lock records versions, not architectures, and every one of these publishes
an aarch64 wheel, so a lock generated on the workstation installs fine on the
Pi. Everything comes from public PyPI — `rv-pytools`, `elo-system` and `sleeper`
are all published, so there is no private index to set up.

**Do not copy `.venv/`** from the workstation; it is x86_64.

To regenerate the lock after changing a dependency:

**[ WORKSTATION ]** — then commit the result

```bash
uv pip compile pyproject.toml -o requirements.lock
```

## 5. Copy the state the repo doesn't carry

These are gitignored, so a clone won't have them:

```
.env
sql_config.yml
resources/configs/sql_config.yml
resources/anon/anon_manager.json
resources/anon/anon_manager_platform.json
resources/ratings/                      (per-league Elo CSVs)
```

**[ WORKSTATION ]** — from `/home/weezy/activity/WojBot`, since `--relative`
resolves the sources against the working directory

```bash
cd /home/weezy/activity/WojBot
rsync -av --relative \
  .env sql_config.yml \
  resources/configs/sql_config.yml \
  resources/anon/ resources/ratings/ \
  newpi:WojBot/
```

**`resources/anon/` is not optional.** Those are the anonymizer's reversal maps.
Without them rumors print `manager_18` instead of a name, and — worse — the next
`/commish sync` finds no map, starts numbering from zero, and mints `manager_0`
for a *different* person than the `manager_0` already in the warehouse. That
silently repoints names on rows that are already there.

`resources/ratings/` matters because `sys_config.yml` sets `reader: csv` /
`writer: csv` — the Elo history lives in those CSVs, not in Postgres.

**Do not copy** `sql/manager.log.json` or `sql/queries/_queries.json`. They
regenerate, and a stale log makes the bot skip registering the query files.

Note that `.env` and both `sql_config.yml` files arrive on the new Pi still
naming `thegoldenunasinn`. Step 6 is where that gets fixed, and it has to happen
**before the first start**, not after — see the warning at the top of this guide.

If `resources/anon/discord_ids.json` exists by then, it is the **most important
file in the transfer** — it is the only place the mapping from real Discord IDs
to the surrogates stored in `dim_league.discord_server_id` and
`dim_manager.discord_id` lives. Lose it and every `/commish db link` and
`linkuser` is gone, and `/rumor` stops working until you redo them all.

## 6. Point it at localhost

Everything in this step is **[ NEW PI ]**, in `~/WojBot`.

**Three files name the database host, not one.** All of them came across in step
5 pointing at `thegoldenunasinn`, and with the old board still serving Postgres
every one of them is a live route back to the stale warehouse:

| file | read by | what to do |
|---|---|---|
| `.env` | the bot's `SqlService` | switch to the `SQL_*` fields, below |
| `resources/configs/sql_config.yml` | `EloSystem` → `EloSQL` | set `conn_uri` to `127.0.0.1` |
| `sql_config.yml` (repo root) | nothing on this path | correct it or delete it |

The middle one is the one that gets missed. `resources/configs/sys_config.yml`
sets `sql_config_name: sql_config.yml`, and `EloSystem` resolves that against its
`configs_dir` — which is `resources/configs/`, not the repo root. `EloSQL`
validates and stores that URI even when the bot hands it a live connection to
reuse (`wojbot/core/elo.py`), and the bot only hands one over when it *has* one.
So the host in that file can genuinely be dialled.

The root `sql_config.yml` is on nobody's read path, but it holds the same string,
and a second copy of a wrong hostname is a trap for whoever reads this next.

Now `.env`: comment out `SQL_CONN_URI` and use the fields, which is what the
commented block at the bottom of the file is for.

```
# SQL_CONN_URI=...
SQL_HOST=127.0.0.1
SQL_PORT=5432
SQL_USER=pylot
SQL_PASSWORD=<the password>
SQL_DBNAME=wojbot_db
```

`SQL_CONN_URI` wins while it is set, so it genuinely has to be commented out.
The fields escape the password for you.

Use `127.0.0.1` rather than a Unix socket: the socket path is likely governed by
a `local ... peer` rule that would demand an OS user named `pylot`.

Then lock the database down — it no longer needs to be on the network at all:

**[ NEW PI ]** — `/etc/postgresql/<version>/main/postgresql.conf`

```
listen_addresses = 'localhost'
```

```bash
sudo systemctl restart postgresql     # listen_addresses needs a restart, not a reload
```

Skip this one if you added the LAN line to `pg_hba.conf` in step 3 for DataGrip;
the two settings pull in opposite directions and `listen_addresses` wins.

Check the bot resolves the URI it should, without printing the password:

**[ NEW PI ]** — from `~/WojBot`

```bash
.venv/bin/python -c "
from wojbot.core.config import Settings
import re; s=Settings.load()
print(re.sub(r'://([^:@/]*)(:[^@]*)?@', r'://\1:***@', s.sql_conn_uri or ''))"
```

Then make sure nothing else still points at the old box:

**[ NEW PI ]**

```bash
grep -rn thegoldenunasinn ~/WojBot --exclude-dir=.git --exclude-dir=.venv
```

Anything this prints outside `docs/` is a route back to the stale warehouse.
Expect no hits at all once the three files above are done.

## 7. Run it as a service

**[ NEW PI ]** — `/etc/systemd/system/wojbot.service`

```ini
[Unit]
Description=WojBot
After=network-online.target postgresql.service
Wants=network-online.target
Requires=postgresql.service

[Service]
Type=simple
User=weezy
WorkingDirectory=/home/weezy/WojBot
ExecStart=/home/weezy/WojBot/.venv/bin/wojbot
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`User=` and both paths assume the OS account is `weezy`; fix all three together
if it isn't.

**[ NEW PI ]**

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now wojbot
journalctl -u wojbot -f
```

**`WorkingDirectory` is load-bearing**, and its failure mode is misleading.
`load_dotenv()` finds `.env` relative to the current directory, so a unit
without it starts in `/` and dies with:

```
ConfigError: DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in
```

— with a perfectly good `.env` sitting in the repo. (The config and anon files
under `resources/` are fine either way: `PROJECT_ROOT` is derived from the
installed package location, not the working directory.)

**Mind the clock.** Postgres is now the machine stamping `reported_at` on every
rumor, so if the bot writes before NTP settles those timestamps are wrong. The
Pi 5 does have a real-time clock, but the battery for it is a separate purchase
and it keeps no time without one — so treat the clock as NTP-dependent unless
you've fitted the battery to the RTC connector. `After=network-online.target`
doesn't cover this; add `After=time-sync.target` and `sudo systemctl enable
systemd-time-wait-sync` if you see it happen.

At this point both bots would run against different warehouses, so don't leave
it here long — go straight to step 8. (If you need to pause, stop the new one:
`sudo systemctl stop wojbot`.)

## 8. Cut over

1. **[ WORKSTATION ]** Stop the bot.
2. **[ NEW PI ]** Start it, and watch for a clean login:
   ```bash
   sudo systemctl start wojbot && journalctl -u wojbot -f
   ```
3. **[ DISCORD ]** `/ping`, then `/sql status` — it should report the connection
   up and 10 registered queries.
4. **[ DISCORD ]** `/setup show` in each league server.
5. **[ DISCORD ]** Report one throwaway rumor and read it back with
   `/rumor recent`, then confirm the row landed on the **new** box and not the
   old one:

   **[ WORKSTATION ]**
   ```bash
   ssh newpi            "sudo -u postgres psql -Atc \
     'SELECT count(*) FROM fantasy_sports.fact_rumor' wojbot_db"
   ssh thegoldenunasinn "sudo -u postgres psql -Atc \
     'SELECT count(*) FROM fantasy_sports.fact_rumor' wojbot_db"
   ```
   The new count went up and the old one didn't. If it's the other way round,
   something is still reading a config from step 6.
6. **Take the old warehouse out of service**, once the counts above satisfy you.
   With the board staying on for other projects, this is what "powered off but
   unwiped" used to do for free — it makes a config you missed fail loudly
   instead of silently succeeding.

   Keep the data, drop the route to it. Cheapest first:

   **[ OLD PI ]** — if nothing else on that board needs Postgres
   ```bash
   sudo systemctl disable --now postgresql
   ```

   If other projects on that board *do* need Postgres, leave the server up and
   revoke the bot's way in instead:

   **[ OLD PI ]** — `sudo -u postgres psql`
   ```sql
   ALTER ROLE pylot NOLOGIN;
   ALTER DATABASE wojbot_db RENAME TO wojbot_db_retired_2026_08;
   ```

   Either way the dump from step 1 is still your rollback, and the database is
   still on disk. Keep the dumps for a couple of weeks regardless.

### Rolling back

Nothing before step 8 is destructive, so the rollback is short: stop the unit on
the new Pi, undo whichever half of step 8.6 you applied, and start the bot on the
workstation again. Its config still names `thegoldenunasinn` — the very thing
that makes a missed config dangerous during the move is what makes the rollback
trivial. Rumors reported through the new Pi in between live only in the new
warehouse; if that matters, dump `fact_rumor` from it before you turn it off.

---

## About the flapping

What we actually saw from the workstation:

```
connection to server at "thegoldenunasinn" (192.168.1.165), port 5432 failed: No route to host
connection to server at "thegoldenunasinn" (fe80::e21e:a032:d05b:8d15), port 5432 failed: Invalid argument
```

Two separate problems, and the move fixes one of them outright.

**The IPv6 half is a resolver bug.** The FiOS gateway hands back a AAAA record
containing a *link-local* address (`fe80::/10`). Those can't be connected to
without a scope id, so every attempt fails with `Invalid argument` before the
IPv4 address is even tried. It's pure latency and noise. Fix it for any client
that still connects over the network with a hosts entry that pins IPv4:

**[ WORKSTATION ]** — `/etc/hosts`

```
192.168.1.165  thegoldenunasinn
<new-pi-ip>    newpi
```

**This isn't currently applied** — the workstation's `/etc/hosts` has no entry
for either box, so both are resolving over mDNS (`~/.ssh/config` points `raspi`
at `thegoldenunasinn.local`). Worth adding for both now that two Pis are staying
on the network.

**The `No route to host` half is a real network drop**, and the usual suspects
on a Pi are, roughly in order of likelihood. All of these are checked **on
whichever Pi is dropping** — historically the old one, but the wifi and power
items apply just as much to the new board:

- **Wi-Fi power management.** The single most common cause. `iw wlan0 get
  power_save`; turn it off with `sudo iw wlan0 set power_save off`, made
  permanent in `/etc/rc.local` or a NetworkManager profile. **Use Ethernet if
  the new Pi can reach a port** — it makes the whole class of problem go away.
- **Undervoltage**, which browns out the USB/network stack under load.
  `vcgencmd get_throttled` — anything other than `0x0` means the supply isn't
  keeping up. A Pi 5 wants the official 27W USB-C PD supply, and genuinely needs
  it once an NVMe drive is drawing from the same rail; a phone charger or an
  older 15W Pi 4 supply is a common source of exactly this. The Pi 5 also runs
  hot enough to want the active cooler.
- **SD card stalls.** Postgres on an SD card blocks for seconds at a time as the
  card does wear-levelling, which looks like the host disappearing. This is the
  argument for booting from SSD.
- **DHCP lease renewal** moving the address mid-session. The reservation in
  step 2 covers it.

**But note that once the bot is on the Pi, none of this affects it.** It'll be
talking to `127.0.0.1`, and loopback doesn't flap — no DNS, no Wi-Fi, no
gateway. Anything still connecting over the network (DataGrip from the
workstation, say) keeps the old exposure, which is the reason to bother with the
hosts entry and the power-save setting even after the move — and with the old
board staying in service for other work, that reason doesn't expire.

So: a fresh 64-bit install on good storage plausibly fixes the flapping, and
colocating the bot makes it irrelevant to the bot either way. Don't count on the
reinstall alone if other machines still need to reach the database.
