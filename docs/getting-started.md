# Getting started

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

Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN` and `SQL_CONN_URI`.
`.env` is gitignored.

If you're actively developing, also set `DISCORD_GUILD_ID` to one server —
commands sync globally, which reaches everywhere but takes about an hour to
propagate, and this makes them appear in that one server immediately. Leave it
blank otherwise: the guild copy is a *second* registration, not a replacement,
so that server can end up listing every command twice.

Then:

```bash
pip install -e .
wojbot
```

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

Walks four steps on one message:

1. **Privileged roles** — which roles get the Commissioner and Admin tiers. See
   [Permissions](#permissions).
2. **League** — which configured league this server drives. The options come
   from `resources/configs/sys_config.yml`. Picking one loads it, exactly as
   `/commish load` would.
3. **Rumor channel** — where `/rumor report` announces to.
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
command that fixes it. It's all config and file reads, so it works even with the
database down.

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

The wizard asks for three things:

- **Release type** — how loudly it's being said, from `rumor` up to
  `league release`.
- **Source type** — who's saying it. `{team}` and `{manager}` are filled in from
  your team and handle for the current season.
- **Form** — the sentence it goes in. Right now that's the free form.

Then you type the rumor, review it, and post it. It's recorded and announced to
the rumor channel — **without naming you**. The source type is the attribution.

**The heavier the release, the more senior a source it takes.** A `statement`
has to come from your GM or President of Basketball Ops; a `league release` only
from the Commissioner, which is limited to the Admin tier. A plain `rumor` will
take anybody, down to `anonymous sources`. Release types you have no source for
aren't offered at all, so you can't walk into a dead end.

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

Three tiers. **The bot owner passes every check, everywhere. Server
administrators pass both role tiers in their own server.**

| Tier | Configured as | Gates |
|---|---|---|
| **Bot owner** | not configurable | `/sql`, `/setup global` |
| **Commissioner** | `commissioner_roles`, default `Commish` | `/commish load\|sync\|publish\|dump`, all of `/commish db`, `/commish season add\|platform\|current` |
| **Admin** | `admin_roles`, default `mods`, `Champion`, `Commish` | `/setup`, `/verify add\|remove`, `/commish leagues\|status`, `/commish season list`, and the Commissioner source in `/rumor` |

Roles are matched **by name**, per server — so renaming a role in Discord
silently unconfigures it. `/verify show` tells you where you stand and flags
configured names that don't match any live role.

Manage them with `/verify add tier:… role:@Role` and `/verify remove`.

## Troubleshooting

**"You aren't linked to a manager yet"** — nobody ran `/commish db linkuser` for
that user. See [2c](#2c-link-the-managers).

**"This server isn't linked to a league"** — run `/commish db migrate` (or
`/commish db link` if the league is already adopted elsewhere).

**Rumors save but never appear** — no rumor channel, or the bot can't post in
it. `/setup rumorchannel` re-points it and warns you up front if permissions are
missing. Rumors filed meanwhile are still recorded.

**"The database is down"** — a bot owner can run `/sql reconnect`. `/sql status`
shows the connection and how many queries are registered.

**A command is missing** — global commands take up to an hour on first sync.

**You edited a file in `sql/queries/`** — the registry skips files it has
already logged, so delete `sql/manager.log.json` and `sql/queries/_queries.json`
and restart to pick the change up.
