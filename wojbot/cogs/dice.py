"""Dice cog: roll dice with standard ``NdM`` notation.

A slash-only, dependency-free rewrite of the v1 ``Roller`` cog. The old version
parsed a free-form string with numpy and had several latent bugs; here the roll
parameters are proper typed slash options and the roll logic is pure Python
(``random``), so it's easy to read and test.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

MAX_DICE = 100
MAX_SIDES = 1000


@dataclass
class DiceResult:
    """Outcome of a roll. ``second`` is populated only for advantage/disadvantage."""

    count: int
    sides: int
    modifier: int
    roll_type: str
    first: list[int]
    kept: list[int]
    second: list[int] | None = None

    @property
    def total(self) -> int:
        return sum(self.kept) + self.modifier


def parse_notation(notation: str) -> tuple[int, int]:
    """Parse ``NdM`` / ``dM`` / ``M`` into ``(count, sides)``.

    Raises:
        ValueError: with a user-facing message if the notation is invalid.
    """
    text = notation.strip().lower().replace(" ", "")
    count_str, sep, sides_str = text.partition("d")
    if sep:  # "NdM" or "dM"
        count_str = count_str or "1"
    else:  # bare number means a single die of that many sides
        count_str, sides_str = "1", count_str

    try:
        count, sides = int(count_str), int(sides_str)
    except ValueError:
        raise ValueError(
            f"Couldn't read `{notation}`. Try something like `2d20`, `d6`, or `1d100`."
        ) from None

    if not 1 <= count <= MAX_DICE:
        raise ValueError(f"Number of dice must be between 1 and {MAX_DICE}.")
    if not 2 <= sides <= MAX_SIDES:
        raise ValueError(f"A die must have between 2 and {MAX_SIDES} sides.")
    return count, sides


def _roll_pool(count: int, sides: int) -> list[int]:
    return [random.randint(1, sides) for _ in range(count)]


def roll_dice(
    count: int, sides: int, modifier: int = 0, roll_type: str = "normal"
) -> DiceResult:
    """Roll a pool of dice, applying advantage/disadvantage per die if requested."""
    first = _roll_pool(count, sides)
    if roll_type == "normal":
        return DiceResult(count, sides, modifier, roll_type, first, kept=first)

    second = _roll_pool(count, sides)
    pick = max if roll_type == "advantage" else min
    kept = [pick(a, b) for a, b in zip(first, second)]
    return DiceResult(count, sides, modifier, roll_type, first, kept=kept, second=second)


def format_result(notation: str, r: DiceResult) -> str:
    """Render a roll as a friendly Discord message."""
    lines = [f"🎲 You rolled **{r.total}**!"]

    if r.second is None:
        lines.append(f"Dice (`{notation}`): {', '.join(map(str, r.first))}")
    else:
        label = "advantage" if r.roll_type == "advantage" else "disadvantage"
        lines.append(f"With **{label}** (`{notation}`):")
        lines.append(f"• roll 1: {', '.join(map(str, r.first))}")
        lines.append(f"• roll 2: {', '.join(map(str, r.second))}")
        lines.append(f"• kept:   {', '.join(map(str, r.kept))}")

    if r.modifier:
        lines.append(f"Modifier: {r.modifier:+d}")
    return "\n".join(lines)


class Dice(commands.Cog):
    """Roll dice for the D&D-inclined."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        description="Roll dice, e.g. 2d20. Supports advantage/disadvantage and a modifier."
    )
    @app_commands.describe(
        dice="Dice notation like 2d20, d6, or 1d100 (default 1d20).",
        modifier="Flat number added to the total (may be negative).",
        roll_type="Advantage/disadvantage rolls twice and keeps the higher/lower per die.",
    )
    @app_commands.choices(
        roll_type=[
            app_commands.Choice(name="Normal", value="normal"),
            app_commands.Choice(name="Advantage", value="advantage"),
            app_commands.Choice(name="Disadvantage", value="disadvantage"),
        ]
    )
    async def roll(
        self,
        interaction: discord.Interaction,
        dice: str = "1d20",
        modifier: int = 0,
        roll_type: app_commands.Choice[str] | None = None,
    ) -> None:
        rtype = roll_type.value if roll_type else "normal"
        try:
            count, sides = parse_notation(dice)
        except ValueError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return

        result = roll_dice(count, sides, modifier, rtype)
        await interaction.response.send_message(format_result(dice, result))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Dice(bot))
