# Getting started

> Moving an existing install to a host of its own? See
> [deploying-to-ec2.md](deploying-to-ec2.md) for EC2, or
> [deploying-to-a-pi.md](deploying-to-a-pi.md) for a Raspberry Pi.

How to stand WojBot up and bring a league onto it. If you run more than one
league, each has its own Discord server and you do the [per-server
setup](#2-set-up-each-league-server) once in each.

The short version, run in the league's server:

```
/setup wizard          → roles, league, rumor channel
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
3. **Rumor channel** — where `/rumor report` announces to. **League servers
   only:** the step is there if the server is already bound to a league, or as
   soon as step two binds it. A server that runs no league has no rumors to
   post, and the same goes for `/setup rumorchannel`, which declines.
4. **Dad jokes** — on, off, or follow the bot-wide default.

Every step is optional and you can re-run the wizard, or set any one of them
later with `/verify add`, `/commish load`, `/setup rumorchannel`, and
`/setup dadjokes`.

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

### Elo

Open to anyone:

```
/elo standings   → current standings
/elo team        → one member's rating
```

Commissioner-only: `/elo run` recomputes a week or a range, `/commish sync`
scrapes members and teams, `/commish publish` runs and publishes ratings,
`/commish season …` manages league-years, and `/commish dump` backs up the
config files.

## Permissions

Four tiers, and they are a **ladder** — each one passes everything below it.
**The bot owner passes every check, everywhere. Server administrators pass every
role tier in their own server, and cannot be restricted.**

| Tier | Configured as | Gates |
|---|---|---|
| **Bot owner** | not configurable | `/sql`, `/setup global` |
| **Admin** | `admin_roles`, default `Commish` | `/commish load\|sync\|publish\|dump`, all of `/commish db`, `/commish season add\|platform\|current`, `/verify add\|remove` |
| **Verified** | `verified_roles`, default `mods`, `Champion` | `/setup`, `/commish leagues\|status`, `/commish season list`, and the Commissioner source in `/rumor` |
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

**No rumor step in the wizard, or `/setup rumorchannel` declines** — the bot
doesn't think this server runs a league. Bind one with `/commish load` (or the
wizard's league step) and adopt its database row with `/commish db migrate`;
either binding is enough for the rumor settings to come back.

**"The database is down"** — a bot owner can run `/sql reconnect`. `/sql status`
shows the connection and how many queries are registered.

**A command is missing** — global commands take up to an hour on first sync.

**You edited a file in `sql/queries/`** — the registry skips files it has
already logged, so delete `sql/manager.log.json` and `sql/queries/_queries.json`
and restart to pick the change up. A bot owner can restart from Discord with
`/restart`, wherever the bot runs under systemd.
