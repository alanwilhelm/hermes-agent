"""Discord slash command wiring and approval UI helpers."""

from __future__ import annotations

from typing import Any

from gateway.platforms.discord_impl import native_commands

try:
    import discord
    DISCORD_AVAILABLE = True
except ImportError:  # pragma: no cover - import guard
    discord = None
    DISCORD_AVAILABLE = False


def register_slash_commands(tree: Any, adapter: Any) -> None:
    """Register Discord slash commands on the command tree."""
    native_commands.register_slash_commands(tree, adapter)


def build_slash_event(adapter: Any, interaction: discord.Interaction, text: str):
    """Build a MessageEvent from a Discord slash command interaction."""
    return native_commands.build_slash_event(adapter, interaction, text)


if DISCORD_AVAILABLE:

    class ExecApprovalView(discord.ui.View):
        """Interactive button view for exec approval of dangerous commands."""

        def __init__(self, approval_id: str, allowed_user_ids: set):
            super().__init__(timeout=300)
            self.approval_id = approval_id
            self.allowed_user_ids = allowed_user_ids
            self.resolved = False

        def _check_auth(self, interaction: discord.Interaction) -> bool:
            """Verify the user clicking is authorized."""
            if not self.allowed_user_ids:
                return True
            return str(interaction.user.id) in self.allowed_user_ids

        async def _resolve(
            self, interaction: discord.Interaction, action: str, color: discord.Color
        ):
            """Resolve the approval and update the message."""
            if self.resolved:
                await interaction.response.send_message(
                    "This approval has already been resolved~", ephemeral=True
                )
                return

            if not self._check_auth(interaction):
                await interaction.response.send_message(
                    "You're not authorized to approve commands~", ephemeral=True
                )
                return

            self.resolved = True

            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if embed:
                embed.color = color
                embed.set_footer(text=f"{action} by {interaction.user.display_name}")

            for child in self.children:
                child.disabled = True

            await interaction.response.edit_message(embed=embed, view=self)

            try:
                from tools.approval import approve_permanent

                if action == "allow_always":
                    approve_permanent(self.approval_id)
            except ImportError:
                pass

        @discord.ui.button(label="Allow Once", style=discord.ButtonStyle.green)
        async def allow_once(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "allow_once", discord.Color.green())

        @discord.ui.button(label="Always Allow", style=discord.ButtonStyle.blurple)
        async def allow_always(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "allow_always", discord.Color.blue())

        @discord.ui.button(label="Deny", style=discord.ButtonStyle.red)
        async def deny(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "deny", discord.Color.red())

        async def on_timeout(self):
            """Handle view timeout -- disable buttons and mark as expired."""
            self.resolved = True
            for child in self.children:
                child.disabled = True

else:  # pragma: no cover - import guard
    ExecApprovalView = None
