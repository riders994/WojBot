# WojBot

A Discord bot for a fantasy basketball league. Slash commands only.

- **Rumors** — managers file league rumors through a wizard, from the server or
  from a DM, and the bot posts them to a channel without naming the reporter.
- **Elo** — standings and ratings, backed by the
  [`elo-system`](https://github.com/riders994/eloSystem) package.
- **Reminders** — one-off or recurring, delivered to a DM, to a channel, or to a
  channel with other people tagged. The last of those is the privileged one, and
  the only one that can ping anybody but its author.
- **Weekly league processes** — the jobs a league runs by itself once a week and
  reports into a channel: the Elo run, and a
  [`max-pf`](https://github.com/riders994/basketball_max_pf) report of what every
  team scored against what its best lineup could have. Admin-gated, off until
  somebody turns them on, and timed for after the platform's stat corrections
  settle — basketball Monday, football Tuesday.
- **Commissioner tools** — leagues, seasons, and the warehouse dimension tables.
- **Setup** — per-server configuration with a guided wizard, and four permission
  tiers on top of it: Admin, Verified, Normal, and Restricted, each passing
  everything below it.

## Getting started

**[docs/getting-started.md](docs/getting-started.md)** is the full guide:
prerequisites, inviting the bot, setting up a league server, and troubleshooting.

The short version, once the bot is running and invited:

```
/setup wizard          → roles, league, rumor channel
/commish db migrate    → this server now owns the league in the database
/commish db managers   → see who's in it
/commish db linkuser   → attach each Discord user to their manager
/setup show            → what's still outstanding
```

`/help` says the same things inside Discord.

## Running it

```bash
cp .env.example .env    # fill in DISCORD_TOKEN and the database settings
# The Elo configs: copy each template and fill it in (see docs/getting-started.md)
cd resources/configs && for f in sys_config csv_config sql_config; do cp $f.example.yml $f.yml; done && cd -
pip install -e .        # or: pip install -r requirements.lock && pip install -e . --no-deps
wojbot
```

Enable the **Message Content** intent in the Discord Developer Portal — the bot
won't start without it.

The warehouse schema lives in a separate repo (`leagueSQL`): `sql/ddl/schema.sql`
first, then `sql/ddl/rumors.sql`, `sql/ddl/schedule.sql` and the
`sql/dml/insert_*.sql` seeds. The bot's own database role only needs DML —
`schedule.sql` carries its own grants, because the schema's default privileges
don't reach a table created after they were set.

## Layout

```
wojbot/cogs/       one cog per command group; discovered and loaded automatically
wojbot/core/       config, permissions, database, and the rumor/scheduling rules
sql/queries/       named queries, registered at startup
resources/         league configs and generated state
tests/             pytest; `pip install -e .[dev]` gets the runner
requirements.lock  pinned runtime deps for deployments; regenerate with uv
```
