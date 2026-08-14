# Moving to EC2

Notes for lifting the bot and the warehouse off the Pi and onto a single EC2
instance. Written against the current setup: bot and Postgres 17 both on
`wojingtonpost`, a Raspberry Pi running Debian trixie on aarch64, with the
original Pi `thegoldenunasinn` still powered up and still serving an un-retired
`wojbot_db`.

The shape is the same as the Pi move — dump, restore, verify, *then* switch —
and for the same reason: the source machine keeps running the whole time, so
nothing here has to be done under pressure and the Pi stays a working rollback
for as long as you want one.

Three things are genuinely new, and they are what most of this document is
about:

- **Secrets stop living in `.env`.** They move to SSM Parameter Store and are
  fetched at start by an instance role. That removes a file from the box and
  adds a way for the bot to fail that a Pi never had.
- **The box costs money by the hour and sits on the public internet.** Both are
  design inputs now. Neither was on a Pi in your living room.
- **There is no physical access.** On a Pi, a bad `sshd_config` is a walk to the
  other room with a keyboard. Here it's an instance you can't reach. Session
  Manager is the replacement for that walk, and it's the reason the IAM role
  goes on before anything else.

**Three machines now hold a database called `wojbot_db`**, and after this move
there will be four. That was the sharpest edge in the Pi migration and it is
sharper here, because a config pointing at the wrong one still won't fail — it
will connect, and the copies will drift apart with nothing to tell you. As of
today `pylot` can still log into `thegoldenunasinn` over the network from the
workstation; that route was never closed. Step 11 closes all of them.

---

## The four machines

Every command block below is tagged with the box it runs on.

| tag | machine | what it is | reached by |
|---|---|---|---|
| **[ WORKSTATION ]** | the x86_64 desktop | where this repo lives, at `/home/weezy/activity/WojBot`. Runs no bot any more. | you're sitting at it |
| **[ PI ]** | `wojingtonpost` | 192.168.50.60 — trixie, aarch64, Postgres 17, Python 3.13. **Runs the bot today.** Checkout at `/home/piders994/activity/WojBot`. | `ssh wojingtonpost` |
| **[ OLD PI ]** | `thegoldenunasinn` | 192.168.1.165 — Postgres 13.23, bullseye, 32-bit. Still up, still serving a stale `wojbot_db`. | `ssh oldpi` (port 50314) |
| **[ EC2 ]** | `pressbox` | the new instance — ends up running *both* Postgres and the bot. Checkout at `/home/ubuntu/activity/WojBot`. | `ssh pressbox`, once step 2 sets it up |

`pressbox` is a suggestion, not a requirement — but like `wojingtonpost` in the
Pi guide it is written out in full throughout rather than left as a placeholder,
so the commands can be run as-is. If you want a different name, change it in
`~/.ssh/config`, in the `hostnamectl` call in step 3, and nowhere else.

**The home directory is `/home/ubuntu`, not `/home/piders994`.** Ubuntu's AMI
gives you an `ubuntu` account and there is no reason to fight it, but it does
mean every path you have in muscle memory from the Pi is wrong by one component.
`hostname` settles which box you're on; `pwd` settles which checkout.

## The move at a glance

| step | runs on | what happens |
|---|---|---|
| 1 | AWS | IAM role, key pair, security group — the things the instance needs at launch |
| 2 | AWS | launch it: AMI, instance type, storage, address |
| 3 | EC2 | hostname, swap, locale, **Postgres 17 from PGDG** |
| 4 | WORKSTATION | put the secrets into SSM Parameter Store |
| 5 | PI → EC2 | dump, ship, restore, verify |
| 6 | EC2 | clone the repo, build the venv from the lock |
| 7 | PI → EC2 | rsync the gitignored state — **from the Pi, not the workstation** |
| 8 | EC2 | the SSM wrapper and the systemd unit |
| 9 | AWS + EC2 | backups, which the Pi never had |
| 10 | everywhere | cut over and prove the writes land on EC2 |
| 11 | everywhere | close every route to every old warehouse |

Steps 1–9 are all reversible and none of them touch the running bot. Step 10 is
the only one that changes what's live.

---

## Sizing, and why arm64

Pick a **Graviton** instance — `t4g.small`. It is aarch64, exactly like the Pi,
which means `requirements.lock` is already proven on this architecture: the
wheels that install on `wojingtonpost` are the same wheels that install here. An
x86_64 `t3.small` works too and every package in the lock publishes both, but
you'd be swapping a known-good architecture for an unknown-good one to no
purpose, and paying about 20% more for the privilege.

**Not `t4g.micro`.** 1 GiB has to hold Postgres' shared buffers *and* a Python
process with pandas and numpy resident — the `import pandas` alone costs well
over 100 MB of RSS before the bot does anything. 2 GiB is comfortable; 1 GiB is
the kind of tight that shows up as the OOM killer taking whichever process is
larger, at 3am, once a month.

`t4g` instances launch in **`unlimited`** burst mode by default, which bills a
surcharge for CPU sustained above the baseline instead of throttling you. A
mostly-idle Discord bot will never touch it, but if you want a hard ceiling on
the bill rather than a hard ceiling on performance, set credit specification to
`standard` at launch.

**Storage: 20 GiB `gp3`.** gp3 includes 3,000 IOPS and 125 MB/s at no extra
charge, which is the end of the SD-card problem that shaped the Pi guide — no
wear-levelling stalls, no seconds-long blocking writes that look like the host
vanishing. 20 GiB is generous for a warehouse this size; the reason not to go
smaller is journald, the rotating `wojbot.log`, and enough headroom to write a
`pg_dump` locally before shipping it.

### The AMI: Ubuntu 24.04 LTS, arm64

Ubuntu rather than Amazon Linux 2023, for one reason that matters during a
migration: it's Debian-family, so `pg_lsclusters`, `/etc/postgresql/17/main/`,
and `postgresql@17-main.service` all mean exactly what they meant on the Pi.
Every path and unit name carries over unchanged. AL2023 uses the upstream layout
(`/var/lib/pgsql/data`, a plain `postgresql.service`), so choosing it means
relearning where things live at the same moment you're moving data — two
unfamiliar things at once instead of one.

If you'd rather run a newer LTS, that's fine; just run `pg_lsclusters` after
step 3 and substitute the version number into the unit file in step 8, the same
way the Pi guide handles bookworm-versus-trixie.

## 1. The things that must exist before launch

### The IAM role

Do this first. The role is what makes the instance reachable when SSH isn't, and
attaching it at launch is much less annoying than attaching it afterwards.

Create an IAM role for the **EC2** service named `WojBotInstanceRole`, with:

- the AWS managed policy **`AmazonSSMManagedInstanceCore`** — this is what gives
  you Session Manager, and the SSM agent is already installed on Ubuntu's AMI;
- an inline policy for the parameters, `WojBotReadSecrets`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadWojBotParameters",
      "Effect": "Allow",
      "Action": ["ssm:GetParametersByPath", "ssm:GetParameters", "ssm:GetParameter"],
      "Resource": "arn:aws:ssm:us-east-1:<account-id>:parameter/wojbot/prod/*"
    },
    {
      "Sid": "DecryptThem",
      "Effect": "Allow",
      "Action": "kms:Decrypt",
      "Resource": "*",
      "Condition": {
        "StringEquals": {"kms:ViaService": "ssm.us-east-1.amazonaws.com"}
      }
    }
  ]
}
```

Two things about that policy. The **`kms:Decrypt` statement is not optional** —
SecureString parameters encrypted with the AWS-managed key `alias/aws/ssm` still
require the caller to hold `kms:Decrypt` in its own identity policy, and leaving
it out produces an `AccessDeniedException` from a call that looks like it should
only need `ssm:` permissions. The `kms:ViaService` condition is what keeps
`"Resource": "*"` from meaning "decrypt anything in the account" — it narrows the
grant to decryption requests that arrive through Parameter Store.

And the resource path must match how you'll read them: `GetParametersByPath` on
`/wojbot/prod/` is authorised by `.../parameter/wojbot/prod/*`. A trailing-slash
mismatch here is a five-minute puzzle later.

### The key pair and the security group

Create a key pair (ed25519) and keep the private half on the workstation at
`~/.ssh/wojbot-ec2.pem`, mode `600`.

Security group `wojbot-sg`:

| direction | port | source/dest | why |
|---|---|---|---|
| in | 22/tcp | **your workstation's public IP, /32** | `rsync`, `scp`, ordinary work |
| in | — | nothing else | in particular **never 5432** |
| out | all | 0.0.0.0/0 | Discord's gateway and REST, apt, PyPI, the SSM endpoints |

Get the source address with `curl -s https://checkip.amazonaws.com`. It's a
residential IP so it will move eventually, and when it does the fix is to edit
the rule — not to widen it. Session Manager is what gets you in while you sort
that out, which is the other half of why the role goes on at launch.

**Nothing inbound on 5432, ever.** Postgres stays on `listen_addresses =
'localhost'` exactly as it does on the Pi. The bot talks to it over loopback and
nothing else needs to.

## 2. Launch

- **AMI**: Ubuntu Server 24.04 LTS, **arm64**
- **Type**: `t4g.small`
- **Key pair**: the one from step 1
- **Security group**: `wojbot-sg`
- **Storage**: 20 GiB `gp3`
- **IAM instance profile**: `WojBotInstanceRole`
- **Advanced → Metadata version**: IMDSv2 required (the default on new
  instances). Leave the hop limit at 1 — that's correct for a bot running
  directly on the host. It would need to be 2 only if the bot ran in a
  container, which would put an extra network hop between it and the metadata
  service.

### Give it a fixed address, and know what it costs

Allocate an **Elastic IP** and associate it. This is the DHCP-reservation step
from the Pi guide, and it matters more here: without it, stopping and starting
the instance hands you a *different* public address and every `ssh`, `scp` and
`rsync` in this guide silently starts pointing at nothing.

There is no cost argument against it. Since February 2024 **every public IPv4
address bills at about $0.005/hour (~$3.60/month)** whether it's an
auto-assigned address or an Elastic IP. You are paying that either way; the only
question is whether the address you're paying for stays put. (An Elastic IP
allocated and *not* associated with anything bills at the same rate — so if you
tear the instance down later, release the address too.)

Then, on the workstation, `~/.ssh/config`:

```
# EC2 — the migration target. Elastic IP, so this address is stable.
Host pressbox wojbot-ec2
    HostName <elastic-ip>
    User ubuntu
    IdentityFile ~/.ssh/wojbot-ec2.pem
```

```bash
ssh pressbox hostname     # answers, so the SG rule and the key are both right
```

Worth actually running before you go further. If it hangs, it's the security
group (a wrong source address gives you a timeout, not a refusal); if it refuses
the key, it's the key pair or the `IdentityFile` path.

### Prove Session Manager works *now*, not when you need it

**[ WORKSTATION ]**

```bash
aws ssm start-session --target <instance-id>
```

This needs the `session-manager-plugin` installed locally. Do it today, while
SSH also works, so that you find out about a missing plugin or a role that
didn't attach at a moment when it costs you nothing. The whole value of this
path is that it works when the other one doesn't, and an untested escape hatch
is not an escape hatch.

If it fails: the agent takes a minute or two after first boot to register, and
`aws ssm describe-instance-information` tells you whether it ever did. An
instance that never appears there has no role attached, or no route out to the
SSM endpoints.

## 3. Base system

**[ EC2 ]**

```bash
sudo hostnamectl set-hostname pressbox
sudo apt update && sudo apt upgrade -y
sudo apt install -y git python3-venv python3-pip rsync unzip
uname -m        # aarch64
python3 -V      # 3.12 on 24.04 — anything >= 3.11 is fine
```

### Swap

Cloud images ship with none, and 2 GiB of RAM with no swap means the OOM killer
is your only backstop.

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### The locale, before Postgres

Both `CREATE DATABASE` lines in the dump carry `LOCALE = 'en_GB.UTF-8'`,
inherited from the original Raspbian install and carried faithfully through the
Pi migration. Ubuntu's cloud image has only `C.UTF-8`, so the restore would get
all the way through the roles and then fail with `invalid locale name`.

Confirm what the Pi actually has, rather than trusting this paragraph:

**[ PI ]**

```bash
sudo -u postgres psql -Atc "SELECT datname, datcollate FROM pg_database ORDER BY 1"
```

Then generate it here:

**[ EC2 ]**

```bash
sudo locale-gen en_GB.UTF-8
locale -a | grep -i en_GB
```

Do this **before** installing Postgres, so the cluster's own template databases
are created against a locale the system already has.

### Postgres 17 — and it has to be 17

This is the one step where following the Pi guide's habits gets you a broken
restore.

`wojingtonpost` runs **PostgreSQL 17** (trixie's version). Ubuntu 24.04's
archive has **16**. `pg_dump` and `pg_dumpall` are forward-compatible only —
dumping from 17 and restoring into 16 is the unsupported direction, and it does
not fail cleanly at the start. It fails partway through, on whatever the first
17-only piece of syntax happens to be, leaving `ON_ERROR_STOP` to abort a
cluster that is already half full of roles.

So take Postgres from the PGDG archive instead:

**[ EC2 ]**

```bash
sudo apt install -y curl ca-certificates
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl --fail -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list
sudo apt update
sudo apt install -y postgresql-17
```

**Install `postgresql-17` before anything else pulls in Postgres.** The Debian
packaging gives port 5432 to the first cluster created and 5433 to the next, so
if a stray `apt install postgresql` has already brought up Ubuntu's 16, your 17
lands on 5433 and every connection string in this guide quietly reaches the
wrong — and empty — cluster.

```bash
pg_lsclusters     # expect exactly one line: 17  main  5432  online
```

One line, version 17, port 5432. Two lines means the above happened; drop the
one you don't want with `sudo pg_dropcluster --stop 16 main` before continuing.

Installing the package leaves a running, empty cluster, which is precisely what
step 5 wants: **don't create the database, the schemas or the roles by hand.**

## 4. The secrets go into Parameter Store

Standard-tier `SecureString` parameters encrypted with the AWS-managed
`alias/aws/ssm` key. Standard tier is free — no per-parameter charge, and the
4 KB limit is far more than a token needs. Advanced tier exists and you don't
need it.

Everything goes under **one path**, so the wrapper in step 8 can fetch the lot
in a single API call, and **each parameter is named for the environment variable
it becomes**:

```
/wojbot/prod/DISCORD_TOKEN
/wojbot/prod/LOG_LEVEL
/wojbot/prod/SQL_HOST
/wojbot/prod/SQL_PORT
/wojbot/prod/SQL_USER
/wojbot/prod/SQL_PASSWORD
/wojbot/prod/SQL_DBNAME
```

**[ WORKSTATION ]**

```bash
put() { aws ssm put-parameter --name "/wojbot/prod/$1" --type SecureString \
          --value "$2" --overwrite >/dev/null && echo "  set $1"; }

put SQL_HOST     127.0.0.1
put SQL_PORT     5432
put SQL_USER     pylot
put SQL_DBNAME   wojbot_db
put LOG_LEVEL    INFO
```

For the two real secrets, **don't type the value as an argument.** It goes into
your shell history and is visible in `ps` for as long as the call runs. Read
them from the file that already holds them instead:

```bash
put DISCORD_TOKEN "$(ssh wojingtonpost 'grep -m1 ^DISCORD_TOKEN= activity/WojBot/.env' | cut -d= -f2-)"
```

and for the database password, either the same trick against the Pi's `.env` or
`read -rs PW && put SQL_PASSWORD "$PW"`, which at least keeps it out of history.
Either way, verify without printing:

```bash
aws ssm get-parameters-by-path --path /wojbot/prod/ --recursive \
  --query 'Parameters[].{name:Name,len:length(Value)}' --output table
```

Seven rows, and the two secret ones have plausible lengths. (`--with-decryption`
is deliberately absent — you want to know they're there, not what they say.)

### Two parameters that must not exist

**Never create `/wojbot/prod/SQL_CONN_URI`, and never put a `.env` on this
instance.** This is the single most dangerous thing in the whole move, and it's
worth understanding rather than just obeying, because the failure is silent.

`Settings.load()` calls `load_dotenv()`, which by default **does not override**
variables already present in the environment. So anything the wrapper exports
from SSM beats a same-named key in a `.env` file. That sounds like a `.env` is
harmless. It isn't:

`resolve_sql_uri()` in `wojbot/core/config.py` checks **`SQL_CONN_URI` first**,
and it wins whenever it is set at all. The wrapper exports `SQL_HOST`,
`SQL_USER`, `SQL_PASSWORD` and friends — it never exports `SQL_CONN_URI`, so
there is nothing to shadow it with. A `.env` copied up out of habit, carrying
the workstation's still-present
`SQL_CONN_URI=postgresql://pylot:…@thegoldenunasinn:5432/wojbot_db`, would send
the EC2 bot straight to the **oldest** of the three warehouses, over your home
internet connection, and it would *work*. Rumors would land in a database
nobody's looked at since last year.

Also **don't create `DISCORD_GUILD_ID`.** Same reasoning as the Pi: the bot
syncs commands globally and then, if that variable is set, copies them into the
one guild and syncs again, which registers each command twice in that server's
picker (`wojbot/bot.py`). It's a development convenience and this is production.
The Pi already had it cleared, so there is nothing to clean up on Discord's side
this time — just don't reintroduce it.

## 5. Dump, ship, restore

### Dump on the Pi

**[ PI ]**

```bash
sudo -u postgres pg_dumpall > wojbot-full-$(date +%F).sql
sudo -u postgres pg_dump -Fc wojbot_db > wojbot_db-$(date +%F).dump   # belt and braces
```

Run `pg_dumpall` **as a superuser** and without `--no-role-passwords`, or the
role passwords are silently left out and `pylot` restores as a login role that
can't log in.

Record what you expect on the other side:

**[ PI ]** — `sudo -u postgres psql wojbot_db`

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

Take the dump **while the bot is still running**, and take it again just before
the cutover in step 10 if a lot of rumors land in between. The counts you record
here are the ones you check in step 5's verification, so they have to come from
the same dump.

### Ship it

The Pi has no key for EC2 and doesn't need one; go through the workstation.

**[ WORKSTATION ]**

```bash
mkdir -p ~/backups
scp wojingtonpost:'wojbot-*.sql' wojingtonpost:'wojbot_db-*.dump' ~/backups/
scp ~/backups/wojbot-full-2026-08-14.sql ~/backups/wojbot_db-2026-08-14.dump pressbox:
```

Keeping a copy on the workstation in passing is deliberate: at that moment the
data exists in three places, which is the only point in this process where
you're properly covered.

### Restore

**[ EC2 ]**

```bash
sed -e '/^CREATE ROLE postgres;$/d' \
    wojbot-full-2026-08-14.sql \
  | sudo -u postgres psql -v ON_ERROR_STOP=1 -f - 2>&1 \
  | tee ~/restore.log
```

The three lessons from the Pi restore, and where they stand now:

**Feed it in on stdin, not with `-f wojbot-full-….sql`.** Still true, for the
same reason: `psql` runs as `postgres`, Ubuntu 24.04 creates home directories
`0750`, and `postgres` can't traverse into `/home/ubuntu` to read a file you
just `scp`'d there. You'd get `Permission denied` before a single statement
runs. The `sed` reads the file as *you*, before `sudo` lowers privileges. Don't
`chmod o+x` your home to work around it; if you want a real path to re-run,
`sudo mv` both files to `/var/tmp` and `sudo chown postgres:` them.

**Filtering `CREATE ROLE postgres;` is still needed.** `pg_dumpall` does not
special-case the bootstrap superuser at any version, so 17 emits it just as 13
did, and it is the one statement in the file that cannot succeed against a
freshly-initdb'd cluster. The `ALTER ROLE postgres WITH …` on the next line
carries the actual attributes, so deleting the `CREATE` loses nothing.

**The `GRANTED BY` filter is no longer needed** — but it's worth knowing why,
because the reasoning is what tells you it's safe to drop. PostgreSQL 16
tightened `GRANT` so the named grantor must hold ADMIN OPTION on the role being
granted, with only the bootstrap superuser (OID 10) exempt. The Pi restore
stripped those clauses, which meant the grants were replayed by the connected
superuser, so `pg_auth_members` on the Pi now records **`postgres`** as the
grantor of every membership. `pg_dumpall` 17 therefore emits `GRANTED BY
postgres`, which is the exempt case. Re-adding
`-e 's/^\(GRANT .*\) GRANTED BY [^;]*;$/\1;/'` would be a harmless no-op if you'd
rather not think about it.

`ON_ERROR_STOP=1` is what turns a silent partial restore into an obvious one.
Don't reach for `--single-transaction` instead: the dump uses `\connect` to
switch databases, which can't happen inside a transaction block.

> **If a restore aborts partway, reset the cluster before retrying.** psql
> auto-commits each statement, so an abort in the globals section leaves behind
> every role it had already created and the retry dies on the first of them.
>
> ```bash
> sudo pg_dropcluster --stop 17 main
> sudo pg_createcluster --locale en_GB.UTF-8 --start 17 main
> ```

### Verify before trusting it

**[ EC2 ]** — `sudo -u postgres psql wojbot_db`

- Re-run the count query from above and compare every number.
- `\du` — expect `pylot`, `piders994`, `moderator`, `power_user`, `manager`,
  `consumer`.
- Ownership and grants:
  ```sql
  SELECT tablename, tableowner FROM pg_tables WHERE schemaname='fantasy_sports';
  SELECT grantee, privilege_type FROM information_schema.table_privileges
   WHERE table_schema='fantasy_sports' AND table_name='dim_league';
  ```
  Owner `piders994`, and `moderator` holding SELECT/INSERT/UPDATE/DELETE.
- And that `pylot` can actually log in and read:
  ```bash
  psql "postgresql://pylot:PASSWORD@127.0.0.1:5432/wojbot_db" -c \
    "SELECT count(*) FROM fantasy_sports.dim_source_type"
  ```

**No password re-setting this time.** The md5 problem in the Pi guide came from
a Postgres 13 source; 17 → 17 means the roles are already `scram-sha-256` and
the verifiers restore and work as-is. Confirm rather than assume:

```sql
SELECT rolname, left(rolpassword, 6) FROM pg_authid WHERE rolcanlogin;
```

Every row `SCRAM-`, except `postgres` which is blank because it authenticates by
peer.

### Two settings the dump doesn't carry

`pg_hba.conf` and `postgresql.conf` are not in the dump. The defaults are
already what you want — loopback with `scram-sha-256`, and nothing else — but
pin the listener explicitly:

**[ EC2 ]** — `/etc/postgresql/17/main/postgresql.conf`

```
listen_addresses = 'localhost'
```

```bash
sudo systemctl restart postgresql@17-main   # listen_addresses needs a restart, not a reload
```

**The server timezone will differ, and that's fine — but know why.** The Pi's
cluster runs `America/New_York`; EC2 runs UTC. `fact_rumor.reported_at` is
`timestamp with time zone` defaulting to `now()`, so what's stored is an
absolute instant either way, psycopg2 hands the bot an aware `datetime`, and
`Rumor.timestamp` in `wojbot/core/rumor.py` renders a correct Discord timestamp
regardless of what either machine thinks the local zone is. Nothing to fix.

What *does* change is anything that reads the wall clock: `psql` renders
existing rows five hours later than you're used to, and any `::date` cast or
`date_trunc('day', …)` you write by hand now buckets by UTC days. If that
bothers you, `ALTER DATABASE wojbot_db SET timezone = 'America/New_York';` makes
the new box read like the old one without touching a single stored value.

## 6. Install the bot

**[ EC2 ]**

```bash
mkdir -p ~/activity && cd ~/activity
git clone https://github.com/riders994/WojBot.git && cd WojBot
pwd                     # must print /home/ubuntu/activity/WojBot
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install -e . --no-deps
```

HTTPS rather than ssh, because the instance has no deploy key and a public clone
needs none. If you'd rather push from the box, `ssh-keygen -t ed25519` and add
the public half as a deploy key on `riders994/WojBot`.

The `activity/` level is deliberate, mirroring both other machines, so the same
relative path means the same thing everywhere. That `pwd` matters because three
later things are pinned to this exact directory: step 7's rsync target, step 8's
`WorkingDirectory`, and the `exec` at the end of the wrapper. Clone one level up
by mistake and the rsync quietly builds a second, resource-only `WojBot` next to
the real checkout — no error, no warning.

**Install from `requirements.lock`, not from `pyproject.toml`.** The constraints
there are ranges, so a plain `pip install -e .` resolves them against whatever is
newest today, and the whole point is that EC2 runs what the Pi has been running.
`--no-deps` on the second line stops pip resolving the ranges again on top.

Everything comes from public PyPI — `rv-pytools`, `elo-system` and `sleeper` are
all published — so there's no private index to configure. **Don't copy `.venv/`**
from anywhere, including the Pi: it hardcodes absolute paths, and
`/home/piders994` doesn't exist here.

## 7. Copy the state the repo doesn't carry

These are gitignored, so the clone doesn't have them:

```
resources/configs/sql_config.yml
resources/anon/                         (the reversal maps)
resources/ratings/                      (per-league Elo CSVs)
resources/configs/servers/              (per-guild overrides)
resources/configs/leagues/              (per-league overrides)
```

**Copy them from the Pi, not from the workstation.** This is the reversal from
the Pi guide and the easiest thing to get wrong out of habit. The Pi has been
the live bot since the last migration, so its `resources/` is current and the
workstation's is frozen at whatever it was on cutover day: every rating written
since then, every `/setup` change, and every anonymizer entry minted for a new
manager exists only on `wojingtonpost`.

**`.env` is not in that list and must not be added to it.** See step 4.

Two hops again, through the workstation:

**[ WORKSTATION ]**

```bash
S=/tmp/wojbot-state ; P=wojingtonpost:activity/WojBot
mkdir -p $S/resources/configs

rsync -av $P/resources/anon/                $S/resources/anon/
rsync -av $P/resources/ratings/             $S/resources/ratings/
rsync -av $P/resources/configs/sql_config.yml $S/resources/configs/
rsync -av --ignore-missing-args $P/resources/configs/servers/ $S/resources/configs/servers/
rsync -av --ignore-missing-args $P/resources/configs/leagues/ $S/resources/configs/leagues/

rsync -av $S/ pressbox:activity/WojBot/
```

Staging in `/tmp` rather than piping Pi-to-EC2 in one shot is deliberate: it
lets you look at exactly what's about to land (`find $S -type f`) before it
lands, and the Pi has no key for EC2 anyway.

`--ignore-missing-args` keeps the last two pulls a no-op rather than an error if
`/setup` never persisted anything — though on the Pi it almost certainly has, and
those files carry the rumor channel, `admin_roles`, and which Elo league each
server is bound to. Where they're absent the bot falls back to
`wojbot/core/defaults.py`, which means `rumor_channel: None` and **rumors that
record but go unannounced until you run `/setup rumorchannel` again**.

**`resources/anon/` is not optional.** Those are the anonymizer's reversal maps.
Without them rumors print `manager_18` instead of a name and — worse — the next
`/commish sync` finds no map, starts numbering from zero, and mints `manager_0`
for a *different* person than the `manager_0` already in the warehouse, silently
repointing names on rows that are already there.

`resources/anon/discord_ids.json`, if it exists, is the **most important file in
the transfer**: it is the only place the mapping from real Discord IDs to the
surrogates stored in `dim_league.discord_server_id` and `dim_manager.discord_id`
lives. Lose it and every `/commish db link` and `linkuser` is gone.

**Do not copy** `sql/manager.log.json` or `sql/queries/_queries.json` — they
regenerate, and a stale log makes the bot skip registering the query files.

### Then check nothing points anywhere but here

**[ EC2 ]**

```bash
grep -rnE 'thegoldenunasinn|wojingtonpost|192\.168\.' ~/activity/WojBot \
  --exclude-dir=.git --exclude-dir=.venv
ls ~/activity/WojBot/.env 2>/dev/null && echo "!!! delete this !!!"
```

The grep should print nothing outside `docs/`. `resources/configs/sql_config.yml`
already says `127.0.0.1` — the Pi migration set it — which happens to be correct
here too, so for once there is nothing to edit. Verify it rather than assuming
it; that file is read by `EloSystem` through `sys_config.yml`'s
`sql_config_name`, resolved against `resources/configs/`, and it is the one that
gets missed.

## 8. The wrapper and the unit

### Fetching the secrets

The AWS CLI first — Ubuntu's `awscli` package is v1 and old:

**[ EC2 ]**

```bash
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp && sudo /tmp/aws/install
aws sts get-caller-identity     # must show an assumed-role ARN for WojBotInstanceRole
```

You never run `aws configure`. Credentials come from the instance profile via
IMDS, which is exactly the point — there is no access key on this box to leak.
If `get-caller-identity` fails here, stop and fix it; nothing downstream can
work.

Then `/usr/local/bin/wojbot-run`:

```python
#!/usr/bin/python3
"""Fetch WojBot's secrets from SSM into the environment, then exec the bot.

Nothing is written to disk and nothing is passed as a command-line argument, so
the values never appear in `ps`, in the journal, or in a file somebody forgets
about. The parameter's last path component is the environment variable name.
"""
import json
import os
import subprocess
import sys

BOT = "/home/ubuntu/activity/WojBot/.venv/bin/wojbot"
PATH = "/wojbot/prod/"

result = subprocess.run(
    ["aws", "ssm", "get-parameters-by-path",
     "--path", PATH, "--recursive", "--with-decryption",
     "--query", "Parameters[].[Name,Value]", "--output", "json"],
    capture_output=True, text=True, check=True,
)

env = dict(os.environ)
for name, value in json.loads(result.stdout):
    env[name.rsplit("/", 1)[-1]] = value

if not env.get("DISCORD_TOKEN"):
    sys.exit(f"no DISCORD_TOKEN under {PATH} — check the instance role and the path")

os.execve(BOT, [BOT], env)
```

```bash
sudo install -o root -g root -m 755 wojbot-run /usr/local/bin/wojbot-run
```

Four deliberate choices in twenty lines, each of which is a way this goes wrong
if you do it the obvious way instead:

**Python rather than bash.** The natural bash version is
`eval "$(aws ssm … | jq -r '…')"`, and `eval` on a value you fetched over the
network is a command-injection hole in your init path — a parameter containing
`$(…)` executes. The next-safest bash version reads NUL-delimited pairs out of
`jq`, which works but depends on jq handling embedded NULs the way you hope.
JSON through the standard library sidesteps all of it: values can contain
newlines, `=`, quotes, anything.

**`check=True`, and the explicit `DISCORD_TOKEN` guard as well.** Not
redundant — they catch different things. `check=True` catches the CLI exiting
non-zero (no role, throttled, no route to the endpoint). The guard catches the
CLI *succeeding* and returning an empty list, which is what a typo in `PATH` or
a policy scoped to the wrong prefix produces. Without it the bot starts,
`Settings.load()` raises `ConfigError: DISCORD_TOKEN is not set`, and you spend
your time looking at the bot instead of at IAM.

This is also why the wrapper isn't an `ExecStartPre` that writes an
`EnvironmentFile`. That pattern works, but it puts the decrypted token in a file
— even under `/run` — for the lifetime of the service, for no gain.

**`os.execve`, not `subprocess.run`.** The bot *replaces* this process, so
systemd's `Restart=` watches the bot and `SIGTERM` on `systemctl stop` reaches
it directly. A wrapper that stayed alive as the parent would have to forward
signals itself, and would get that wrong.

**`/usr/bin/python3`, standard library only.** No boto3, so the wrapper doesn't
depend on the venv at all and keeps working if you rebuild it.

Test it before wiring up systemd — but note this actually starts the bot, and
the Pi's bot is still running, so kill it as soon as it logs in:

```bash
sudo -u ubuntu /usr/local/bin/wojbot-run     # Ctrl-C once you see it connect
```

### The unit

**[ EC2 ]** — `/etc/systemd/system/wojbot.service`

```ini
[Unit]
Description=WojBot
After=network-online.target nss-lookup.target time-sync.target postgresql@17-main.service
Wants=network-online.target
Requires=postgresql@17-main.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/activity/WojBot
Environment=AWS_DEFAULT_REGION=us-east-1
ExecStartPre=-/usr/local/bin/wojbot-update
ExecStart=/usr/local/bin/wojbot-run
TimeoutStartSec=300
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

`postgresql@17-main.service` carries the cluster version, and it must be the
real versioned unit — **not** plain `postgresql.service`, which on Debian and
Ubuntu is a wrapper whose entire job is `ExecStart=/bin/true`. It reports
success the instant systemd looks at it whether or not a cluster came up, so
`Requires=` on it is a dependency that cannot fail.

`AWS_DEFAULT_REGION` isn't strictly required — CLI v2 can discover the region
from IMDS — but being explicit removes a metadata round trip from every start
and one thing from the list when a start fails.

**`WorkingDirectory` is load-bearing, for a different reason than on the Pi.**
There, it was `load_dotenv()` finding `.env` relative to the current directory.
There's no `.env` here, so that reason is gone — but
`wojbot/core/logging.py` sets `_LOG_FILE = Path("wojbot.log")`, also relative to
the current directory, and `RotatingFileHandler` opens it eagerly during
`configure_logging()`. A unit without `WorkingDirectory` starts in `/`, tries to
create `/wojbot.log` as `ubuntu`, and dies with a `PermissionError` traceback
before it has logged one useful line. (Everything under `resources/` is fine
either way: `PROJECT_ROOT` derives from the installed package location, not the
working directory.)

```bash
sudo systemctl daemon-reload && sudo systemctl enable wojbot
systemctl show wojbot -p Restart -p RestartSec -p Requires -p After
```

**`enable`, not `enable --now`.** The Pi's bot is still running and still holds
the same Discord token; two instances on one token against two warehouses is
exactly the divergence this guide exists to prevent. Step 10 performs the only
start.

And note that the check above is `systemctl show`, not `systemctl cat`.
`cat` prints the bytes on disk and tells you nothing about whether systemd
accepted them. Directive *values* are case-sensitive: a capitalised
`Restart=Always` is not a near-miss, it's invalid — systemd logs a parse warning,
discards the line, and leaves `Restart=no`. The unit then looks perfect in `cat`
and has no restart policy at all, which is quiet until the first crash and then
costs you a full outage. `Restart=always` in the `show` output is the answer;
`Restart=no` means go read `journalctl -b -u wojbot | grep -i 'restart setting'`.

### Updating the checkout on start

`ExecStartPre=` runs before `ExecStart=` in the same service, as the same user,
and that's the hook for pulling the latest code and reconciling the venv against
`requirements.lock` before the bot comes up. It makes `systemctl restart wojbot`
your deploy command.

Two things about this unit make the naive version actively worse than no updater
at all, and both are why the script below is shaped the way it is.

**A failed `ExecStartPre` fails the whole unit.** By default, if the pre-command
exits non-zero, systemd never runs `ExecStart` and the service goes to `failed`.
So a `git pull` in front of this bot means **GitHub being unreachable stops the
bot from starting** — even though a perfectly good checkout is sitting on the
disk. That is precisely the post-outage boot the unit was hardened against,
re-introduced through the front door. Unreachable network at boot must mean
"start what we've got", never "don't start".

**`ExecStartPre` runs on automatic restarts too**, not just the ones you type.
With `Restart=always` and `RestartSec=30`, a crash-looping bot fetches every 30
seconds — and worse, a bad commit that crashes on startup will be pulled in and
retried forever rather than leaving the last good code running.

So: the script never exits non-zero, and it rolls the checkout back rather than
starting the bot against a venv it failed to build.

**[ EC2 ]** — `/usr/local/bin/wojbot-update`

```bash
#!/usr/bin/env bash
# Fast-forward the checkout and reconcile the venv before the bot starts.
#
# Exits 0 unconditionally. Every failure here means "start the code already on
# disk", which is always a better outcome than not starting: an unreachable
# GitHub at boot must not be able to keep the bot down.
set -uo pipefail

REPO=/home/ubuntu/activity/WojBot
BRANCH=primary

cd "$REPO" || exit 0
before=$(git rev-parse HEAD)

# BatchMode + a hard timeout: a prompt or a black-holed connection would
# otherwise hang here until TimeoutStartSec kills the whole start.
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=10"
export GIT_TERMINAL_PROMPT=0

if ! timeout 60 git fetch --quiet origin "$BRANCH"; then
    echo "fetch failed — starting $before as it is"
    exit 0
fi

if ! git -c advice.diverging=false merge --ff-only --quiet "origin/$BRANCH"; then
    echo "not a fast-forward (local commits, or a dirty tree) — starting $before"
    exit 0
fi

after=$(git rev-parse HEAD)
if [[ "$before" == "$after" ]]; then
    echo "already current at $after"
    exit 0
fi
echo "updated $before -> $after"

# Only touch the venv when something that describes it actually changed.
if ! git diff --quiet "$before" "$after" -- requirements.lock pyproject.toml; then
    echo "dependencies changed — reinstalling"
    if ! .venv/bin/pip install --quiet -r requirements.lock \
      || ! .venv/bin/pip install --quiet -e . --no-deps; then
        echo "pip failed — rolling back to $before and starting that"
        git reset --hard --quiet "$before"
    fi
fi
exit 0
```

```bash
sudo install -o root -g root -m 755 wojbot-update /usr/local/bin/wojbot-update
```

The pieces that matter:

**`ExecStartPre=-/usr/local/bin/wojbot-update`** — the leading `-` tells systemd
to ignore a non-zero exit. The script already never returns one, so this is
belt-and-braces for the cases the script can't cover: it's missing, it's not
executable, or somebody edits it later and breaks the `exit 0`.

**`git merge --ff-only`, not `git pull`.** A plain `pull` on a diverged branch
drops you into a merge — or a conflicted tree — inside a systemd start. `--ff-only`
refuses instead, and refusing is logged and survivable. This also means a
deliberate local edit on the box is respected rather than silently reverted:
you'll see "not a fast-forward" in the journal and know exactly why the deploy
didn't take.

**The rollback on pip failure.** Starting the bot against a half-installed venv
is the one outcome worse than not updating, so a failed install returns the
checkout to the commit that matched the venv you already have. `git reset --hard`
is safe for everything in step 7: it doesn't touch untracked or gitignored
files, so `resources/anon/`, `resources/ratings/` and the per-guild configs are
never at risk.

**The `git diff` guard on `requirements.lock` and `pyproject.toml`.** The venv is
an editable install, so ordinary code changes are picked up with no pip
involvement at all. Running pip on every start would add seconds to each restart
to do nothing; running it only when the files that define the environment changed
means a normal deploy is a fetch and a fast-forward.

**`TimeoutStartSec=300`** in the unit. The default is 90 seconds and it covers
`ExecStartPre` plus `ExecStart` together. A cold `pip install` that has to build
or download much of the lock will blow through 90s and get the start killed
partway through — leaving exactly the broken venv the rollback exists to
prevent.

Check it end to end before you rely on it:

```bash
sudo systemctl restart wojbot
journalctl -u wojbot -n 30 --no-pager | grep -E 'updated|current|failed|rolling'
```

#### Know what you've signed up for

This makes **restart mean deploy**, and those are two things you often want
separately. Restarting to clear a stuck gateway connection at 1am now also ships
whatever landed on `primary` since the last start, which is a surprise at the
worst possible time. If that trade bothers you — and it reasonably might — drop
`ExecStartPre` and run the same script deliberately instead:

```bash
sudo /usr/local/bin/wojbot-update && sudo systemctl restart wojbot
```

or put it on a timer that only restarts when the commit actually moved, which
gets you unattended deploys without coupling them to every crash recovery. The
script is written to be safe in all three modes; only the unit line changes.

### What `Restart=always` is actually covering here

Less than it was on the Pi, and it's worth knowing what changed.

The scenario the Pi guide designed for — power returns to the house, the Pi boots
in seconds off SSD, and the bot dies on `ClientConnectorDNSError` because the
FiOS gateway is still negotiating with the street — mostly can't happen here. The
VPC resolver at `.2` is up before your instance is, and `time-sync.target` is
nearly free too: EC2 provides the Amazon Time Sync Service on link-local
`169.254.169.123` and Ubuntu's AMI has chrony pointed at it out of the box, so
none of the Pi's RTC-battery worry applies. Clocks are simply correct here.

What `Restart=always` still earns its keep for:

- **Postgres losing the race.** `Requires=` orders the units, but the bot can
  still open a connection before the cluster finishes recovery on a cold start.
- **Discord gateway drops** that discord.py can't resume through.
- **Instance stop/start and AWS host retirement**, which are the EC2-shaped
  version of a power cut — you get an emailed retirement notice, but a
  spontaneous reboot still ends with the bot needing to come back by itself.

`RestartSec=30` keeps you clear of the start rate limiter: the default
`StartLimitBurst=5` counts starts inside a `StartLimitIntervalSec=10s` window, so
retries spaced wider than the window never accumulate. Tighten it below 10 and a
crash-looping bot trips the limiter and is left `failed` permanently — the exact
outcome the restart policy was meant to prevent.

## 9. Backups

The Pi had none of this, and it's the clearest thing you gain by moving. Do both
layers; they fail differently and recover differently.

### EBS snapshots — the whole box, cheap

Create a **Data Lifecycle Manager** policy: target the instance by tag (tag it
`Backup=wojbot` first), daily, retain 7. The DLM service itself is free; you pay
only for snapshot storage, and snapshots are incremental — after the first one,
a warehouse this size adds cents a month.

These are crash-consistent, not application-consistent, which is fine for
Postgres: restoring one looks to the cluster exactly like a power cut, and it
recovers from WAL on boot. That's the same guarantee the Pi gave you and it has
always been enough.

What a snapshot gets you is the whole machine back after you've destroyed it —
a bad `apt upgrade`, a full disk, a fat-fingered `rm`. What it can't do is give
you one table.

### `pg_dump` to S3 — the surgical one

This is what covers a bad `/commish` operation or a rumor deleted in error,
where you want yesterday's `fact_rumor` and not yesterday's entire filesystem.

Create a bucket (`s3://wojbot-backups-<account-id>`, versioning on, public
access blocked), add `s3:PutObject` on it to `WojBotReadSecrets`, and set a
lifecycle rule to move objects to Glacier Instant Retrieval after 30 days and
expire them after a year.

**[ EC2 ]** — `/usr/local/bin/wojbot-backup`

```bash
#!/usr/bin/env bash
set -euo pipefail
BUCKET=s3://wojbot-backups-<account-id>
STAMP=$(date -u +%Y-%m-%dT%H%M%SZ)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

sudo -u postgres pg_dump -Fc wojbot_db > "$TMP/wojbot_db-$STAMP.dump"
sudo -u postgres pg_dumpall --roles-only > "$TMP/roles-$STAMP.sql"
aws s3 cp "$TMP/wojbot_db-$STAMP.dump" "$BUCKET/daily/"
aws s3 cp "$TMP/roles-$STAMP.sql"      "$BUCKET/daily/"
```

Note the second dump. A `pg_dump` of one database does **not** contain the
roles, and a data-only restore into a cluster that has never heard of `pylot` or
`moderator` fails on the first `ALTER TABLE … OWNER TO`. The Pi migration needed
`pg_dumpall` for exactly this reason; a backup that skips it is a backup you
can't restore onto a fresh box.

Then a timer, rather than cron, so the failures land in the journal alongside
everything else:

`/etc/systemd/system/wojbot-backup.service`

```ini
[Unit]
Description=WojBot nightly logical backup

[Service]
Type=oneshot
Environment=AWS_DEFAULT_REGION=us-east-1
ExecStart=/usr/local/bin/wojbot-backup
```

`/etc/systemd/system/wojbot-backup.timer`

```ini
[Unit]
Description=Nightly WojBot backup

[Timer]
OnCalendar=daily
RandomizedDelaySec=30m
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now wojbot-backup.timer
sudo systemctl start wojbot-backup && journalctl -u wojbot-backup -n 20 --no-pager
```

`Persistent=true` runs a missed backup after a reboot rather than skipping the
day.

**Then restore one.** Pull yesterday's dump into a scratch database
(`createdb restore_test && pg_restore -d restore_test …`) and run the count
query against it. A backup you have never restored is a hypothesis, and the
moment you need it is a bad moment to test it.

## 10. Cut over

1. **[ PI ]** Stop the bot and confirm it's down. Both instances share one
   Discord token, so overlapping them gets you duplicated responses and two
   processes writing to two different warehouses.
   ```bash
   sudo systemctl stop wojbot && systemctl is-active wojbot
   ```
2. **[ PI ]** If more than a few rumors have landed since the step 5 dump, take
   a fresh `pg_dump -Fc wojbot_db` now — with the bot stopped, so it's quiet —
   and restore just that database over the EC2 copy. Skipping this loses
   whatever was reported during the migration.
3. **[ EC2 ]** Start it, and watch:
   ```bash
   sudo systemctl start wojbot && journalctl -u wojbot -f
   ```
   This is the **first** time the unit has ever run, so this is where a bad
   `WorkingDirectory`, a missing IAM permission, or a wrong parameter path shows
   up. If it won't come up, start the Pi's bot again and debug with no clock
   running; nothing is lost.
4. **[ DISCORD ]** `/ping`, then `/sql status` — the connection up and 10
   registered queries.
5. **[ DISCORD ]** `/setup show` in each league server, to confirm the per-guild
   config came across in step 7.
6. **[ DISCORD ]** Report one throwaway rumor, read it back with `/rumor
   recent`, and then prove where it landed:

   **[ WORKSTATION ]**
   ```bash
   for h in pressbox wojingtonpost oldpi; do
     printf '%-16s ' "$h"
     ssh $h "sudo -u postgres psql -Atc \
       'SELECT count(*) FROM fantasy_sports.fact_rumor' wojbot_db"
   done
   ```
   `pressbox` went up; the other two didn't move. If it's the other way round,
   something is still reading a config from step 7 — or there's a `.env` on the
   box.

## 11. Close every route

Now do this properly, for both Pis. The Pi guide left the old board's warehouse
reachable, which is why `pylot` can still log into `thegoldenunasinn` from the
workstation today — a config you miss will silently succeed against it rather
than failing loudly.

**[ PI ]** — `wojingtonpost`. Keep the data, drop the route:

```bash
sudo systemctl disable --now wojbot        # disable, not stop: see below
sudo -u postgres psql -c "ALTER ROLE pylot NOLOGIN"
sudo -u postgres psql -c "ALTER DATABASE wojbot_db RENAME TO wojbot_db_retired_2026_08"
```

**[ OLD PI ]** — `thegoldenunasinn`, the one that's been open all along:

```bash
sudo -u postgres psql -c "ALTER ROLE pylot NOLOGIN"
sudo -u postgres psql -c "ALTER DATABASE wojbot_db RENAME TO wojbot_db_retired_2026_08"
```

Or `sudo systemctl disable --now postgresql` on either board if nothing else
there needs it.

**[ WORKSTATION ]** — and finally, the config on your own desk. `.env` here
still holds `SQL_CONN_URI=postgresql://pylot:…@thegoldenunasinn:5432/wojbot_db`,
a live credential aimed at the stalest warehouse of the three. Point it at
nothing, or delete the line. The repo-root `sql_config.yml` is the same problem
and is on nobody's read path — delete it.

Keep the dumps for a couple of weeks regardless.

### Rolling back

Nothing before step 10 is destructive, so: stop and **disable** the unit on EC2,
undo whichever half of step 11 you'd applied on the Pi, and start the Pi's bot.

`sudo systemctl disable --now wojbot` rather than a bare `stop`, on whichever box
you're stepping away from. Step 8 enabled the unit for boot, so a
stopped-but-enabled service comes back on the next reboot and quietly rejoins
Discord against the wrong warehouse — with the other bot running by then too.

Rumors reported through EC2 in the meantime live only in the EC2 warehouse; if
that matters, `pg_dump -Fc -t fantasy_sports.fact_rumor wojbot_db` before you
turn it off.

---

## The bot is down and the instance is up

The same shape as on the Pi: work outward from the unit, not from the network.

**[ EC2 ]**

```bash
systemctl status wojbot --no-pager -l
journalctl -u wojbot -n 50 --no-pager
systemctl show wojbot -p Restart -p NRestarts
```

Read the `Active:` timestamp first and compare it to `uptime` — a failure that
matches boot time is a different bug from one that doesn't.

| what you see | what it means |
|---|---|
| `no DISCORD_TOKEN under /wojbot/prod/` | the wrapper's guard fired: role, policy path, or region. `aws sts get-caller-identity` next |
| `CalledProcessError` from the wrapper | the CLI itself failed — no route to the SSM endpoint, or IMDS is unreachable |
| `AccessDeniedException` naming `kms:Decrypt` | the second statement in the inline policy is missing or its `ViaService` region is wrong |
| `PermissionError: '/wojbot.log'` | `WorkingDirectory` — see step 8 |
| `ConfigError: DISCORD_TOKEN is not set` | the wrapper isn't being used at all; check `ExecStart` |
| `failed`, timestamp long after boot | a real crash. The traceback is in `journalctl` |
| `active (running)` but silent in Discord | not an EC2 problem. Token, gateway, or permissions |

`NRestarts=0` next to a `failed` service is the tell that no restart policy is in
force — go back to the `systemctl show` check in step 8.

Recovery is two commands. `reset-failed` clears the latched state, without which
a service parked at its start limit refuses to start at all:

```bash
sudo systemctl reset-failed wojbot
sudo systemctl start wojbot
```

Note what is *not* on that list: Postgres. It's on loopback, so it is nearly
never the answer, and `SQL connected` appears in the bot's own startup log within
a second of launch.

### When you can't get in at all

**Use Session Manager**, which is why step 1 set it up:

```bash
aws ssm start-session --target <instance-id>
```

It doesn't use port 22, your security group, or your SSH key, so it survives all
three being wrong. From there, `sudo su - ubuntu` and carry on.

The usual causes, in order: your home IP moved (check
`curl -s https://checkip.amazonaws.com` against the SG rule); the instance was
stopped and started without an Elastic IP, so the address changed; or the disk is
full and sshd can't fork. `df -h` first if you get in and things are strange —
journald plus a rotating `wojbot.log` plus a local `pg_dump` is the combination
that fills 20 GiB.

If even Session Manager is unreachable, the agent needs the instance to be
running and networked; the **EC2 Serial Console** is the layer below that, and
works on a box that isn't networked at all.

---

## What actually changed, moving off the Pi

| | Pi | EC2 |
|---|---|---|
| **Cost** | invisible (electricity) | ~$18/month, itemised, forever |
| **Storage** | SD/SSD, wear-levelling stalls | gp3, 3,000 IOPS baseline, snapshottable |
| **Clock** | NTP-dependent, RTC needs a battery you didn't buy | Amazon Time Sync on link-local, chrony preconfigured |
| **DNS at boot** | races the FiOS gateway | VPC resolver is up before the instance is |
| **Network flapping** | Wi-Fi power save, undervoltage, DHCP renewal | not a category that exists |
| **Backups** | none | EBS snapshots + nightly dumps to S3 |
| **Secrets** | `.env` on disk | SSM, fetched at start, never written down |
| **Exposure** | behind your router | public IP, scanned constantly |
| **Physical access** | walk over with a keyboard | Session Manager, then serial console, then nothing |

The last two are the trade. You give up the ability to fix anything with a
monitor and a keyboard, and you take on a box that hostile traffic reaches. Both
are managed by things you set up in step 1 rather than things you do later, which
is why that step comes first.

### Roughly what it costs

us-east-1, on-demand, running 24/7 — **check current pricing, these move**:

| | |
|---|---|
| `t4g.small` | ~$12.30/mo |
| 20 GiB gp3 | ~$1.60/mo |
| one public IPv4 | ~$3.60/mo |
| snapshots + S3 | ~$1/mo |
| SSM Parameter Store (standard) | free |
| Session Manager | free |
| **≈** | **$18–19/mo** |

If this is permanent, a one-year Compute Savings Plan takes roughly a third off
the instance line, which is the only line big enough to bother with. Don't buy
one until the box has been up a month and you're sure `t4g.small` is the right
size.

The bill is also the monitoring you never had on the Pi: a cost anomaly is
usually something running that shouldn't be. Set a Budgets alert at $30 and
forget about it.
