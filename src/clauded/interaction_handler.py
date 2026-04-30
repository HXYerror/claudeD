"""Bridge ``AskUserQuestion`` tool calls to Discord buttons / select menus.

When Claude (running inside a :class:`ClaudeBridge` session) invokes the
``AskUserQuestion`` tool, the SDK's ``can_use_tool`` callback delegates to
:meth:`InteractionHandler.handle_ask_user_question`. This class renders the
question(s) as Discord interactive components in the bound thread, awaits the
user's click, and returns an ``updated_input`` dict that augments the original
tool input with an ``answers`` field — that becomes the ``updated_input`` of a
``PermissionResultAllow`` returned to the SDK.

UI rules:
* single-select, ≤ 4 options → row of buttons
* multi-select OR > 4 options → select menu (capped at Discord's 25 options)

A single ``AskUserQuestion`` invocation may carry multiple sub-questions; they
are asked one at a time, in order. If the user fails to answer any of them
within :data:`DEFAULT_TIMEOUT_SECONDS`, the whole interaction returns ``None``
and the bridge denies the tool call with a timeout message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

log = logging.getLogger("clauded.interaction_handler")

# Default time we will wait for the user to answer a single sub-question
# before timing out and denying the AskUserQuestion tool call.
DEFAULT_TIMEOUT_SECONDS = 300.0

# Discord-imposed limits we respect when building components.
_DISCORD_BUTTON_LABEL_MAX = 80
_DISCORD_SELECT_LABEL_MAX = 100
_DISCORD_SELECT_DESC_MAX = 100
_DISCORD_SELECT_MAX_OPTIONS = 25
_DISCORD_EMBED_DESC_MAX = 4000


class InteractionHandler:
    """Render ``AskUserQuestion`` calls into Discord UI in a single thread."""

    def __init__(
        self,
        thread: discord.abc.Messageable,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.thread = thread
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Entry point used as the ``on_ask_user`` callback by ClaudeBridge.
    # ------------------------------------------------------------------

    async def handle_ask_user_question(
        self, tool_input: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Drive a Discord UI for an ``AskUserQuestion`` tool call.

        Returns the ``updated_input`` to pass back to the SDK on success
        (a copy of ``tool_input`` with an additional ``answers`` field), or
        ``None`` if the user did not respond in time / the input was empty.
        """
        questions = tool_input.get("questions") or []
        if not questions:
            log.warning("AskUserQuestion called with no questions; denying")
            return None

        answers: dict[str, Any] = {}
        for q in questions:
            if not isinstance(q, dict):
                continue
            q_text = str(q.get("question") or "").strip()
            header = str(q.get("header") or "Question").strip() or "Question"
            options = q.get("options") or []
            multi_select = bool(q.get("multiSelect", False))

            if not options:
                # No choices to render → skip silently.
                continue

            answer = await self._ask_one(q_text, header, options, multi_select)
            if answer is None:
                log.info("AskUserQuestion timed out waiting for user")
                return None
            # Key answers by question text so the tool can correlate them.
            answers[q_text or header] = answer

        if not answers:
            return None
        return {**tool_input, "answers": answers}

    # ------------------------------------------------------------------
    # Per-question rendering
    # ------------------------------------------------------------------

    async def _ask_one(
        self,
        question_text: str,
        header: str,
        options: list[dict[str, Any]],
        multi_select: bool,
    ) -> Any:
        """Show one sub-question and return the user's selection."""
        labels = [
            str(opt.get("label") or f"Option {i + 1}") for i, opt in enumerate(options)
        ]
        descriptions = [str(opt.get("description") or "") for opt in options]

        use_buttons = (not multi_select) and len(options) <= 4
        if use_buttons:
            view: _BaseAskView = AskButtonView(labels, timeout=self.timeout)
        else:
            view = AskSelectView(
                labels, descriptions, multi_select=multi_select, timeout=self.timeout
            )

        embed = discord.Embed(
            title=f"❓ {header}"[:256],
            description=_render_question_body(question_text, options),
        )

        try:
            msg = await self.thread.send(embed=embed, view=view)
        except discord.HTTPException:
            log.exception("Failed to send AskUserQuestion UI to thread")
            return None

        try:
            indices = await view.wait_for_result()
        finally:
            for item in view.children:
                # Both Buttons and Selects expose ``.disabled``.
                setattr(item, "disabled", True)
            try:
                await msg.edit(view=view)
            except discord.HTTPException:
                log.debug("Could not disable AskUserQuestion view (already gone)")

        if not indices:
            return None
        if multi_select:
            return [labels[i] for i in indices if 0 <= i < len(labels)]
        first = indices[0]
        if 0 <= first < len(labels):
            return labels[first]
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_question_body(question: str, options: list[dict[str, Any]]) -> str:
    """Render question + per-option descriptions as the embed body."""
    parts: list[str] = []
    if question:
        parts.append(question)
    bullet_lines: list[str] = []
    for i, opt in enumerate(options):
        label = str(opt.get("label") or f"Option {i + 1}")
        desc = str(opt.get("description") or "").strip()
        if desc:
            bullet_lines.append(f"• **{label}** — {desc}")
        else:
            bullet_lines.append(f"• **{label}**")
    if bullet_lines:
        if parts:
            parts.append("")
        parts.extend(bullet_lines)
    body = "\n".join(parts)
    return body[:_DISCORD_EMBED_DESC_MAX] or "\u200b"


# ---------------------------------------------------------------------------
# Discord UI views
# ---------------------------------------------------------------------------


class _BaseAskView(discord.ui.View):
    """Common future-resolution scaffolding shared by button/select views."""

    def __init__(self, *, timeout: float) -> None:
        super().__init__(timeout=timeout)
        self._result_future: asyncio.Future[list[int] | None] = (
            asyncio.get_running_loop().create_future()
        )

    async def wait_for_result(self) -> list[int] | None:
        """Block until the user picks (or the view times out)."""
        try:
            return await self._result_future
        except asyncio.CancelledError:
            return None

    async def on_timeout(self) -> None:  # type: ignore[override]
        if not self._result_future.done():
            self._result_future.set_result(None)

    def _resolve(self, indices: list[int]) -> None:
        if not self._result_future.done():
            self._result_future.set_result(indices)
        self.stop()


class AskButtonView(_BaseAskView):
    """Single-select view: renders ≤ 4 button options."""

    def __init__(self, labels: list[str], timeout: float = DEFAULT_TIMEOUT_SECONDS):
        super().__init__(timeout=timeout)
        for i, label in enumerate(labels[:4]):
            style = (
                discord.ButtonStyle.primary
                if i == 0
                else discord.ButtonStyle.secondary
            )
            self.add_item(_AskButton(label=label, style=style, option_index=i))


class _AskButton(discord.ui.Button):
    def __init__(
        self, *, label: str, style: discord.ButtonStyle, option_index: int
    ) -> None:
        super().__init__(
            label=(label[:_DISCORD_BUTTON_LABEL_MAX] or "?"),
            style=style,
            custom_id=f"clauded_ask_btn_{option_index}",
        )
        self.option_index = option_index

    async def callback(self, interaction: discord.Interaction) -> None:
        # ``self.view`` is set by discord.py when the button is added to a view.
        view = self.view
        if isinstance(view, _BaseAskView):
            view._resolve([self.option_index])
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            log.debug("Failed to defer button interaction (already responded)")


class AskSelectView(_BaseAskView):
    """Select-menu view: used for > 4 options or when ``multiSelect`` is true."""

    def __init__(
        self,
        labels: list[str],
        descriptions: list[str],
        *,
        multi_select: bool,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        capped = list(zip(labels, descriptions))[:_DISCORD_SELECT_MAX_OPTIONS]
        select_options: list[discord.SelectOption] = []
        for i, (label, desc) in enumerate(capped):
            select_options.append(
                discord.SelectOption(
                    label=(label[:_DISCORD_SELECT_LABEL_MAX] or f"Option {i + 1}"),
                    description=(desc[:_DISCORD_SELECT_DESC_MAX] if desc else None),
                    value=str(i),
                )
            )
        max_values = len(select_options) if multi_select else 1
        self.add_item(
            _AskSelect(
                options=select_options,
                min_values=1,
                max_values=max_values,
            )
        )


class _AskSelect(discord.ui.Select):
    def __init__(
        self,
        *,
        options: list[discord.SelectOption],
        min_values: int,
        max_values: int,
    ) -> None:
        super().__init__(
            placeholder="Pick an option…",
            min_values=min_values,
            max_values=max_values,
            options=options,
            custom_id="clauded_ask_select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        indices: list[int] = []
        for value in self.values:
            try:
                indices.append(int(value))
            except (TypeError, ValueError):
                continue
        if isinstance(view, _BaseAskView):
            view._resolve(indices)
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            log.debug("Failed to defer select interaction (already responded)")


__all__ = [
    "InteractionHandler",
    "AskButtonView",
    "AskSelectView",
    "DEFAULT_TIMEOUT_SECONDS",
]
