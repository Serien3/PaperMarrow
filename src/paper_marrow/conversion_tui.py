"""Interactive terminal picker for explicit PDF conversion selection."""

from __future__ import annotations

import sys
from typing import Sequence, TextIO

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    BufferControl,
    ConditionalContainer,
    FormattedTextControl,
    HSplit,
    Layout,
    UIContent,
    UIControl,
    VSplit,
    Window,
)
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.screen import Point
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, Label

from .conversion import ConversionCandidate


_STATUS_ORDER = {
    "Ready": 0,
    "Converted": 1,
    "Unavailable": 2,
}


def supports_tui(
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> bool:
    """Return whether both sides of the interactive session are terminals."""
    source = sys.stdin if input_stream is None else input_stream
    destination = sys.stdout if output_stream is None else output_stream
    return source.isatty() and destination.isatty()


class PickerModel:
    """Presentation state kept independent from prompt_toolkit rendering."""

    def __init__(self, candidates: Sequence[ConversionCandidate]):
        self.candidates = tuple(
            sorted(
                candidates,
                key=lambda candidate: (
                    _STATUS_ORDER.get(candidate.status, len(_STATUS_ORDER)),
                    candidate.paper_folder.name.casefold(),
                    candidate.item_key,
                ),
            )
        )
        self.selected = set()
        self.query = ""
        self.cursor = 0
        self.confirming = False

    @property
    def visible(self) -> tuple[ConversionCandidate, ...]:
        needle = self.query.casefold().strip()
        if not needle:
            return self.candidates
        return tuple(
            candidate
            for candidate in self.candidates
            if needle
            in " ".join(
                (
                    candidate.item_key,
                    candidate.paper_folder.name,
                    candidate.status,
                    candidate.detail,
                )
            ).casefold()
        )

    @property
    def current(self) -> ConversionCandidate | None:
        visible = self.visible
        return visible[self.cursor] if visible else None

    @property
    def ready_count(self) -> int:
        return sum(candidate.status == "Ready" for candidate in self.candidates)

    def set_query(self, query: str) -> None:
        self.query = query
        self.cursor = 0

    def move(self, amount: int) -> None:
        visible = self.visible
        if visible:
            self.cursor = min(max(self.cursor + amount, 0), len(visible) - 1)

    def toggle_current(self) -> None:
        candidate = self.current
        if candidate is None or candidate.status != "Ready":
            return
        if candidate.item_key in self.selected:
            self.selected.remove(candidate.item_key)
        else:
            self.selected.add(candidate.item_key)

    def toggle_visible_ready(self) -> None:
        ready = {
            candidate.item_key for candidate in self.visible if candidate.status == "Ready"
        }
        if ready and ready <= self.selected:
            self.selected.difference_update(ready)
        else:
            self.selected.update(ready)

    def selected_keys(self) -> list[str]:
        return [
            candidate.item_key
            for candidate in self.candidates
            if candidate.item_key in self.selected
        ]


class _CandidateControl(UIControl):
    def __init__(self, model: PickerModel):
        self.model = model

    def is_focusable(self) -> bool:
        return True

    def create_content(self, width: int, height: int) -> UIContent:
        del width, height
        visible = self.model.visible
        if not visible:
            lines: list[StyleAndTextTuples] = [
                [("class:empty", "  没有匹配当前搜索的论文")]
            ]
            cursor = Point(x=0, y=0)
        else:
            lines = [self._render(candidate, index) for index, candidate in enumerate(visible)]
            cursor = Point(x=0, y=self.model.cursor)
        return UIContent(
            get_line=lambda number: lines[number],
            line_count=len(lines),
            cursor_position=cursor,
        )

    def _render(
        self, candidate: ConversionCandidate, index: int
    ) -> StyleAndTextTuples:
        current = index == self.model.cursor
        selectable = candidate.status == "Ready"
        if selectable:
            mark = "[x]" if candidate.item_key in self.model.selected else "[ ]"
        else:
            mark = "[-]"
        cursor = ">" if current else " "
        row_style = "class:cursor" if current else ""
        status_style = "class:status." + candidate.status.lower().replace(" ", "-")
        return [
            (row_style, f"{cursor} {mark} "),
            (f"{row_style} {status_style}", f"{candidate.status:<11}"),
            (row_style, f" {candidate.item_key}  {candidate.paper_folder.name}"),
        ]


def run_conversion_picker(
    candidates: Sequence[ConversionCandidate],
    *,
    input: Input | None = None,
    output: Output | None = None,
) -> list[str] | None:
    """Run the full-screen picker and return confirmed item keys, or ``None``."""
    model = PickerModel(candidates)
    candidate_control = _CandidateControl(model)
    search_buffer = Buffer(multiline=False)
    search_control = BufferControl(buffer=search_buffer)
    bindings = KeyBindings()

    @Condition
    def browsing() -> bool:
        return not model.confirming and not get_app().layout.has_focus(search_control)

    @Condition
    def confirming() -> bool:
        return model.confirming

    def invalidate() -> None:
        get_app().invalidate()

    def finish_search(event) -> None:
        event.app.layout.focus(candidate_control)
        event.app.invalidate()

    def on_search_changed(buffer: Buffer) -> None:
        model.set_query(buffer.text)
        invalidate()

    search_buffer.on_text_changed += on_search_changed

    @bindings.add("up", filter=browsing)
    def _move_up(event) -> None:
        model.move(-1)
        event.app.invalidate()

    @bindings.add("down", filter=browsing)
    def _move_down(event) -> None:
        model.move(1)
        event.app.invalidate()

    @bindings.add("space", filter=browsing)
    def _toggle(event) -> None:
        model.toggle_current()
        event.app.invalidate()

    @bindings.add("a", filter=browsing)
    def _toggle_all(event) -> None:
        model.toggle_visible_ready()
        event.app.invalidate()

    @bindings.add("/", filter=browsing)
    def _search(event) -> None:
        event.app.layout.focus(search_control)

    @bindings.add("enter", filter=browsing)
    def _review(event) -> None:
        if model.selected:
            model.confirming = True
            event.app.invalidate()

    @bindings.add("q", filter=browsing)
    def _quit(event) -> None:
        event.app.exit(result=None)

    @bindings.add("enter", filter=has_focus(search_control))
    @bindings.add("escape", filter=has_focus(search_control))
    def _finish_search(event) -> None:
        finish_search(event)

    @bindings.add("y", filter=confirming)
    def _confirm(event) -> None:
        event.app.exit(result=model.selected_keys())

    @bindings.add("n", filter=confirming)
    @bindings.add("escape", filter=confirming)
    def _return_to_list(event) -> None:
        model.confirming = False
        event.app.layout.focus(candidate_control)
        event.app.invalidate()

    @bindings.add("c-c")
    @bindings.add("c-d")
    def _cancel(event) -> None:
        event.app.exit(result=None)

    def header_text() -> str:
        filtered = f" / {len(model.visible)} 匹配" if model.query else ""
        return (
            f"转换论文  |  {model.ready_count} Ready / {len(model.candidates)} 篇论文"
            f" / 已选择 {len(model.selected)}{filtered}"
        )

    def search_text() -> str:
        return "无" if not model.query else model.query

    def detail_text() -> StyleAndTextTuples:
        candidate = model.current
        if candidate is None:
            return [("class:detail", "详情：没有匹配项")]
        return [
            ("class:detail.label", "详情："),
            ("class:detail", f"{candidate.status} — {candidate.detail}"),
        ]

    def toolbar_text() -> str:
        if get_app().layout.has_focus(search_control):
            return "输入关键词实时筛选  Enter/Esc 返回列表  Ctrl+U 清空  Ctrl+C 取消"
        return "↑↓ 移动  Space 选择  a 全选当前 Ready  / 搜索  Enter 继续  q 取消"

    list_view = HSplit(
        [
            Label(header_text, style="class:header"),
            ConditionalContainer(
                VSplit(
                    [
                        Label("搜索：", width=Dimension.exact(6)),
                        Window(search_control, height=1),
                    ]
                ),
                filter=has_focus(search_control),
            ),
            ConditionalContainer(
                Label(lambda: f"搜索：{search_text()}", style="class:search"),
                filter=~has_focus(search_control),
            ),
            Window(
                candidate_control,
                wrap_lines=False,
                always_hide_cursor=True,
                right_margins=[ScrollbarMargin(display_arrows=True)],
            ),
            Frame(
                Window(
                    content=FormattedTextControl(detail_text),
                    height=Dimension(min=1, max=3),
                    wrap_lines=True,
                ),
                title="当前论文",
            ),
            Label(toolbar_text, style="class:toolbar"),
        ]
    )

    def confirmation_text() -> StyleAndTextTuples:
        rows: StyleAndTextTuples = []
        for candidate in model.candidates:
            if candidate.item_key in model.selected:
                rows.append(("", f"  {candidate.item_key}  {candidate.paper_folder.name}\n"))
        return rows

    confirmation_view = HSplit(
        [
            Label(
                lambda: f"将转换 {len(model.selected)} 篇论文，可能消耗 MinerU 额度。",
                style="class:warning",
            ),
            Frame(
                Window(
                    content=FormattedTextControl(confirmation_text),
                    wrap_lines=False,
                    always_hide_cursor=True,
                ),
                title="已选择",
            ),
            Label(
                "y 确认转换   n/Esc 返回选择   Ctrl+C 取消",
                style="class:toolbar",
            ),
        ]
    )
    root = HSplit(
        [
            ConditionalContainer(list_view, filter=~confirming),
            ConditionalContainer(confirmation_view, filter=confirming),
        ]
    )
    style = Style.from_dict(
        {
            "header": "bold",
            "search": "fg:#888888",
            "cursor": "reverse",
            "status.ready": "fg:#00aa00 bold",
            "status.converted": "fg:#00aaaa",
            "status.unavailable": "fg:#aa5500",
            "detail.label": "bold",
            "toolbar": "reverse",
            "warning": "fg:#aa5500 bold",
            "empty": "fg:#888888",
        }
    )
    application: Application[list[str] | None] = Application(
        layout=Layout(root, focused_element=candidate_control),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=False,
        style=style,
        input=input,
        output=output,
    )
    return application.run()
