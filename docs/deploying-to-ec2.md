# Moving to EC2

Notes for lifting the bot and the warehouse off the Pi and onto a single EC2
instance. Written against the current setup: bot and Postgres 17 both on
`wojingtonpost`, a Raspberry Pi running Debian trixie on aarch64, with the
original Pi `thegoldenunasinn` still powered up and still serving an un-retired
`wojbot_db`.

**The EC2 instance already exists and already has a PostgreSQL cluster running
on it.** None of the WojBot databases are on it — no `wojbot_db`, no
`fantasy_sports` schema, none of the bot's roles. So this guide does *not*
install Postgres and does *not* assume the cluster is empty, new, or the version
the Pi runs. Step 3 is a read-only look at what's actually there, and step 5's
restore is written to go into a cluster that may already hold other things.
Nothing in this guide drops, recreates or "resets" that cluster.

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

(The instance is already launched, so steps 1–2 are written as a **checklist
against what exists** rather than as a build. Anything that's already right, skip.)

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
| **[ WORKSTATION ]** | the x86_64 desktop | where this repo lives, at `~/activity/WojBot`. Runs no bot any more. | you're sitting at it |
| **[ PI ]** | `wojingtonpost` | 192.168.50.60 — trixie, aarch64, Postgres 17, Python 3.13. **Runs the bot today.** Checkout at `~/activity/WojBot`. | `ssh wojingtonpost` |
| **[ OLD PI ]** | `thegoldenunasinn` | 192.168.1.165 — Postgres 13.23, bullseye, 32-bit. Still up, still serving a stale `wojbot_db`. | `ssh oldpi` (port 50314) |
| **[ EC2 ]** | `pressbox` | the existing instance — already running a Postgres cluster, and ends up running the bot too. Checkout at `~/activity/WojBot`. | `ssh pressbox`, once step 2 sets it up |

`pressbox` is a suggestion, not a requirement — but like `wojingtonpost` in the
Pi guide it is written out in full throughout rather than left as a placeholder,
so the commands can be run as-is. If you want a different name, change it in
`~/.ssh/config`, in the `hostnamectl` call in step 3, and nowhere else.

### No account names are assumed

The OS account on each box is whatever that box has: the Pi's login, the
workstation's login, and the one you SSH into EC2 as. They are not the same, and
**this guide never types any of them.** Paths are written `~/…` or `$HOME/…`, and
the two places that genuinely need an absolute path and a user name (the systemd
unit and the two helper scripts in step 8) are generated from shell variables
that you set from the live box, not copied from the page.

Set these on **[ EC2 ]** once at the start of every session that runs steps 6–9,
and check the output looks right before using it:

```bash
APP_USER=$(id -un)
APP_DIR=$HOME/activity/WojBot
echo "user=$APP_USER  dir=$APP_DIR"
```

`APP_USER` is whoever you logged in as and will own the checkout and run the
service. That's deliberately not root. `postgres` is the one fixed name — it is
the OS account the Postgres packages create, so `sudo -u postgres` is correct on
any Debian-family box.

Postgres **roles** are a separate namespace from OS accounts and are also not
guessed: the bot's login role and the table-owner role are read off the Pi in
step 5 and held in `BOT_ROLE` and `OWNER_ROLE`. `hostname` settles which box
you're on; `pwd` settles which checkout.

## The move at a glance

| step | runs on | what happens |
|---|---|---|
| 1 | AWS | IAM role, key pair, security group — check each exists and is attached |
| 2 | AWS | the instance itself: type, storage, address, ssh alias, Session Manager |
| 3 | EC2 | hostname, swap, locale, **inspect the existing Postgres cluster** (read-only) |
| 4 | WORKSTATION | put the secrets into SSM Parameter Store |
| 5 | PI → EC2 | dump, ship, restore into the existing cluster, verify |
| 6 | EC2 | clone the repo, build the venv from the lock |
| 7 | PI → EC2 | rsync the gitignored state — **from the Pi, not the workstation** |
| 8 | EC2 | the SSM wrapper and the systemd unit |
| 9 | AWS + EC2 | backups, which the Pi never had |
| 10 | everywhere | cut over and prove the writes land on EC2 |
| 11 | everywhere | close every route to every old warehouse |

Steps 1–9 are all reversible and none of them touch the running bot. The one
exception to "reversible by deleting what you added" is that step 5 adds to a
cluster that was already there, so it is written to refuse rather than overwrite. Step 10 is
the only one that changes what's live.

---

## Sizing, and why arm64

This section is for judging the instance you already have, and for the record of
why it was picked. `uname -m` on the box settles the architecture; the instance
type is in the console.

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

## 1. The things the instance depends on

Check each of these against what's in the account. For anything already
present, compare it to what's written here rather than assuming it matches.

### The IAM role

Do this first. The role is what makes the instance reachable when SSH isn't.

Find out whether the instance has a profile attached already:

**[ WORKSTATION ]**

```bash
aws ec2 describe-instances --instance-ids <instance-id> \
  --query 'Reservations[].Instances[].IamInstanceProfile.Arn' --output text
```

`None` means nothing is attached; attach one with *Actions → Security → Modify IAM
role* (it takes effect without a reboot). If one is attached and it isn't the
role below, either add the two policies to it or swap it — but don't remove a
role something else on this instance may be using without looking at what it
grants first.

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
      "Resource": "arn:aws:ssm:<region>:<account-id>:parameter/wojbot/prod/*"
    },
    {
      "Sid": "DecryptThem",
      "Effect": "Allow",
      "Action": "kms:Decrypt",
      "Resource": "*",
      "Condition": {
        "StringEquals": {"kms:ViaService": "ssm.<region>.amazonaws.com"}
      }
    }
  ]
}
```

`<region>` and `<account-id>` are the instance's own — `aws sts get-caller-identity
--query Account --output text` and the region shown in the console. Use the same
region everywhere below (`AWS_DEFAULT_REGION` in step 8, the bucket in step 9).

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

The instance has a key pair already; you can only SSH in if the private half is
on the workstation. Find the key's name with the instance's *Details* tab, and
keep the file at `~/.ssh/wojbot-ec2.pem`, mode `600`, or wherever it already is
(and use that path in the ssh config below). If the private half is lost, EC2
can't recover it — use Session Manager to add a new public key to the login
user's `~/.ssh/authorized_keys` instead.

Security group — whichever one is attached to the instance. This guide calls it
`wojbot-sg`; if the instance's existing group is shared with other things, add a
rule here rather than creating another, and check what else it already opens:

| direction | port | source/dest | why |
|---|---|---|---|
| in | 22/tcp | **your workstation's public IP, /32** | `rsync`, `scp`, ordinary work |
| in | — | nothing else | in particular **never 5432**; if the existing group already has a 5432 rule, remove it now |
| out | all | 0.0.0.0/0 | Discord's gateway and REST, apt, PyPI, the SSM endpoints |

Get the source address with `curl -s https://checkip.amazonaws.com`. It's a
residential IP so it will move eventually, and when it does the fix is to edit
the rule — not to widen it. Session Manager is what gets you in while you sort
that out, which is the other half of why the role goes on at launch.

**Nothing inbound on 5432, ever.** The bot talks to Postgres over loopback and
nothing else needs to. Because this cluster pre-dates the guide, step 3 checks
what it's actually listening on instead of trusting that it matches the Pi.

## 2. The instance

Already launched. Check it against this list and fix whatever differs; none of it
needs a rebuild.

- **AMI**: a Debian-family image (Ubuntu Server 24.04 LTS, **arm64**, is what the
  rest of this guide's paths assume — `cat /etc/os-release` on the box)
- **Type**: `t4g.small` or larger
- **Key pair**: the one from step 1
- **Security group**: as in step 1
- **Storage**: 20 GiB `gp3` or more, with room to spare — `df -h /` once you're in
- **IAM instance profile**: `WojBotInstanceRole`
- **Advanced → Metadata version**: IMDSv2 required. Leave the hop limit at 1 — that's correct for a bot running
  directly on the host. It would need to be 2 only if the bot ran in a
  container, which would put an extra network hop between it and the metadata
  service.

### Give it a fixed address, and know what it costs

Check whether the instance already has an **Elastic IP** (the console shows
*Elastic IP addresses* in its details). If it doesn't, allocate one and
associate it. This is the DHCP-reservation step
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
    User <login-user>
    IdentityFile ~/.ssh/wojbot-ec2.pem
```

`<login-user>` is whatever account the AMI provisioned, which depends on the image
(Ubuntu's AMIs use `ubuntu`, Amazon Linux uses `ec2-user`, Debian's uses
`admin`). If you don't know it, the console's *Connect → SSH client* tab names it,
or `ssh -i … root@<elastic-ip>` will refuse and print the right one. Nothing
below depends on what it is.

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

### The locale, before the restore

`wojbot_db` was created on the Pi with `LC_COLLATE`/`LC_CTYPE` of `en_GB.UTF-8`,
inherited from the original Raspbian install and carried through the Pi
migration. Step 5 creates the EC2 database with that same locale, and Ubuntu's
cloud image has only `C.UTF-8`, so `createdb` would fail with `invalid locale name`.

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

Postgres looks locales up in the operating system at `CREATE DATABASE` time, so
this only has to exist **before the restore in step 5** — the running cluster
doesn't need a restart for it. (The cluster's own template databases were created
long ago against whatever locale the box had and are not affected.)

### The Postgres cluster that's already here

Postgres is already installed and running on this box. **Do not install or
upgrade it, and do not create or drop clusters.** What this step does is read:
find out which cluster it is, whether the dump from the Pi can go into it, and
what else lives in it that the restore must not disturb.

**[ EC2 ]**

```bash
pg_lsclusters                          # version, name, port, status, data dir
systemctl list-units --type=service 'postgresql*' --no-legend
sudo -u postgres psql -Atc "SHOW server_version; SHOW port; SHOW listen_addresses; SHOW data_directory"
```

Write down three things from that, because the rest of the guide leans on them:

- **`PGVER`** — the major version (`17` if it says `17.x`).
- **`PGPORT`** — usually 5432. If there is more than one cluster, the Debian
  packaging gave 5432 to the first and 5433 to the next, so check which one you
  mean.
- **`PGUNIT`** — the service that runs it: `postgresql@<PGVER>-<name>.service`
  on a Debian/Ubuntu package install, as printed by the second command.

If `pg_lsclusters` isn't found, or the unit isn't of that form, this isn't the
Debian packaging and the paths in this guide (`/etc/postgresql/…`,
`postgresql@…`) won't match. The same ideas apply; `SHOW config_file` and `SHOW
data_directory` give you the real locations, and `systemctl status` the unit.

```bash
PGVER=17 ; PGPORT=5432 ; PGUNIT=postgresql@17-main      # ← fill in from the above
```

#### Is it new enough? If not, upgrade it — to 17, and no further

`pg_dump` is forward-compatible only: a dump taken from PostgreSQL 17 restores
into 17 or later, and into 16 or earlier it is the unsupported direction. It does
not fail at the start; it fails partway through, on whatever the first 17-only
piece of syntax is, with a half-restored database left behind.

So **the minimum version that can take this restore is the Pi's major version,
17**, and that is exactly what you upgrade to — not 18, not "latest". The
version on the Pi is the thing to check, not this page:

**[ PI ]** `sudo -u postgres psql -Atc "SHOW server_version"`

| EC2 `PGVER` is | what to do |
|---|---|
| **17 or later** | skip the rest of this subsection |
| **16 or earlier** | upgrade the existing cluster in place to 17, below |

Ubuntu 24.04's own archive tops out at 16, so 17 comes from the PGDG archive.
This is an upgrade of a cluster other things may be using, so it comes with a
safety net first and an explicit "nothing else is connected" check:

**[ EC2 ]**

```bash
OLDVER=$PGVER                                  # e.g. 16
# 1. nothing else connected (the pg_stat_activity query below), then a safety net:
sudo -u postgres pg_dumpall -p "$PGPORT" > ~/pre-upgrade-$(date +%F).sql
#    and an EBS snapshot of the instance's volume, from the workstation:
#    aws ec2 create-snapshot --volume-id <vol-id> --description "pre pg upgrade"

# 2. PGDG repository
sudo apt install -y curl ca-certificates
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl --fail -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list
sudo apt update

# 3. install 17 WITHOUT letting the package create an empty 17/main cluster;
#    pg_upgradecluster has to create it itself
echo "create_main_cluster = false" | sudo tee /etc/postgresql-common/createcluster.d/no-main.conf
sudo apt install -y postgresql-17
```

Extensions: any extension in the old cluster (`SELECT extname FROM pg_extension`
in each database) needs its `postgresql-17-<name>` package installed *before* the
upgrade, or `pg_upgradecluster` fails on it.

Then the upgrade itself, which dumps the old cluster and loads it into a new 17
one, preserving encodings and locales, and puts **17 on the old port**:

```bash
sudo pg_upgradecluster -v 17 "$OLDVER" main
pg_lsclusters
```

Expect two lines: the new `17 main` online on the port the old one had, and the
old `$OLDVER main` stopped on a different port. Check that every database and role
that was in step 3's listing is present in the new one, update the three values,
and remove the helper config:

```bash
PGVER=17 ; PGUNIT=postgresql@17-main                # PGPORT is unchanged
sudo rm /etc/postgresql-common/createcluster.d/no-main.conf
sudo -u postgres psql -p "$PGPORT" -Atc "SELECT version()"
```

The old cluster is **left in place, stopped**. Leave it there through the
cutover; dropping it (`sudo pg_dropcluster $OLDVER main`) is a separate decision
for a week from now, not part of this migration. This does mean the upgrade
briefly stops the cluster, so anything else using it sees an outage — which is why
the connected-clients check comes first.

If `pg_lsclusters` isn't present, the cluster isn't Debian-packaged and this
procedure doesn't apply; stop and sort out an upgrade path for that layout first.

#### What's in it already?

**[ EC2 ]**

```bash
sudo -u postgres psql -p "$PGPORT" -Atc "SELECT datname, datcollate, pg_encoding_to_char(encoding) FROM pg_database ORDER BY 1"
sudo -u postgres psql -p "$PGPORT" -Atc "SELECT rolname, rolsuper, rolcanlogin FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY 1"
```

You are looking for three things:

1. **No `wojbot_db`** (and nothing called `fantasy_sports` or similar). The
   starting premise is that none of the WojBot databases are here. If one
   *does* exist, find out what it is before going further. An empty or abandoned
   one can be renamed out of the way (`ALTER DATABASE wojbot_db RENAME TO
   wojbot_db_old_$(date +%Y%m%d)`); one with data in it is a surprise worth
   understanding, not overwriting.
2. **Encoding `UTF8`** on `template1`. The restore creates a UTF-8 database, and
   that can't be built from a template in another encoding.
3. **Role names that overlap the ones being restored.** Step 5 compares the two
   lists before it writes anything, but look now: any application on this cluster
   that already uses a role called `moderator`, `manager` or `consumer` (generic
   names) would collide, and the restore stops rather than touching it.

Check listening and authentication, rather than assume they match the Pi:

```bash
sudo -u postgres psql -p "$PGPORT" -Atc "SHOW listen_addresses"
sudo -u postgres psql -p "$PGPORT" -Atc "SELECT type, database, user_name, address, auth_method FROM pg_hba_file_rules ORDER BY line_number"
ss -ltnp | grep -E ":$PGPORT\b"
```

Want `listen_addresses` to be `localhost` (or `127.0.0.1`) and the `ss` line to
show only `127.0.0.1` / `::1`. If it says `*` or `0.0.0.0`, the cluster is
listening on every interface; the security group is currently the only thing
standing between it and the internet. Fix it in step 5's settings section — but
that's a change to an existing cluster, so it needs a restart that interrupts
whatever else uses it. Look first.

Ask whether anything else uses this cluster before step 5's restore and the later
`listen_addresses` change:

```bash
sudo -u postgres psql -p "$PGPORT" -Atc "SELECT datname, usename, application_name, client_addr FROM pg_stat_activity WHERE datname IS NOT NULL"
```

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

Two values here come from the Pi and the EC2 cluster rather than from this page:
the **port** you recorded as `PGPORT` in step 3, and the **bot's login role**.
Read the role off the Pi so you're using what the data was actually built with:

**[ WORKSTATION ]**

```bash
# whatever user the Pi's bot connects as — from its live sql config, not from memory
ssh wojingtonpost "grep -m1 conn_uri activity/WojBot/resources/configs/sql_config.yml" \
  | sed -E 's#.*://([^:@/]+).*#\1#'
```

```bash
BOT_ROLE=<the name that printed>
PGPORT=<from step 3>

put() { aws ssm put-parameter --name "/wojbot/prod/$1" --type SecureString \
          --value "$2" --overwrite >/dev/null && echo "  set $1"; }

put SQL_HOST     127.0.0.1
put SQL_PORT     "$PGPORT"
put SQL_USER     "$BOT_ROLE"
put SQL_DBNAME   wojbot_db
put LOG_LEVEL    INFO
```

(If that `ssh` prints nothing, the Pi's file uses `conn_dict` or a `.env`; read it
from there. Stop and look rather than guessing a name — a wrong `SQL_USER` is a
login failure, and a *different but valid* role is silently the wrong
permissions.)

For the two real secrets, **don't type the value as an argument.** It goes into
your shell history and is visible in `ps` for as long as the call runs. Read
them from the file that already holds them instead:

```bash
put DISCORD_TOKEN "$(ssh wojingtonpost 'grep -m1 ^DISCORD_TOKEN= activity/WojBot/.env' | cut -d= -f2-)"
```

and for the database password, either the same trick against the Pi's `.env` or
`read -rs PW && put SQL_PASSWORD "$PW"`, which at least keeps it out of history.
The same applies to `put` itself: it is a shell function, so the value is
expanded inside this shell and passed to `aws` as an argument. The `ps`
exposure is brief but real on a shared workstation; if that matters, put the
value in a `file://` argument (`--value file:///dev/stdin` with the secret on
stdin) instead.

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

### What gets restored, and what deliberately doesn't

Roles in Postgres are cluster-wide objects — there is no such thing as a role that
exists "only in `wojbot_db`". What can be done is to bring across **only the roles
that `wojbot_db` actually depends on**, leave every other role and every other
database alone, and then make `wojbot_db` the only place those roles are let in.
So this restore does **not** use `pg_dumpall` for the data and does not replay the
Pi's whole role list:

- the roles come from a filtered extract: owners, ACL grantees, the database
  owner, the bot's login role, and the roles those are members of — found from
  `pg_shdepend`, not by name;
- `postgres` is never part of it, so the EC2 `postgres` role isn't touched, and
  nothing is `ALTER`ed for a role that already existed;
- the database itself travels as a `pg_dump -Fc`, restored into a database that
  `createdb` makes first.

The extractor is a small script. Put it at `/usr/local/bin/wojbot-roles` on the
Pi now (and on EC2 in step 9, where the nightly backup reuses it):

```bash
sudo tee /usr/local/bin/wojbot-roles >/dev/null <<'EOF'
#!/usr/bin/env bash
# wojbot-roles [port] — print the DDL for only the roles $WOJBOT_DB depends on.
# Needs BOT_ROLE in the environment; the bot's login may own nothing yet.
set -euo pipefail
DB=${WOJBOT_DB:-wojbot_db}
P=(); [[ -n ${1:-} ]] && P=(-p "$1")

names=$(sudo -u postgres psql "${P[@]}" -Atq -d "$DB" \
          -v db="$DB" -v bot="${BOT_ROLE:?set BOT_ROLE}" <<'SQL'
WITH RECURSIVE seed(oid) AS (
  SELECT refobjid FROM pg_shdepend
   WHERE dbid = (SELECT oid FROM pg_database WHERE datname = :'db')
     AND refclassid = 'pg_authid'::regclass AND deptype IN ('o','a')
  UNION SELECT datdba FROM pg_database WHERE datname = :'db'
  UNION SELECT oid FROM pg_roles WHERE rolname = :'bot'
), closure(oid) AS (
  SELECT oid FROM seed
  UNION SELECT m.roleid FROM pg_auth_members m JOIN closure c ON m.member = c.oid
)
SELECT rolname FROM pg_roles
 WHERE oid IN (SELECT oid FROM closure)
   AND rolname <> 'postgres' AND rolname !~ '^pg_'
 ORDER BY 1;
SQL
)
[[ -n $names ]] || { echo "no roles found for $DB" >&2; exit 1; }

sudo -u postgres pg_dumpall "${P[@]}" --roles-only | awk -v list="$names" -v db="$DB" '
  BEGIN { n = split(list, a, "\n"); for (i = 1; i <= n; i++) keep[a[i]] = 1 }
  function bare(x) { gsub(/"/, "", x); sub(/;$/, "", x); return x }
  /^\\/ || /^--/ || /^$/ { next }
  $1 == "SET" { print; next }
  $1 == "CREATE" && $2 == "ROLE" { if (bare($3) in keep) print; next }
  $1 == "ALTER" && $2 == "ROLE" {
    if (!(bare($3) in keep)) next
    if ($4 == "IN" && $5 == "DATABASE" && bare($6) != db) next
    print; next }
  $1 == "GRANT" && $3 == "TO" { if ((bare($2) in keep) && (bare($4) in keep)) print; next }
  { print "UNRECOGNISED: " $0 > "/dev/stderr" }'
EOF
sudo chmod 755 /usr/local/bin/wojbot-roles
```

Anything it doesn't recognise goes to stderr as `UNRECOGNISED:` instead of being
silently dropped. Role names containing odd characters are the likeliest cause; if
you see one, read it before going on.

### Dump on the Pi

**[ PI ]**

```bash
BOT_ROLE=<the bot's login role, from step 4>
D=$(date +%F)
BOT_ROLE=$BOT_ROLE wojbot-roles > wojbot-roles-$D.sql
sudo -u postgres pg_dump -Fc wojbot_db > wojbot_db-$D.dump
cat wojbot-roles-$D.sql
```

**Read that role file.** It is the whole of what will be created on EC2. Check:

- Every role in it is one you'd expect `wojbot_db` to need. A stranger means
  something unrelated is granted on a table.
- `SUPERUSER` — a role carrying it on the Pi arrives as a superuser on a cluster
  that other things use. Often the table owner is one, because it was created that
  way. Decide deliberately: if nothing needs it, change that role's `SUPERUSER` to
  `NOSUPERUSER` in the file before shipping it.
- Passwords are included (it's a plain `--roles-only` dump with the hash), so
  guard the file like a credential. Without them the bot's role restores as a
  login that can't log in.

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
scp wojingtonpost:'wojbot-roles-*.sql' wojingtonpost:'wojbot_db-*.dump' ~/backups/
ls -l ~/backups/wojbot-roles-*.sql ~/backups/wojbot_db-*.dump
```

Look at the listing, then name the two files explicitly — a glob here would ship
every old dump in `~/backups` too:

```bash
ROLES=~/backups/wojbot-roles-<date>.sql
DUMP=~/backups/wojbot_db-<date>.dump
scp "$ROLES" "$DUMP" pressbox:
```

Keeping a copy on the workstation in passing is deliberate: at that moment the
data exists in three places, which is the only point in this process where
you're properly covered. The roles file holds password hashes, so don't leave it
anywhere shared.

### Restore

The cluster on EC2 already exists and has other things in it, so everything below
is checked **before** it writes.

**[ EC2 ]** — name the files you shipped, then compare against the cluster

```bash
ROLES=$HOME/wojbot-roles-<date>.sql
DUMP=$HOME/wojbot_db-<date>.dump
BOT_ROLE=<from step 4>
OWNER_ROLE=<from the Pi, see "Verify" below>
DB_LOCALE=en_GB.UTF-8          # the Pi's datcollate for wojbot_db (step 3)

grep -oE '^CREATE ROLE [^ ;]+' "$ROLES" | awk '{print $3}' | sort > /tmp/dump-roles
sudo -u postgres psql -p "$PGPORT" -Atc "SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_'" | sort > /tmp/live-roles
echo "roles that already exist here:"; comm -12 /tmp/dump-roles /tmp/live-roles
sudo -u postgres psql -p "$PGPORT" -Atc "SELECT datname FROM pg_database WHERE datname='wojbot_db'"
```

- **The first list must be empty.** `CREATE ROLE` on an existing name fails. If
  something is listed, don't skip it casually: a role already here may have a
  different password, different memberships and other applications' objects, and
  this file's `ALTER ROLE` would rewrite it. Decide per role whether it's
  genuinely the same one (then delete its `CREATE ROLE` and `ALTER ROLE` lines
  from the file, so nothing about it changes), or a name collision, which is a
  reason to stop.
- **The last query must print nothing.** An existing `wojbot_db` is dealt with
  by hand (step 3), not by this restore.

Safety net, since the cluster isn't ours alone:

```bash
sudo -u postgres pg_dumpall -p "$PGPORT" > ~/pre-restore-$(date +%F).sql
```

Then, in order:

```bash
# 1. the roles — only these ones; stdin, because postgres can't read your home
sudo -u postgres psql -p "$PGPORT" -v ON_ERROR_STOP=1 -f - < "$ROLES" 2>&1 | tee ~/restore.log

# 2. an empty database with the Pi's encoding and locale, owned by the right role
sudo -u postgres createdb -p "$PGPORT" --template template0 \
  --encoding UTF8 --locale "$DB_LOCALE" --owner "$OWNER_ROLE" wojbot_db

# 3. the data; ownership and grants come out of the dump
sudo -u postgres pg_restore -p "$PGPORT" --exit-on-error -d wojbot_db < "$DUMP" 2>&1 | tee -a ~/restore.log

# 4. confine it: only the roles that need it may connect to this database
sudo -u postgres psql -p "$PGPORT" -v ON_ERROR_STOP=1 wojbot_db <<SQL
REVOKE CONNECT ON DATABASE wojbot_db FROM PUBLIC;
GRANT  CONNECT ON DATABASE wojbot_db TO "$BOT_ROLE", "$OWNER_ROLE";
SQL
```

Why it is shaped this way:

**Stdin, not a file path.** `psql` and `pg_restore` run as `postgres`; home
directories are commonly `0750`, so `postgres` can't read a file you `scp`'d
there. The redirect is opened by *your* shell, before `sudo` drops privileges.
Don't `chmod o+x` your home to work around it.

**`createdb --template template0` with an explicit locale.** The dump's data is
collation-dependent (indexes, ordering), so the database must be built with the
Pi's collation, not whatever this cluster defaults to; `template0` is the only
template that allows a different one. The locale has to exist in the OS (step 3's
`locale-gen`).

**`--exit-on-error`** is what turns a partial restore into an obvious one;
without it `pg_restore` logs errors and carries on.

**Step 4 is the "only in `wojbot_db`" part.** Roles are cluster-global, so what
limits them is where they may connect and what they own. After step 4, nobody
else can connect to `wojbot_db`, and the roles you created can't touch the
others' objects because they were granted nothing there. One honest caveat: a
database elsewhere on this cluster that still grants `CONNECT` to `PUBLIC` (the
default) will still let these roles *connect* to it, with no object privileges.
Revoking that on other applications' databases is their owner's call, so this
guide doesn't do it.

> **If any step aborts, remove only what it created — never the cluster.** psql
> and `createdb` commit as they go. In reverse order:
>
> ```bash
> sudo -u postgres psql -p "$PGPORT" -c "DROP DATABASE IF EXISTS wojbot_db"
> while read -r r; do sudo -u postgres psql -p "$PGPORT" -c "DROP ROLE IF EXISTS \"$r\""; done < /tmp/dump-roles
> ```
>
> Only run the second line when `comm -12` above printed nothing, otherwise
> `/tmp/dump-roles` includes roles that were here first and this would drop
> them. `DROP ROLE` refuses a role that still owns objects, which is the right
> thing for it to do — database first. Fix the cause and restart at "Restore".

### Verify before trusting it

Set the two role names from the Pi, not from memory:

**[ PI ]**

```bash
sudo -u postgres psql -Atc "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname='fantasy_sports'" wojbot_db
sudo -u postgres psql -Atc "SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY 1"
```

The first gives `OWNER_ROLE` (needed by the restore above); the second is the
Pi's full role list, of which only the roles in the shipped file are expected on
EC2. (`BOT_ROLE` is already known from step 4.)

**[ EC2 ]** — `sudo -u postgres psql -p "$PGPORT" wojbot_db`

- Re-run the count query from above and compare every number.
- `\du` — every role in `$ROLES` is present, and nothing else about the cluster's
  roles changed from the pre-restore dump (`diff` its `CREATE ROLE` lines with
  `~/pre-restore-*.sql`).
- Ownership and grants:
  ```sql
  SELECT tablename, tableowner FROM pg_tables WHERE schemaname='fantasy_sports';
  SELECT grantee, privilege_type FROM information_schema.table_privileges
   WHERE table_schema='fantasy_sports' AND table_name='dim_league';
  ```
  Every `tableowner` is `OWNER_ROLE`, and the grantee list matches what the Pi
  shows for the same query. Also `\l wojbot_db`: the access privileges column
  shows no `=c/` entry (that would be `PUBLIC` still holding `CONNECT`).
- And that the bot's role can actually log in and read. Read the password into a
  variable rather than typing it on the command line:
  ```bash
  read -rs PGPASSWORD && export PGPASSWORD
  psql -h 127.0.0.1 -p "$PGPORT" -U "$BOT_ROLE" wojbot_db -c \
    "SELECT count(*) FROM fantasy_sports.dim_source_type"
  unset PGPASSWORD
  ```

**Password hashes.** The md5 problem in the Pi guide came from a Postgres 13
source; a 17 → 17 restore carries `scram-sha-256` verifiers as they are. Confirm
rather than assume — and this also tells you whether the cluster's
`password_encryption` agrees:

```sql
SELECT rolname, left(rolpassword, 6) FROM pg_authid WHERE rolcanlogin;
SHOW password_encryption;
```

Every restored row `SCRAM-`, except `postgres`, which is blank if it
authenticates by peer. An `md5` row would need its password re-set after the
`SHOW` says `scram-sha-256`.

### Two settings the dump doesn't carry

`pg_hba.conf` and `postgresql.conf` are not in the dump, and on this cluster they
are not defaults either — somebody configured it. What step 3 printed is what's
true; compare it to what the bot needs, which is only a TCP connection on
`127.0.0.1` from the bot's role with a password (`scram-sha-256`).

- **`pg_hba.conf`**: there must be a `host` line matching that
  (`host all all 127.0.0.1/32 scram-sha-256` is the standard one). Find the file
  with `sudo -u postgres psql -Atc "SHOW hba_file"`. A `peer` or `ident` rule for
  that connection, or a `reject` above it, is a login failure that looks like a
  wrong password. A reload (`SELECT pg_reload_conf()`) applies hba changes.
- **`listen_addresses`**: if step 3 showed it already at `localhost`, leave it.
  If it was wider, narrowing it is the change worth making — but *only after*
  step 3's `pg_stat_activity` showed nothing remote using it. `SHOW config_file`
  gives the path:

  ```
  listen_addresses = 'localhost'
  ```

  ```bash
  sudo systemctl restart "$PGUNIT"   # listen_addresses needs a restart, not a reload
  ```

  That restart drops every connection to the cluster, not just the bot's. If other
  things depend on it, schedule it.

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
pwd                     # must print $HOME/activity/WojBot — compare to $APP_DIR
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
the real checkout — no error, no warning. (`echo "$APP_DIR"` and `pwd` should
agree; if you haven't set `APP_DIR` yet, step 2's "No account names are assumed"
has the lines.)

**Install from `requirements.lock`, not from `pyproject.toml`.** The constraints
there are ranges, so a plain `pip install -e .` resolves them against whatever is
newest today, and the whole point is that EC2 runs what the Pi has been running.
`--no-deps` on the second line stops pip resolving the ranges again on top.

Everything comes from public PyPI — `rv-pytools`, `elo-system` and `sleeper` are
all published — so there's no private index to configure. **Don't copy `.venv/`**
from anywhere, including the Pi: it hardcodes absolute paths, and
the Pi's home directory doesn't exist here.

## 7. Copy the state the repo doesn't carry

These are gitignored, so the clone doesn't have them:

```
resources/configs/                      (every live config, servers/ and leagues/ included;
                                         only *.example.* templates are tracked)
resources/anon/                         (the reversal maps)
resources/ratings/                      (per-league Elo CSVs)
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
S=$(mktemp -d) ; P=wojingtonpost:activity/WojBot       # private, unguessable staging dir
mkdir -p "$S/resources"

rsync -av --ignore-missing-args "$P/resources/anon/"     "$S/resources/anon/"
rsync -av --ignore-missing-args "$P/resources/ratings/"  "$S/resources/ratings/"
rsync -av --ignore-missing-args "$P/resources/configs/"  "$S/resources/configs/"

find "$S" -type f                  # look at what is about to land
rsync -av "$S/" pressbox:activity/WojBot/
rm -rf "$S"                        # it held every config and the anonymizer maps
```

Staging locally rather than piping Pi-to-EC2 in one shot is deliberate: it
lets you look at exactly what's about to land before it lands, and the Pi has no
key for EC2 anyway. `mktemp -d` rather than a fixed `/tmp/…` name because the
staged files include live configs and the anonymizer maps, and a predictable
path in a shared directory is somebody else's to read.

`--ignore-missing-args` keeps a pull a no-op rather than an error if `/setup`
never persisted anything — though on the Pi it almost certainly has, and
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
grep -rn 'conn_uri\|SQL_CONN_URI' ~/activity/WojBot/resources/configs/
```

The grep should print nothing outside `docs/`. `resources/configs/sql_config.yml`
already says `127.0.0.1` — the Pi migration set it — which is right for the host,
but **check the port in its `conn_uri` against `PGPORT`**: the Pi's is 5432 and
this cluster might not be. Fix the file if they differ. That file is read by `EloSystem` through `sys_config.yml`'s
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

Then `/usr/local/bin/wojbot-run`. It takes the checkout's location from
`WOJBOT_DIR`, which the unit sets below, so no user name or home directory is
written into it:

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

ROOT = os.environ.get("WOJBOT_DIR")
if not ROOT:
    sys.exit("WOJBOT_DIR is not set — the systemd unit provides it")
BOT = os.path.join(ROOT, ".venv", "bin", "wojbot")
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

Four deliberate choices in a few lines, each of which is a way this goes wrong
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
sudo -u "$APP_USER" env WOJBOT_DIR="$APP_DIR" /usr/local/bin/wojbot-run   # Ctrl-C once you see it connect
```

### The unit

**[ EC2 ]** — `/etc/systemd/system/wojbot.service`

Generated from the variables you set, so that the user and the paths are the
box's own. The heredoc is *unquoted* on purpose — the shell fills in `$APP_USER`,
`$APP_DIR` and `$PGUNIT` as it writes. Set `REGION` to the instance's region
first:

```bash
REGION=<region>
echo "user=$APP_USER dir=$APP_DIR pg=$PGUNIT region=$REGION"      # no blanks allowed

sudo tee /etc/systemd/system/wojbot.service >/dev/null <<EOF
[Unit]
Description=WojBot
After=network-online.target nss-lookup.target time-sync.target $PGUNIT.service
Wants=network-online.target
Requires=$PGUNIT.service

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=WOJBOT_DIR=$APP_DIR
Environment=AWS_DEFAULT_REGION=$REGION
ExecStartPre=-/usr/local/bin/wojbot-update
ExecStart=/usr/local/bin/wojbot-run
TimeoutStartSec=300
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

cat /etc/systemd/system/wojbot.service     # read it: no empty "User=" or "WorkingDirectory="
```

`$PGUNIT.service` is the cluster's own versioned unit, and it must be the
real one — **not** plain `postgresql.service`, which on Debian and
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
create `/wojbot.log` as the service user, and dies with a `PermissionError` traceback
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

Like the wrapper, it takes the checkout's location from `WOJBOT_DIR` (set by the
unit) and has no user or path baked in:

```bash
#!/usr/bin/env bash
# Fast-forward the checkout and reconcile the venv before the bot starts.
#
# Exits 0 unconditionally. Every failure here means "start the code already on
# disk", which is always a better outcome than not starting: an unreachable
# GitHub at boot must not be able to keep the bot down.
set -uo pipefail

REPO=${WOJBOT_DIR:?WOJBOT_DIR is not set — the systemd unit provides it}
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
/usr/local/bin/wojbot-update && sudo systemctl restart wojbot
```

**Not under `sudo`.** `ExecStartPre` runs as the unit's `User=`, which is the
ownership the checkout already has. Run it by hand as that user with the
variable set: `WOJBOT_DIR="$APP_DIR" /usr/local/bin/wojbot-update`. Running the updater as root writes root-owned
objects into `.git/`, and the next unattended run — back as the service user — then fails
on them. Only the restart needs privilege.

or put it on a timer that only restarts when the commit actually moved, which
gets you unattended deploys without coupling them to every crash recovery. The
script is written to be safe in all three modes; only the unit line changes.

#### Deploying from Discord

With `ExecStartPre` wired up, `/restart` is a deploy. The command is owner-only,
asks for a confirmation, and then closes the bot from inside — `Restart=always`
answers the clean exit the same way it answers a crash, which means the updater
runs and the bot comes back on whatever `primary` is at that moment. No SSH, no
sudoers entry for the service account: the bot needs no privilege to exit.

It refuses when `INVOCATION_ID` isn't in its environment — systemd sets that for
every service it runs, so its absence means nothing is watching for the exit and
`/restart` would be a shutdown. Drop `ExecStartPre` and the command still works;
it just restarts the code already on disk.

Either way the bot DMs the owner when it comes back, with a line picked at
random from `resources/restart_messages.json` — from that file's `commanded`
list when `/restart` was what took it down, and from its `disruption` list when
something else did. Editing the file is the whole of changing what it says; it
is read fresh on every start. The two are told apart by a marker the command
leaves in `resources/state/`, which the next start consumes, so a power cut
never reports itself as a deploy.

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
access blocked, in the same region as the instance), add `s3:PutObject` on it to
`WojBotReadSecrets`, and set a
lifecycle rule to move objects to Glacier Instant Retrieval after 30 days and
expire them after a year.

**[ EC2 ]** — `/usr/local/bin/wojbot-backup`

```bash
#!/usr/bin/env bash
set -euo pipefail
BUCKET=s3://wojbot-backups-<account-id>
PGPORT=${PGPORT:-5432}          # set to the port step 3 recorded
STAMP=$(date -u +%Y-%m-%dT%H%M%SZ)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

sudo -u postgres pg_dump -p "$PGPORT" -Fc wojbot_db > "$TMP/wojbot_db-$STAMP.dump"
BOT_ROLE=<the bot's login role> /usr/local/bin/wojbot-roles "$PGPORT" > "$TMP/roles-$STAMP.sql"
aws s3 cp "$TMP/wojbot_db-$STAMP.dump" "$BUCKET/daily/"
aws s3 cp "$TMP/roles-$STAMP.sql"      "$BUCKET/daily/"
```

Hard-code the port and the role name in the script (replace the `:-5432` and the
`<…>`) rather than relying on an environment the timer won't have. The script
needs `/usr/local/bin/wojbot-roles` from step 5 on this box too — install it
with the same `tee` block, since step 5 only put it on the Pi.

Note the second dump. A `pg_dump` of one database does **not** contain the roles,
and a restore into a cluster that has never heard of the bot's role or the table
owner fails on the first `ALTER TABLE … OWNER TO`. This one uses the same
filtered extract as the migration, so it contains only the roles `wojbot_db`
depends on and not the rest of the cluster's — that keeps other applications'
role names and password hashes out of an S3 bucket they have nothing to do with.

Then a timer, rather than cron, so the failures land in the journal alongside
everything else:

`/etc/systemd/system/wojbot-backup.service`

```ini
[Unit]
Description=WojBot nightly logical backup

[Service]
Type=oneshot
Environment=AWS_DEFAULT_REGION=<region>
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
(`sudo -u postgres createdb -p "$PGPORT" restore_test && sudo -u postgres pg_restore -p "$PGPORT" -d restore_test …`, then `dropdb` it) and run the count
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
   whatever was reported during the migration. On EC2, since the roles are
   already there from step 5, restore the data into a **new** database and swap
   the names, rather than overwriting the live one in place:
   ```bash
   sudo -u postgres createdb -p "$PGPORT" -T template0 -O "$OWNER_ROLE" wojbot_db_new
   sudo -u postgres pg_restore -p "$PGPORT" --no-owner --role="$OWNER_ROLE" -d wojbot_db_new wojbot_db-<date>.dump
   # verify counts, then:
   sudo -u postgres psql -p "$PGPORT" -c "ALTER DATABASE wojbot_db RENAME TO wojbot_db_pre_cutover"
   sudo -u postgres psql -p "$PGPORT" -c "ALTER DATABASE wojbot_db_new RENAME TO wojbot_db"
   ```
   (`RENAME` needs no other connections to the database; the bot is stopped on
   both ends, so there are none.)
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
BOT_ROLE=<the bot's login role, from step 4>
RETIRED=wojbot_db_retired_$(date +%Y_%m)
sudo -u postgres psql -c "ALTER ROLE $BOT_ROLE NOLOGIN"
sudo -u postgres psql -c "ALTER DATABASE wojbot_db RENAME TO $RETIRED"
```

**[ OLD PI ]** — `thegoldenunasinn`, the one that's been open all along:

```bash
BOT_ROLE=<the bot's login role, from step 4>
RETIRED=wojbot_db_retired_$(date +%Y_%m)
sudo -u postgres psql -c "ALTER ROLE $BOT_ROLE NOLOGIN"
sudo -u postgres psql -c "ALTER DATABASE wojbot_db RENAME TO $RETIRED"
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

Rolling back also leaves what step 5 added to the shared cluster: `wojbot_db` and
the bot's roles. They're harmless idle, and the safest thing is to leave them for
the retry. If you do want them gone, use the `DROP DATABASE` / `DROP ROLE` list
in step 5's abort note — database first — and nothing broader.

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
three being wrong. From there, `sudo su - <login-user>` and carry on (`ls /home` lists the candidates).

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
