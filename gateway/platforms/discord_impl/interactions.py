"""Discord slash command wiring and approval UI helpers."""

from __future__ import annotations

from typing import Any

from gateway.platforms.discord_impl import components as discord_components
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


def create_component_runtime() -> discord_components.DiscordComponentRuntime:
    """Create a generic Discord component runtime."""
    return discord_components.DiscordComponentRuntime()


if DISCORD_AVAILABLE:

    class ExecApprovalView(discord_components.ManagedComponentView):
        """Interactive exec approval view built on the generic component runtime."""

        def __init__(
            self,
            approval_id: str,
            allowed_user_ids: set,
            runtime: discord_components.DiscordComponentRuntime | None = None,
        ):
            self._runtime = runtime or create_component_runtime()
            super().__init__(self._runtime, timeout=300)
            self.approval_id = approval_id
            self.allowed_user_ids = allowed_user_ids
            self.resolved = False
            self.add_button(
                discord_components.DiscordButtonSpec(
                    label="Allow Once",
                    style="success",
                    allowed_user_ids=tuple(str(user_id) for user_id in allowed_user_ids),
                    handler=lambda invocation: self.allow_once(
                        invocation.interaction,
                        invocation.component,
                    ),
                )
            )
            self.add_button(
                discord_components.DiscordButtonSpec(
                    label="Always Allow",
                    style="primary",
                    allowed_user_ids=tuple(str(user_id) for user_id in allowed_user_ids),
                    handler=lambda invocation: self.allow_always(
                        invocation.interaction,
                        invocation.component,
                    ),
                )
            )
            self.add_button(
                discord_components.DiscordButtonSpec(
                    label="Deny",
                    style="danger",
                    allowed_user_ids=tuple(str(user_id) for user_id in allowed_user_ids),
                    handler=lambda invocation: self.deny(
                        invocation.interaction,
                        invocation.component,
                    ),
                )
            )

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

        async def allow_once(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "allow_once", discord.Color.green())

        async def allow_always(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            await self._resolve(interaction, "allow_always", discord.Color.blue())

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
