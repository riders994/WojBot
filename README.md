# WojBot

A Discord bot for a fantasy basketball league. Slash commands only.

- **Rumors** — managers file league rumors through a wizard, from the server or
  from a DM, and the bot posts them to a channel without naming the reporter.
- **Elo** — standings and ratings, backed by the
  [`elo-system`](https://github.com/riders994/eloSystem) package.
- **Commissioner tools** — leagues, seasons, and the warehouse dimension tables.
- **Setup** — per-server configuration with a guided wizard, and three
  permission tiers on top of it.

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
cp .env.example .env    # fill in DISCORD_TOKEN and SQL_CONN_URI
pip install -e .
wojbot
```

Enable the **Message Content** intent in the Discord Developer Portal — the bot
won't start without it.

The warehouse schema lives in a separate repo (`leagueSQL`): `sql/ddl/schema.sql`
first, then `sql/ddl/rumors.sql` and the `sql/dml/insert_*.sql` seeds. The bot's
own database role only needs DML.

## Layout

```
wojbot/cogs/     one cog per command group; discovered and loaded automatically
wojbot/core/     config, permissions, database, and the rumor domain rules
sql/queries/     named queries, registered at startup
resources/       league configs and generated state
tests/           pytest; `pip install -e .[dev]` gets the runner
```
