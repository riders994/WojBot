# Getting started

> Moving an existing install to a host of its own? See
> [deploying-to-ec2.md](deploying-to-ec2.md) for EC2, or
> [deploying-to-a-pi.md](deploying-to-a-pi.md) for a Raspberry Pi.

How to stand WojBot up and bring a league onto it. If you run more than one
league, each has its own Discord server and you do the [per-server
setup](#2-set-up-each-league-server) once in each.

The short version, run in the league's server:

```
/setup wizard          → roles, league, channels, dad jokes
/commish db migrate    → this server now owns the league in the database
/commish db managers   → see who's in it
/commish db linkuser   → attach each Discord user to their manager
/setup show            → what's still outstanding
```

---

## 0. Before you start

You need three things on the machine running the bot:

| | |
|---|---|
| **A bot token** | Discord Developer Portal → your application → Bot → Token. |
| **The Message Content intent** | Same page, under *Privileged Gateway Intents*. The dad-joke listener reads message text and the bot won't start without it. |
| **A database** | Postgres, with the `fantasy_sports` schema built from the [`leagueSQL`](https://github.com/riders994) repo. Without one the bot still runs, but `/rumor` and everything under `/commish db` won't. |

Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`. `.env` is gitignored.

The Elo configs in `resources/configs/` are gitignored too, because the bot
writes to them as it runs. Only templates are tracked. Copy each one to its
real name and fill it in:

| Template | Copy to | What it holds |
|---|---|---|
| `sys_config.example.yml` | `sys_config.yml` | which leagues exist, and where ratings are stored |
| `csv_config.example.yml` | `csv_config.yml` | where the ratings CSVs go |
| `sql_config.example.yml` | `sql_config.yml` | the Elo engine's warehouse connection (holds a password) |
| `ratings.example.yml` | `ratings_<league>.yml`, one per league | the league's platform, type and season ids |

Per-server settings (`servers/`) and `bot_config.yaml` need no template: the
bot creates them the first time a setting is changed.

For the database, give the connection **either** as a whole string in
`SQL_CONN_URI`, **or** as the discrete `SQL_HOST` / `SQL_PORT` / `SQL_USER` /
`SQL_PASSWORD` / `SQL_DBNAME` fields. `SQL_CONN_URI` is checked first and wins
while it's set, so comment it out to use the fields.

Prefer the fields unless you need something only a URI can carry (`sslmode`, a
connect timeout, a socket path) — the password is escaped for you, so it can
contain `:` `/` `@` `?` or `#` without you percent-encoding it by hand. Setting
some of `SQL_HOST`/`SQL_USER`/`SQL_DBNAME` but not all is an error rather than a
silent fallback, so a typo can't quietly leave you with no database.

Leave the whole section blank to run without one.

If you're actively developing, also set `DISCORD_GUILD_ID` to one server —
commands sync globally, which reaches everywhere but takes about an hour to
propagate, and this makes them appear in that one server immediately. Leave it
blank otherwise: the guild copy is a *second* registration, not a replacement,
so that server can end up listing every command twice.

Then:

```bash
pip install -e .          # development: resolves the ranges in pyproject.toml
wojbot
```

For a deployment you want the pinned set instead, so the machine runs what was
actually tested:

```bash
pip install -r requirements.lock
pip install -e . --no-deps
```

Regenerate the lock with `uv pip compile pyproject.toml -o requirements.lock`
after changing a dependency, and commit it.

## 1. Invite the bot

Build an invite URL in the Developer Portal (OAuth2 → URL Generator) with the
`bot` and `applications.commands` scopes. It needs **Send Messages** and **Embed
Links** in whichever channel rumors will post to.

Invite it to each league's server.

> **Global commands take up to an hour to show up the first time.** If `/setup`
> isn't there yet, that's why — the bot is running fine.

## 2. Set up each league server

Do all of this **in the league's own Discord server**, not in a DM.

Only **server administrators** and the **bot owner** can run setup commands on a
fresh server — role-based access doesn't exist until you configure it in step
one. If you own the bot, you always pass, everywhere.

### 2a. `/setup wizard`

Walks a few steps on one message:

1. **Privileged roles** — which roles get the Admin and Verified tiers. The
   wizard covers the two granting tiers only; Restricted is managed from
   `/verify`. See [Permissions](#permissions).
2. **League** — which configured league this server drives. The options come
   from `resources/configs/sys_config.yml`. Picking one loads it, exactly as
   `/commish load` would.
3. **Default channel** — where the bot's own output goes: the things that
   aren't the rumor wire. Asked of every server, league or not. A quiet,
   muted channel is usually the right answer — somebody pulling a report for
   the league shouldn't have to ping everyone to read it.
4. **Rumor channel** — where `/rumor report` announces to. **League servers
   only:** the step is there if the server is already bound to a league, or as
   soon as step two binds it. A server that runs no league has no rumors to
   post, and the same goes for `/setup rumorchannel`, which declines.
5. **Dad jokes** — on, off, or follow the bot-wide default.

Every step is optional and you can re-run the wizard, or set any one of them
later with `/verify add`, `/commish load`, `/setup defaultchannel`,
`/setup rumorchannel`, and `/setup dadjokes`.

**The default and rumor channels are separate settings and can be separate
channels.** A wire people open to read gossip is not necessarily where a notice
or a report belongs, and a server may want either without the other.

### 2a-i-b. Elo reports for a dynasty league

```
/setup eloreport ratings:Seasonal  → /elo run shows this season's ratings
```

A dynasty league keeps two sets of ratings: each season on its own, and the
all-time timeline that carries ratings from one season into the next.
`/setup eloreport` picks which one `/elo run` reports: **Seasonal** (the
default), **Dynasty**, or **Both**. Both sets are rated and saved either way,
so the choice is only what gets shown. A league that isn't a dynasty only has
seasonal ratings, and this setting has no effect on it.

### 2a-ii. Restart notices

```
/setup restartnotices state:On    → this server hears when the bot comes back
```

Off until a server asks, and deliberately not in the wizard: it's not part of
getting a league running, and it's the one setting here that makes the bot
speak unprompted. Subscribed, the server gets one line in its **default
channel** each time the bot comes back — both the restarts it was asked for and
the ones it wasn't, drawn from the same lists the owner's DM uses
(`resources/restart_messages.json`).

The two settings are independent, so turning notices on without a default
channel subscribes you to nothing; `/setup restartnotices` says so at the time,
and `/setup show` keeps saying it.

### 2b. `/commish db migrate`

Adopts the league's existing database row for this server. Run it **after** the
wizard, because it needs the league loaded first.

This binds the server to the league and prints the roster. It deliberately does
**not** touch managers: a manager's identity in the warehouse is their
Fantrax/Sleeper account, and nothing can match that to a Discord user
automatically. That's the next step, one person at a time.

*(If the league isn't in the database at all yet, use `/commish db leagues` to
see what is, and `/commish sync` to populate one from the platform.)*

### 2c. Link the managers

```
/commish db managers                          → list, with platform handles
/commish db linkuser manager_id:<id> user:@them
```

`/commish db managers` takes a `search` if the list is long. If somebody has no
manager row at all, `/commish db adduser` creates one.

**Until a user is linked, `/rumor report` will refuse them** — the bot won't
know whose front office they'd be speaking for. This is the step people forget.

Real Discord IDs never reach the database. `link`, `linkuser` and `adduser`
store an opaque surrogate and keep the real value bot-side only.

### 2d. Check your work

```
/setup show
```

Lists this server's settings and everything still outstanding, each with the
command that fixes it. Bar one check — whether this server runs a league, which
can ask the database — it's all config and file reads, so it still works with
the database down.

## 3. Using it

### Rumors

```
/rumor report          → the wizard
/rumor recent          → the last 5
/rumor recent count:50 → more; the season is the ceiling
```

`/rumor report` works **in the server or in a DM** — filing one unobserved is
half the point. In a DM the bot works out the league from who you are; if you
play in more than one, it asks which.

The wizard asks for three things, in this order:

- **Source type** — who's leaking it. `{team}` and `{manager}` are filled in from
  your team and handle for the current season.
- **Release type** — how loudly it's being said, from `rumor` up to
  `league release`.
- **Form** — the sentence it goes in. Right now that's the free form.

Then you type the rumor, review it, and post it. It's recorded and announced to
the rumor channel — **without naming you**. The source type is the attribution.

**The more senior the leaker, the heavier a release they can carry.** Pick your
GM or President of Basketball Ops and a `statement` is on the table; the
Commissioner — limited to the Verified tier — is the only one who can put out a
`league release`. `anonymous sources` carry a plain `rumor` and nothing louder.
Once you've picked the leaker, you're only offered the releases they can
actually get out, so you can't walk into a dead end.

### Reminders

```
/remind me      in 90m                    text:set your lineup
/remind channel weekly on monday at 9am   text:lineups lock tonight
/remind server  friday 8pm                text:trade deadline  mentions:@Managers
/remind list                              → what's set
/remind cancel  reminder:12                 (also pause / resume)
/remind help                              → every phrase it accepts
```

Three scopes, and what separates them is **who gets pinged**:

| Command | Goes to | Can tag | Who can set it |
|---|---|---|---|
| `/remind me` | your DM | — | anyone, anywhere including a DM |
| `/remind channel` | a channel | you | anyone, but only a channel **you** can post in |
| `/remind server` | a channel | other people and roles | **Verified tier** |

`/remind server` is the privileged one for one reason: it is the only scope that
makes somebody else's phone buzz. Type `@` in `mentions` and pick from the list
so Discord fills in the mention — anything that isn't a real mention is refused
rather than quietly dropped, and `@everyone` is refused outright.

Two commands are checked twice on purpose. `/remind channel` won't point at a
channel you can't post in yourself — otherwise it would be a way to post
somewhere you have no access to — and the destination is **re-checked when the
reminder fires**, so somebody who has since lost access to a channel doesn't
keep a standing post in it.

#### Saying when

Two kinds of repeat, deliberately two different words:

- **`every` takes a number** — `every 3d`, `every 12h`. This is *elapsed time*,
  so `every 24h` shifts by an hour when the clocks change.
- **Calendar words hold their wall-clock time** — `daily at 9am`,
  `weekly on monday at 9am`, `weekdays at 8:30am`, `weekends at 10am`,
  `monthly on the 1st at 9am`, `hourly`. A 9am reminder is 9am on both sides of
  a daylight-saving change.

One-offs: `in 90m`, `in 2h30m`, `9am`, `noon`, `tomorrow 8am`, `friday 8pm`,
`sep 1 at 9am`, `2026-09-01 14:00`. Or `cron 0 9 * * MON` if you would rather
write it out. Anything it can't read is refused with the list of what works, and
`every monday at 9am` tells you to write `weekly on monday at 9am` instead.

Wall-clock times mean whatever the server's timezone says:

```
/setup timezone zone:America/New_York
```

UTC until somebody sets it, and `/setup show` prints it along with the current
local time so it can be checked at a glance. One reminder can override it with
`timezone:`.

Reminders live in the database, so they survive a restart. A firing missed while
the bot was down still goes out when it comes back, unless it is more than a day
stale — a week-old lineup deadline is noise, and the next one is still wanted. A
recurring reminder that has been failing says so in `/remind list` rather than
just going quiet.

### Elo

Open to anyone:

```
/elo standings   → current standings
/elo team        → one member's rating
```

Verified: `/elo maxpf` — **max points for**, on demand. With no `week:` it is the
season to date; with one it is that matchup period. It is the same report the
weekly `maxpf` process posts (see below), so a league reads one set of numbers
whoever asked for them. It takes a few minutes — longer the first time on a host,
while the box scores are fetched — so it answers privately, keeps working, and
posts the finished report into the channel. One at a time per server.

Commissioner-only: `/elo run` recomputes a week or a range, `/commish sync`
scrapes members and teams, `/commish publish` runs and publishes ratings,
`/commish season …` manages league-years, `/commish dump` backs up the
config files, and `/commish dynasty` shows or changes whether the league rates
its seasons as one continuous dynasty. Dynasty is a setting of the league, not
the server, so it appears there rather than in `/setup show`.

### Weekly league processes

The jobs the league runs by itself, once a week, reporting into a channel. There
are two, and the machinery around them is there so the next one is a handler and
a name:

- **`elo`** — rates every week the platform has scored since the last run, and
  publishes the ratings.
- **`maxpf`** — the week's **max points for**: what every team actually scored
  against what its best lineup could have (`M1`), how much it left on the table
  (`Pot`), and how much the opponent's bench handed it (`Δ`). `/elo maxpf` is the
  same report on demand, for the season to date or a week you missed. It is the
  [`max-pf`](https://github.com/riders994/basketball_max_pf) package, run over
  the last matchup period that finished, with realized box scores rather than
  projections. **Nine-category basketball on Fantrax only** — that is what the
  metric reads — so a football league isn't offered it, and `/weekly list` says
  why rather than hiding it. The Fantrax league id comes from the season's
  `league_id` in the Elo config, so there is nothing extra to configure.

```
/weekly list                              → what runs, when, and how it went
/weekly add process:elo channel:#general  → add it (switched off)
/weekly enable process:elo                → let it run on its schedule
/weekly disable process:elo               → stop it running
/weekly remove process:elo                → take it off the list
/weekly run process:elo                   → run it now, off the schedule
```

All of it is Admin-tier bar `/weekly list`.

**A process is added switched off.** Adding it says the league has one; enabling
it says it may scrape a platform and publish ratings unattended from now on,
which is worth deciding on purpose rather than by reading the command list.

**Basketball servers run Monday at 5am; football servers Tuesday at 5am**, in the
server's `/setup timezone`. That is the whole reason the day is a setting at all:
platforms keep correcting stats after the games finish, and football's are not
settled until the Monday night game has been scored. The sport comes from the
loaded Elo league's `league_type`, so a server that has run `/commish load` never
has to be told; `day:` and `time:` override it. The schedule is a calendar time,
so 5am stays 5am across a clock change.

**Once `elo` is on, it owns the ratings.** `/elo run` stops calculating and
reports the schedule instead — when it last ran, when it next runs, and why the
last run failed if it did. `/weekly run process:elo` is the way to force one, and
it deliberately does *not* move the weekly run. `/weekly disable process:elo`
hands the job back to `/elo run`. `/commish publish`, which rebuilds a whole
season rather than rating forward, still works either way.

A run that finds nothing new is not a failure and posts nothing — no newly
scored week for `elo`, no finished matchup period for `maxpf`. A run that fails
records why, and `/weekly list` shows it.

`maxpf` reports the last matchup period that has *finished*, read off the
league's own schedule, so an all-star break or a short week doesn't throw it off.
It skips playoff periods (the metric is the regular season) and goes quiet
between seasons rather than reposting March every Monday. Expect it to take a few
minutes: it reads the league, then every relevant box score from
basketball-reference, which it caches under `.cache/bref/`.

## Permissions

Four tiers, and they are a **ladder** — each one passes everything below it.
**The bot owner passes every check, everywhere. Server administrators pass every
role tier in their own server, and cannot be restricted.**

| Tier | Configured as | Gates |
|---|---|---|
| **Bot owner** | not configurable | `/sql`, `/scheduler`, `/setup global` |
| **Admin** | `admin_roles`, default `Commish` | `/commish load\|sync\|publish\|dump\|dynasty`, all of `/commish db`, `/commish season add\|platform\|current`, `/weekly add\|remove\|enable\|disable\|run`, `/verify add\|remove` |
| **Verified** | `verified_roles`, default `mods`, `Champion` | `/setup`, `/commish leagues\|status`, `/commish season list`, `/weekly list`, `/elo maxpf`, `/remind server`, and the Commissioner source in `/rumor` |
| **Normal** | not a role list | everything else — the default |
| **Restricted** | `restricted_roles`, empty by default | *denied* every command bar `/help` and `/verify show` |

**Normal isn't configurable, and can't be.** It's simply every command that has
no tier on it — being undecorated is what it means. It only became worth naming
once Restricted existed to sit below it.

**Restricted overrides the ladder rather than sitting under it.** Somebody who
holds both an Admin role and a restricted one is shut out; the deny is checked
before the command's own permissions, so it never has to out-argue a grant. It's
enforced once for the whole command tree rather than per command, which is why a
command added later is covered without anyone marking it.

**A DM isn't a way round it.** The tree check can only read the server a command
was typed in, and `/rumor` — the one group that works in a DM — would otherwise
let somebody shut out of a league report into it from a private window. It asks
the league's own server once it knows which league is meant, for reporting and
for reading alike.

A role belongs in **one** list, the highest that should hold it — Admin already
passes every Verified check, so listing it twice only invites somebody to remove
it from one and assume the other still covers it.

Roles are matched **by name** and exactly, per server — so renaming a role in
Discord silently unconfigures it. `/verify show` tells you where you stand, and
says *why* for each tier.

Manage them with `/verify add tier:… role:@Role` and `/verify remove`. **Only a
Discord server administrator can change the Admin list** — a tier that can hand
itself out isn't a tier. An Admin can edit Verified and Restricted.

Three things about this that catch people out. **Bot owners and Discord server
administrators pass without the configured roles being read at all**, so in a
server whose testers all hold Administrator, a role list matching nothing still
looks like it works — test with somebody who holds neither. **An Admin passes
Verified checks by implication**, so removing a role from `verified_roles` may
change nothing for the people who also hold Admin; `/verify show` says when that
is what happened. And **a server that stores its own list stops following the
defaults**: these keys have no inherit path, so a name added to the built-in
defaults later never reaches a server that has already used `/verify add`.

### Upgrading from the two-tier setup

The old `commissioner_roles` became `admin_roles`, and the old `admin_roles`
(the *lower* of the two tiers) became `verified_roles`. Stored server files are
migrated automatically on the first start, and stamped with a `_schema: 2`
marker so it happens exactly once. Nothing to run by hand — but the rename means
a v1 file's `admin_roles` is deliberately read as *Verified*, since that is what
it granted before.

## Troubleshooting

**"You aren't linked to a manager yet"** — nobody ran `/commish db linkuser` for
that user. See [2c](#2c-link-the-managers).

**A reminder didn't arrive** — `/remind list` first: a failed firing records why
against the reminder, so a dead channel or a lost permission shows up there.
`/scheduler status` (bot owner) says whether the clock is running and when it
last ticked; `/scheduler tick` forces a pass without waiting. A reminder set for
a time that has already passed is refused when you set it, not silently dropped.

**A reminder fired at the wrong time** — check `/setup show`, which prints the
server's timezone and the current local time there. Times are read in that zone,
UTC until `/setup timezone` sets one. If it drifted by exactly an hour after a
clock change, it was set with `every 24h` (elapsed time) where it wanted
`daily at …` (a calendar time).

**The weekly run didn't happen** — `/weekly list` first: it says whether the
process is on, when it next runs, when it last ran, and why the last run failed
if it did. A run that found no newly scored weeks posts nothing on purpose.
`/scheduler status` (bot owner) says whether the clock behind it is running.
`/weekly run process:elo` runs it there and then without touching the schedule.

**"You don't have a team in any league this season"** — they're linked, but no
team in the league's current season is theirs. Only the current season counts:
somebody who left after last year can't report, and a league they've left is no
longer one of the options when they report from a DM. Run `/commish sync` if the
season's rosters were never scraped.

**"This server isn't linked to a league"** — run `/commish db migrate` (or
`/commish db link` if the league is already adopted elsewhere).

**Rumors save but never appear** — no rumor channel, or the bot can't post in
it. `/setup rumorchannel` re-points it and warns you up front if permissions are
missing. Rumors filed meanwhile are still recorded.

**The bot came back and said nothing** — restart notices are per-server and off
by default: `/setup restartnotices state:On`, and `/setup defaultchannel` for
somewhere to put them. `/setup show` lists both. If they're on and set, check the
bot can post in that channel; the log says so when it can't.

**No rumor step in the wizard, or `/setup rumorchannel` declines** — the bot
doesn't think this server runs a league. Bind one with `/commish load` (or the
wizard's league step) and adopt its database row with `/commish db migrate`;
either binding is enough for the rumor settings to come back.

**`/elo` shows another league's ratings, but rumors work** — the server's two
league bindings disagree. Rumors go by the database link (`/commish db
migrate`); Elo goes by the server's `elo_league`. Run `/commish load` with no
league named: it loads the league the database links here and corrects
`elo_league`. Naming a different league than the link is refused — moving the
server is `/commish db migrate`'s job.

**"The database is down"** — a bot owner can run `/sql reconnect`. `/sql status`
shows the connection and how many queries are registered.

**A command is missing** — global commands take up to an hour on first sync.

**You edited a file in `sql/queries/`** — the registry skips files it has
already logged, so delete `sql/manager.log.json` and `sql/queries/_queries.json`
and restart to pick the change up. A bot owner can restart from Discord with
`/restart`, wherever the bot runs under systemd.
