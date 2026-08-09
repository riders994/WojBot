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
machines answer to one name. That name is `wojingtonpost`, and it is written out
throughout rather than left as a placeholder, so every command below can be run
as-is.

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
| **[ OLD PI ]** | `thegoldenunasinn` | 192.168.1.165 — Postgres 13.23, bullseye, 32-bit. Stays in service. | `ssh oldpi` (also aliased `raspi`; **not** port 22) |
| **[ NEW PI ]** | `wojingtonpost` | the fresh 64-bit board — ends up running *both* Postgres and the bot | `ssh wojingtonpost`, once step 2 sets it up |

When in doubt, `hostname` settles it. The dangerous confusions are all
old-Pi-versus-new-Pi, and both answer to `sudo -u postgres psql` with a
plausible-looking `wojbot_db`.

Anything below tagged **[ WORKSTATION ]** that mentions a path is relative to
`/home/weezy/activity/WojBot` unless it says otherwise. The new Pi's checkout
sits at the matching `~/activity/WojBot`; the old Pi has no checkout at all,
only the database.

## The move at a glance

| step | runs on | what happens |
|---|---|---|
| 1 | OLD PI → WORKSTATION | dump roles + data, record counts, check password hashes, copy the dumps off |
| 2 | NEW PI + WORKSTATION | image, hostname, OS account, packages, DHCP reservation, ssh alias |
| 3 | WORKSTATION → NEW PI | ship the dump over, restore it, re-set passwords, verify |
| 4 | NEW PI | clone the repo, build the venv from the lock |
| 5 | WORKSTATION | rsync the gitignored state the clone doesn't carry |
| 6 | NEW PI | repoint both config files at localhost |
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

**OS users — you need exactly one, `piders994`.** That's the primary account on
both boards, so it's what Raspberry Pi Imager's "Set username and password"
panel should say. The systemd unit in step 7 runs as `User=piders994` out of
`/home/piders994/activity/WojBot`, and the rsync in step 5 targets
`wojingtonpost:activity/WojBot/`, which is the same directory written relative to
that account's home. Use any other name and you own the job of editing both.

Note this is *not* the workstation's account: the repo lives at
`/home/weezy/activity/WojBot` there, and `weezy` never exists on a Pi. A
`/home/…/WojBot` path is only unambiguous once you know which machine printed
it.

`postgres` arrives with the package. **Nothing needs an OS account named `pylot`
or `moderator`** — those are Postgres roles, not Unix users, which is exactly
why step 6 connects over `127.0.0.1` instead of the Unix socket, since a
`local … peer` rule *would* demand one. `piders994` being both an OS account and
a Postgres superuser role here is a coincidence of naming, not a requirement.

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

Postgres 13 defaults `password_encryption` to **md5** — the default only flipped
to `scram-sha-256` in 14 — so `md5…` is the answer to plan for. If you get it,
see the warning in step 3: those hashes restore fine and then fail to
authenticate against a fresh 15/17 `pg_hba.conf`.

Don't assume it, though. As of the 2026-08-01 dump this cluster answers `SCRA`
for all eight login roles, `password_encryption` having evidently been switched
at some point, with `postgres` itself blank because it authenticates by peer and
has no password. That combination needs no fixup at all — fresh 15/17 installs
default to `scram-sha-256` for host connections, so the hashes land and work.
Re-run the same query on the **new** Pi once the roles are restored if you want
it confirmed there rather than inferred.

Now copy both dumps **off the Pi** before you touch anything:

**[ WORKSTATION ]**

```bash
mkdir -p ~/backups
scp oldpi:'wojbot-*.sql' oldpi:'wojbot_db-*.dump' ~/backups/
```

## 2. Base system on the new Pi

Give it its **own hostname** — `wojingtonpost` here — and leave it that way.
`thegoldenunasinn` belongs to the old board and stays with it. Set the primary
user to `piders994` while you're in the imager, per the accounts section above.

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
  address never moves. The new one sits at `192.168.50.60`, the old one keeps
  `192.168.1.165` and now needs a reservation of its own if it never had one —
  two Pis competing for leases is a new problem. Note they're on **different
  subnets**, which matters for the cross-box checks in step 8.
- Add an ssh alias for the new box to `~/.ssh/config`. Prefer the machine's real
  hostname over a role name: `raspi` was fine while there was one Pi, and became
  ambiguous the moment there were two. Worse, **both boards use the same
  `piders994` account**, so a path like `/home/piders994/activity/WojBot` in an error
  message doesn't tell you which machine you're on — the hostname is the only
  thing that does.

  ```
  # Old Pi — Postgres 13, bullseye, 32-bit. Rollback target until retired.
  Host raspi oldpi
      HostName thegoldenunasinn.local
      Port 50314
      User piders994

  # New Pi — the migration target.
  Host wojingtonpost newpi
      HostName 192.168.50.60
      User piders994
  ```
- `ssh-copy-id wojingtonpost` so the rest of the guide's `scp`/`rsync` calls
  don't prompt.

```bash
ssh wojingtonpost hostname     # prints wojingtonpost → alias and key are both good
```

That last check is worth actually running. `wojingtonpost` resolves through the
alias above and **not** through DNS or mDNS, so an `rsync` written against a
name your ssh config doesn't define fails with `Could not resolve hostname`
rather than doing anything useful.

## 3. Restore

First get the dump onto the new box — it's currently only on the old Pi and in
`~/backups`:

**[ WORKSTATION ]**

```bash
scp ~/backups/wojbot-full-2026-07-29.sql ~/backups/wojbot_db-2026-07-29.dump wojingtonpost:
```

> **Do not run the `leagueSQL` scripts first.** `pg_dumpall`'s globals section
> emits `CREATE ROLE` for every role in the cluster, so a role you created by
> hand collides with the dump's — and `psql -f` doesn't stop on error by
> default, which leaves you a half-applied globals section that looks like it
> worked. Restore into the empty cluster from step 2 and let the dump do it.
> `sql/databases/wojbot_db/01_`–`05_` in that repo are the *fallback* for a dump
> that turns out to be unusable, not a prerequisite.

### First: the databases want the old box's locale

Both `CREATE DATABASE` lines in the dump carry `LOCALE = 'en_GB.UTF-8'`,
inherited from the old Raspbian install. If the imager set the new Pi to `en_US`
or left it at `C.UTF-8`, the restore gets all the way through the roles and then
fails with `invalid locale name`. Check first — it's read-only, and it decides
whether the cluster you restore into is the right one:

**[ NEW PI ]**

```bash
locale -a | grep -i en_GB
```

If it prints nothing, generate it:

```bash
sudo sed -i 's/^# *en_GB.UTF-8 UTF-8/en_GB.UTF-8 UTF-8/' /etc/locale.gen
sudo locale-gen
```

Get this out of the way before you restore, and before any `pg_createcluster`
below — that command fails outright on a locale the system hasn't generated,
and doing it in this order also leaves the cluster's own template databases
matching the ones being restored into it.

### Then the restore itself

**[ NEW PI ]**

```bash
sed -e '/^CREATE ROLE postgres;$/d' \
    -e 's/^\(GRANT .*\) GRANTED BY [^;]*;$/\1;/' \
    wojbot-full-2026-07-29.sql \
  | sudo -u postgres psql -v ON_ERROR_STOP=1 -f - 2>&1 \
  | tee ~/restore.log
```

Three things about that pipeline, each of which cost a restore to learn.

First, feed the dump in on **stdin**, not with `-f wojbot-full-….sql`. `psql`
runs as the `postgres` user here, and bookworm and trixie both create home
directories `0750`, so `postgres` can't traverse into yours to read a file you
just `scp`'d there — you get `psql: error: wojbot-full-….sql: Permission denied`
before a single statement runs. The `sed` reading the file (or a plain `< …`
redirect, if you drop the filters) runs as *you*, before `sudo` lowers
privileges, so permissions never enter into it. Don't `chmod o+x`
your home directory to work around it; if you'd rather have a real path (handy
if you expect to re-run the restore), `sudo mv` both files to `/var/tmp` and
`sudo chown postgres:` them instead.

Second, **filter out `CREATE ROLE postgres;`**. `pg_dumpall` 13 emits it — it does
*not* special-case the bootstrap superuser — and it is the one statement in the
file that cannot succeed, because every freshly-initdb'd cluster already has
that role. The `ALTER ROLE postgres WITH …` on the next line is what actually
carries the attributes, and they're the stock bootstrap set, so deleting the
`CREATE` loses nothing. Filtering the single known-impossible statement is much
better than dropping `ON_ERROR_STOP` to get past it.

Third, **strip the `GRANTED BY` clauses off the role memberships**. This is a
genuine PG13→16+ incompatibility rather than a quirk. The dump's globals section
ends with fourteen lines like:

```sql
GRANT consumer TO jordanpokesaserver GRANTED BY piders994;
```

PostgreSQL 16 tightened `GRANT`: the role named as grantor must hold ADMIN
OPTION on the role being granted, and the only role exempt from the check is the
bootstrap superuser (`postgres`, OID 10). On the old 13 cluster `piders994`
could grant these purely by being a superuser, so nothing ever recorded an admin
option — and `pg_dumpall` 13 has no `WITH ADMIN OPTION` to emit. Replay them
unmodified on trixie's 17 and every `GRANTED BY piders994` line fails with:

```
ERROR:  permission denied to grant privileges as role "piders994"
DETAIL:  The grantor must have the ADMIN option on role "consumer".
```

The `GRANTED BY postgres` lines in the same block *do* succeed, which makes the
failure look arbitrary until you know the OID 10 exemption. Dropping the clause
lets the grants run as the connected superuser. The memberships that result are
identical; all that changes is the grantor recorded in `pg_auth_members`, which
after a migration is more accurate as `postgres` anyway.

Bookworm's 15 predates the tightening and would replay these lines as-is, but
the `sed` is a no-op there in every way that matters, so run it on either image
rather than tracking which rule applies.

`ON_ERROR_STOP=1` is what turns a silent partial restore into an obvious one.
Don't reach for `--single-transaction` to get the same protection: the dump uses
`\connect` to switch between databases, which can't happen inside a transaction
block.

> **If a restore does abort partway, reset the cluster before retrying.** psql
> auto-commits each statement, so an abort in the globals section leaves behind
> every role and membership it had already created, and the retry dies on the
> first of them instead. There's nothing worth salvaging at that point:
>
> ```bash
> pg_lsclusters                       # note the version in column 1
> VER=17                              # ← whatever that printed; 15 on bookworm
> sudo pg_dropcluster --stop $VER main
> sudo pg_createcluster --locale en_GB.UTF-8 --start $VER main
> ```

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
mkdir -p ~/activity && cd ~/activity
git clone git@github.com:riders994/WojBot.git && cd WojBot
pwd                     # must print /home/piders994/activity/WojBot
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install -e . --no-deps
```

The `activity/` level is deliberate — it mirrors the workstation, where the repo
lives at `/home/weezy/activity/WojBot`, so the same relative path means the same
thing on either machine.

That `pwd` matters, because three later things are pinned to this exact
directory: step 5's rsync target, step 7's `WorkingDirectory` and its
`ExecStart`. Clone one level up by mistake and the rsync quietly builds a
*second*, resource-only `WojBot` next to the real checkout — no error, no
warning, and the bot then starts against whichever of the two the unit file
happens to name.

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
  .env \
  resources/configs/sql_config.yml \
  resources/anon/ resources/ratings/ \
  wojingtonpost:activity/WojBot/
```

**The repo-root `sql_config.yml` is deliberately not in that list.** Nothing
reads it: `resources/configs/sys_config.yml` sets `sql_config_name`, and
`EloSystem` resolves that against its `configs_dir`, which is
`resources/configs/` — never the repo root. The root copy is a leftover from an
older layout that still holds a working `pylot` password aimed at
`thegoldenunasinn`, so copying it would put a live credential and a stale
hostname on the new box for no reason at all. Leave it behind, and consider
deleting it on the workstation too.

**`resources/anon/` is not optional.** Those are the anonymizer's reversal maps.
Without them rumors print `manager_18` instead of a name, and — worse — the next
`/commish sync` finds no map, starts numbering from zero, and mints `manager_0`
for a *different* person than the `manager_0` already in the warehouse. That
silently repoints names on rows that are already there.

`resources/ratings/` matters because `sys_config.yml` sets `reader: csv` /
`writer: csv` — the Elo history lives in those CSVs, not in Postgres.

**Do not copy** `sql/manager.log.json` or `sql/queries/_queries.json`. They
regenerate, and a stale log makes the bot skip registering the query files.

Note that `.env` and `resources/configs/sql_config.yml` arrive on the new Pi
still naming `thegoldenunasinn`. Step 6 is where that gets fixed, and it has to
happen **before the first start**, not after — see the warning at the top of this
guide.

If `resources/anon/discord_ids.json` exists by then, it is the **most important
file in the transfer** — it is the only place the mapping from real Discord IDs
to the surrogates stored in `dim_league.discord_server_id` and
`dim_manager.discord_id` lives. Lose it and every `/commish db link` and
`linkuser` is gone, and `/rumor` stops working until you redo them all.

## 6. Point it at localhost

Everything in this step is **[ NEW PI ]**, in `~/activity/WojBot`.

**Two files name the database host, not one.** Both came across in step 5
pointing at `thegoldenunasinn`, and with the old board still serving Postgres
each is a live route back to the stale warehouse:

| file | read by | what to do |
|---|---|---|
| `.env` | the bot's `SqlService` | switch to the `SQL_*` fields, below |
| `resources/configs/sql_config.yml` | `EloSystem` → `EloSQL` | set `conn_uri` to `127.0.0.1` |

The second is the one that gets missed. `resources/configs/sys_config.yml` sets
`sql_config_name: sql_config.yml`, and `EloSystem` resolves that against its
`configs_dir` — which is `resources/configs/`, not the repo root. `EloSQL`
validates and stores that URI even when the bot hands it a live connection to
reuse (`wojbot/core/elo.py`), and the bot only hands one over when it *has* one.
So the host in that file can genuinely be dialled.

The workstation also has a repo-root `sql_config.yml` naming the same host. It
is on nobody's read path and step 5 deliberately doesn't copy it, so there is
nothing to fix here — but the `grep` below will still flag it on the
*workstation* if you ever run it there.

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

**Also comment out `DISCORD_GUILD_ID` while you're in there.** It is a
development convenience and the Pi is production. The bot syncs its commands
globally and then, if that variable is set, *copies* them into that one guild
and syncs again — and a guild copy of a global command is a second registration,
not a replacement (`wojbot/bot.py`). The result is every command listed twice in
the picker in that server. The workstation's `.env` has it set, step 5 copies
`.env` verbatim, so the behaviour follows the bot to the Pi.

Unsetting it stops *new* copies; the ones already registered live on Discord's
side until cleared explicitly. Clear them first, while the id is still readable
in `.env`:

**[ NEW PI ]** — from `~/activity/WojBot`

```bash
.venv/bin/python - <<'EOF'
import os, discord
from discord import app_commands
from dotenv import load_dotenv
load_dotenv(".env")      # explicit: a stdin script has no frame to search from
GUILD = int(os.environ["DISCORD_GUILD_ID"])
class C(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.none())
        self.tree = app_commands.CommandTree(self)
    async def setup_hook(self):
        g = discord.Object(id=GUILD)
        self.tree.clear_commands(guild=g)
        await self.tree.sync(guild=g)
        print("cleared guild-scoped commands")
        await self.close()
C().run(os.environ["DISCORD_TOKEN"])
EOF
```

Then comment the variable out. Global commands are untouched, so nothing
disappears from other servers; reload the Discord client if the picker still
shows the old list.

The workstation bot is still running at this point, and that's fine here: this
client declares no intents, registers no commands and closes itself inside
`setup_hook`, so it never handles an event. It is not a second bot in any sense
that matters — unlike leaving the step 7 unit running alongside it, which is why
step 7 doesn't start anything.

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

**[ NEW PI ]** — from `~/activity/WojBot`

```bash
.venv/bin/python -c "
from wojbot.core.config import Settings
import re; s=Settings.load()
print(re.sub(r'://([^:@/]*)(:[^@]*)?@', r'://\1:***@', s.sql_conn_uri or ''))"
```

Then make sure nothing else still points at the old box:

**[ NEW PI ]**

```bash
grep -rn thegoldenunasinn ~/activity/WojBot --exclude-dir=.git --exclude-dir=.venv
```

Anything this prints outside `docs/` is a route back to the stale warehouse.
Expect no hits at all once the two files above are done.

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
User=piders994
WorkingDirectory=/home/piders994/activity/WojBot
ExecStart=/home/piders994/activity/WojBot/.venv/bin/wojbot
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`User=` and both paths assume the OS account is `piders994`; fix all three
together if it isn't. Note the unit runs on the Pi, so `weezy` — the
workstation's account — is never the right answer here.

**[ NEW PI ]**

```bash
sudo systemctl daemon-reload && sudo systemctl enable wojbot
systemctl cat wojbot        # confirm systemd sees what you just wrote
```

**`enable`, not `enable --now`.** This registers the unit for boot without
starting it. The bot on the workstation is still running and still holds the
Discord token, and two instances on one token against two different warehouses
is precisely the divergence this guide exists to avoid. Step 8 performs the only
start, in the right order.

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

Nothing is running on the new Pi yet, so you can stop here for as long as you
like. Steps 1–7 leave the workstation bot untouched and in charge; step 8 is the
first and only moment anything changes.

## 8. Cut over

1. **[ WORKSTATION ]** Stop the bot, and confirm it's actually down before
   moving on. Both instances share one Discord token, so overlapping them gets
   you duplicated command responses and two processes writing to two different
   warehouses.
2. **[ NEW PI ]** Start it, and watch for a clean login:
   ```bash
   sudo systemctl start wojbot && journalctl -u wojbot -f
   ```
   This is the **first** time the unit has ever run, so this is where a bad
   `WorkingDirectory` or an unreadable `.env` shows up — see step 7 for what
   that looks like. If it won't come up, start the workstation bot again and
   debug with no clock running; nothing is lost.
3. **[ DISCORD ]** `/ping`, then `/sql status` — it should report the connection
   up and 10 registered queries.
4. **[ DISCORD ]** `/setup show` in each league server.
5. **[ DISCORD ]** Report one throwaway rumor and read it back with
   `/rumor recent`, then confirm the row landed on the **new** box and not the
   old one:

   **[ WORKSTATION ]**
   ```bash
   ssh wojingtonpost    "sudo -u postgres psql -Atc \
     'SELECT count(*) FROM fantasy_sports.fact_rumor' wojbot_db"
   ssh oldpi            "sudo -u postgres psql -Atc \
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
workstation again.

Use `sudo systemctl disable --now wojbot` rather than a bare `stop`. Step 7
enabled it for boot, so a stopped-but-enabled unit comes back on the Pi's next
reboot and quietly rejoins Discord against the new warehouse — with the
workstation bot also running by then.

Its config still names `thegoldenunasinn` — the very thing
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
192.168.1.165   thegoldenunasinn
192.168.50.60   wojingtonpost
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
