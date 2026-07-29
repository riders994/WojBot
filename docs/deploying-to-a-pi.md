# Moving to a new Pi

Notes for putting the bot and the warehouse on one fresh Raspberry Pi. Written
against the current setup: bot on the x86_64 workstation, Postgres 13 on
`thegoldenunasinn` (192.168.1.165), a 32-bit Raspberry Pi OS bullseye box.

The headline is that a new Pi lets you run both machines at once. Dump, restore,
verify, *then* switch. Nothing here has to be done under pressure, and the old Pi
stays a working rollback until you deliberately retire it.

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

---

## 1. On the OLD Pi — dump everything

Roles matter as much as data here: `pylot` (the bot), `moderator`, `consumer`,
`power_user` and the ownership by `piders994` all have to come across, or the
bot's grants silently differ. `pg_dumpall` captures roles; `pg_dump` alone
does not.

```bash
sudo -u postgres pg_dumpall > wojbot-full-$(date +%F).sql
sudo -u postgres pg_dump -Fc wojbot_db > wojbot_db-$(date +%F).dump   # belt and braces
```

Copy both **off the Pi** before you touch anything:

```bash
scp thegoldenunasinn:wojbot-*.sql thegoldenunasinn:wojbot_db-*.dump ~/backups/
```

Record what you expect to see on the other side:

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

## 2. On the NEW Pi — base system

Give it a **different hostname** while both are alive; rename it to
`thegoldenunasinn` only once the old one is retired, or you'll have two machines
answering to the same name.

```bash
sudo apt update && sudo apt install postgresql git python3-venv
uname -m        # must print aarch64
python3 -V      # must be >= 3.11
```

Set a **DHCP reservation** on the FiOS gateway so the address never moves.

## 3. Restore

```bash
sudo -u postgres psql -f wojbot-full-2026-07-29.sql
```

This recreates the roles and the database. Then verify **before** trusting it:

- Re-run the count query from step 1 and compare every number.
- Check ownership and grants survived:
  ```sql
  SELECT tablename, tableowner FROM pg_tables WHERE schemaname='fantasy_sports';
  SELECT grantee, privilege_type FROM information_schema.table_privileges
   WHERE table_schema='fantasy_sports' AND table_name='dim_league';
  ```
  Expect owner `piders994`, and `moderator` holding SELECT/INSERT/UPDATE/DELETE.
- Confirm `pylot` can actually log in and read:
  ```bash
  psql "postgresql://pylot:PASSWORD@127.0.0.1:5432/wojbot_db" -c \
    "SELECT count(*) FROM fantasy_sports.dim_source_type"
  ```

If anything is missing, the `leagueSQL` repo rebuilds the schema from scratch —
`sql/ddl/schema.sql`, then `sql/ddl/rumors.sql`, then the three
`sql/dml/insert_*.sql` seeds, all as `piders994`. That gets you an empty but
correct warehouse; the dump is still the only source for the actual data.

> **`pg_hba.conf` is not in the dump.** A fresh install allows loopback with
> `scram-sha-256` and nothing else, which is all the bot needs now that it lives
> on the same box. If you also want DataGrip reaching it from the workstation,
> re-add a `host ... 192.168.1.0/24 scram-sha-256` line yourself.

## 4. Install the bot

```bash
git clone git@github.com:riders994/WojBot.git && cd WojBot
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install -e . --no-deps
```

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

To regenerate the lock after changing a dependency (on the workstation, then
commit it):

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

```bash
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

If `resources/anon/discord_ids.json` exists by then, it is the **most important
file in the transfer** — it is the only place the mapping from real Discord IDs
to the surrogates stored in `dim_league.discord_server_id` and
`dim_manager.discord_id` lives. Lose it and every `/commish db link` and
`linkuser` is gone, and `/rumor` stops working until you redo them all.

## 6. Point it at localhost

Edit `.env`: comment out `SQL_CONN_URI` and use the fields, which is what the
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

```
# postgresql.conf
listen_addresses = 'localhost'
```

Check it works, without printing the password:

```bash
.venv/bin/python -c "
from wojbot.core.config import Settings
import re; s=Settings.load()
print(re.sub(r'://([^:@/]*)(:[^@]*)?@', r'://\1:***@', s.sql_conn_uri or ''))"
```

## 7. Run it as a service

`/etc/systemd/system/wojbot.service`:

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

## 8. Cut over

1. Stop the bot on the workstation.
2. Start it on the Pi, watch `journalctl -u wojbot -f` for a clean login.
3. `/ping`, then `/sql status` — it should report the connection up and 10
   registered queries.
4. `/setup show` in each league server.
5. Leave the old Pi powered off but unwiped for a couple of weeks.

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

```
# /etc/hosts on the workstation
192.168.1.165  thegoldenunasinn
```

**The `No route to host` half is a real network drop**, and the usual suspects
on a Pi are, roughly in order of likelihood:

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
hosts entry and the power-save setting even after the move.

So: a fresh 64-bit install on good storage plausibly fixes the flapping, and
colocating the bot makes it irrelevant to the bot either way. Don't count on the
reinstall alone if other machines still need to reach the database.
