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
* > 25 options → paginated select menu with prev/next buttons
* no options (open-ended question) → button that opens a text-input Modal

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
                # No choices — open-ended free-text question.
                answer = await self._ask_free_text(q_text, header)
            else:
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
        elif len(options) > _DISCORD_SELECT_MAX_OPTIONS:
            view = AskPaginatedSelectView(
                labels, descriptions, multi_select=multi_select, timeout=self.timeout
            )
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

    async def _ask_free_text(
        self,
        question_text: str,
        header: str,
    ) -> str | None:
        """Show a button that opens a Modal for free-text input."""
        embed = discord.Embed(
            title=f"❓ {header}"[:256],
            description=question_text[:_DISCORD_EMBED_DESC_MAX] or "\u200b",
        )

        view = _ModalTriggerView(
            question=question_text,
            header=header,
            timeout=self.timeout,
        )

        try:
            msg = await self.thread.send(embed=embed, view=view)
        except discord.HTTPException:
            log.exception("Failed to send free-text AskUserQuestion UI to thread")
            return None

        try:
            return await view.wait_for_text()
        finally:
            for item in view.children:
                setattr(item, "disabled", True)
            try:
                await msg.edit(view=view)
            except discord.HTTPException:
                log.debug("Could not disable free-text view (already gone)")


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
        # Defer creating the asyncio.Future until ``wait_for_result`` is
        # actually called. ``asyncio.get_running_loop()`` raises when no
        # loop is running, which makes constructing a view from sync code
        # (tests, early init) impossible if we eagerly bound the future
        # here. Instead, callbacks stash the result in ``_result`` and the
        # future — if any waiter has registered — is resolved at that point.
        self._result_future: asyncio.Future[list[int] | None] | None = None
        self._result: list[int] | None = None
        self._resolved: bool = False

    async def wait_for_result(self) -> list[int] | None:
        """Block until the user picks (or the view times out)."""
        # If a callback (or the timeout) already fired before we started
        # waiting, return the captured value immediately rather than
        # blocking on a future that will never be set.
        if self._resolved:
            return self._result
        if self._result_future is None:
            self._result_future = asyncio.get_running_loop().create_future()
        try:
            return await self._result_future
        except asyncio.CancelledError:
            return None

    async def on_timeout(self) -> None:  # type: ignore[override]
        self._set_result(None)

    def _resolve(self, indices: list[int]) -> None:
        self._set_result(indices)
        self.stop()

    def _set_result(self, value: list[int] | None) -> None:
        """Capture the outcome and notify any pending ``wait_for_result``."""
        if self._resolved:
            return
        self._resolved = True
        self._result = value
        fut = self._result_future
        if fut is not None and not fut.done():
            fut.set_result(value)


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
        placeholder: str = "Pick an option…",
    ) -> None:
        super().__init__(
            placeholder=placeholder,
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


# ---------------------------------------------------------------------------
# Paginated select view for > 25 options
# ---------------------------------------------------------------------------


class AskPaginatedSelectView(_BaseAskView):
    """Paginated select menu for > 25 options.

    Splits the option list into pages of ``page_size`` items, each rendered
    as a Discord select menu.  Prev/Next buttons navigate between pages.
    """

    def __init__(
        self,
        labels: list[str],
        descriptions: list[str],
        *,
        multi_select: bool,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        page_size: int = _DISCORD_SELECT_MAX_OPTIONS,
    ) -> None:
        super().__init__(timeout=timeout)
        self._labels = labels
        self._descriptions = descriptions
        self._multi_select = multi_select
        self._page = 0
        self._page_size = page_size
        self._total_pages = max(1, (len(labels) + page_size - 1) // page_size)
        self._build_page()

    @property
    def page(self) -> int:
        return self._page

    @property
    def total_pages(self) -> int:
        return self._total_pages

    def _build_page(self) -> None:
        self.clear_items()
        start = self._page * self._page_size
        end = min(start + self._page_size, len(self._labels))

        options: list[discord.SelectOption] = []
        for i in range(start, end):
            desc = self._descriptions[i] if i < len(self._descriptions) else ""
            options.append(
                discord.SelectOption(
                    label=self._labels[i][:_DISCORD_SELECT_LABEL_MAX] or f"Option {i + 1}",
                    description=desc[:_DISCORD_SELECT_DESC_MAX] if desc else None,
                    value=str(i),
                )
            )

        max_values = len(options) if self._multi_select else 1
        self.add_item(
            _AskSelect(
                options=options,
                min_values=1,
                max_values=max_values,
                placeholder=f"Page {self._page + 1}/{self._total_pages}",
            )
        )

        if self._page > 0:
            self.add_item(_PageButton("◀️ Prev", -1, self))
        if self._page < self._total_pages - 1:
            self.add_item(_PageButton("Next ▶️", 1, self))


class _PageButton(discord.ui.Button):
    """Navigation button for :class:`AskPaginatedSelectView`."""

    def __init__(
        self, label: str, direction: int, paginated_view: AskPaginatedSelectView
    ) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self._dir = direction
        self._pview = paginated_view

    async def callback(self, interaction: discord.Interaction) -> None:
        self._pview._page += self._dir
        self._pview._build_page()
        try:
            await interaction.response.edit_message(view=self._pview)
        except discord.HTTPException:
            log.debug("Failed to edit paginated view (already gone)")


# ---------------------------------------------------------------------------
# Modal for free-text (open-ended) questions
# ---------------------------------------------------------------------------


class AskTextModal(discord.ui.Modal):
    """Modal dialog for free-text answers to open-ended questions."""

    answer = discord.ui.TextInput(
        label="Your answer",
        style=discord.TextStyle.paragraph,
        max_length=4000,
    )

    def __init__(self, question: str, header: str) -> None:
        super().__init__(title=header[:45] or "Question")
        self.answer.label = question[:45] or "Your answer"
        self._text_future: asyncio.Future[str | None] | None = None
        self._text_result: str | None = None
        self._text_resolved: bool = False

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._text_resolved = True
        self._text_result = self.answer.value
        if self._text_future is not None and not self._text_future.done():
            self._text_future.set_result(self.answer.value)
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("AskTextModal error")
        self._text_resolved = True
        self._text_result = None
        if self._text_future is not None and not self._text_future.done():
            self._text_future.set_result(None)


class _ModalTriggerView(discord.ui.View):
    """View with a single button that opens :class:`AskTextModal`.

    Because Discord Modals can only be sent as an interaction response,
    we render a button first and open the Modal when the user clicks it.
    """

    def __init__(
        self,
        question: str,
        header: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self._question = question
        self._header = header
        self._text_future: asyncio.Future[str | None] | None = None
        self._text_result: str | None = None
        self._text_resolved: bool = False

        btn = discord.ui.Button(
            label="✏️ Type your answer",
            style=discord.ButtonStyle.primary,
            custom_id="clauded_ask_modal_trigger",
        )
        btn.callback = self._open_modal
        self.add_item(btn)

    async def _open_modal(self, interaction: discord.Interaction) -> None:
        modal = AskTextModal(self._question, self._header)
        # Wire the modal's future so on_submit resolves *our* waiter.
        modal._text_future = asyncio.get_running_loop().create_future()

        try:
            await interaction.response.send_modal(modal)
        except discord.HTTPException:
            log.debug("Failed to send AskTextModal (already responded)")
            self._set_text(None)
            return

        # Wait for modal submission
        try:
            text = await asyncio.wait_for(modal._text_future, timeout=self.timeout or 300)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            text = None
        self._set_text(text)
        self.stop()

    async def wait_for_text(self) -> str | None:
        """Block until the user submits text or the view times out."""
        if self._text_resolved:
            return self._text_result
        if self._text_future is None:
            self._text_future = asyncio.get_running_loop().create_future()
        try:
            return await self._text_future
        except asyncio.CancelledError:
            return None

    async def on_timeout(self) -> None:
        self._set_text(None)

    def _set_text(self, value: str | None) -> None:
        if self._text_resolved:
            return
        self._text_resolved = True
        self._text_result = value
        fut = self._text_future
        if fut is not None and not fut.done():
            fut.set_result(value)


__all__ = [
    "InteractionHandler",
    "AskButtonView",
    "AskSelectView",
    "AskPaginatedSelectView",
    "AskTextModal",
    "DEFAULT_TIMEOUT_SECONDS",
]
