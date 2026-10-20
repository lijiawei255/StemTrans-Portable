"""Minimal deep-navy interface for StemTrans-Portable.

Design language
---------------
The palette is taken from a navy-and-white reference: deep navy ink, porcelain
white surfaces, ice-blue highlights and a single muted red for recording.
There are no large saturated blocks and no yellow "highlighter" chips; the
translation itself is the strongest element on screen.

Layout
------
* Persistent navy navigation and a single start/stop action.
* Conversation page with explicit language routing, progress and large text.
* Separate settings page for audio, optional voice and reading preferences.
* Single-column reading for one direction; optional two-column conversation.

The window remains a pure view: no audio or model imports, so the screenshot
harness can instantiate it without loading anything.
"""

from __future__ import annotations

import queue
import re
import time
import tkinter
import tkinter.font
from typing import Callable, Optional

import customtkinter as ctk

ctk.set_appearance_mode("light")

PALETTE = {
    "bg": "#F4F7FB",  # fog white
    "card": "#FFFFFF",  # porcelain
    "ink": "#1E2F52",  # deep navy (body text / primary)
    "ink_soft": "#3E5C8F",  # indigo accent
    "muted": "#6F7E95",  # secondary text
    "faint": "#9AA8BB",  # tertiary text
    "line": "#DCE5F0",  # 1 px borders
    "line_soft": "#E9EFF7",  # dividers
    "ice": "#E8F0FA",  # hover / term highlight
    "navy_hover": "#2A4066",
    "accent": "#5B78A8",  # periwinkle (right channel)
    "red": "#B04A45",  # recording / stop only
    "red_soft": "#E7D6D4",
    "green": "#4E7C6A",
    "amber": "#B8893F",
}

BACKGROUND = PALETTE["bg"]
CARD = PALETTE["card"]
INK = PALETTE["ink"]
MUTED = PALETTE["muted"]
FAINT = PALETTE["faint"]
LINE = PALETTE["line"]
LINE_SOFT = PALETTE["line_soft"]
ICE = PALETTE["ice"]
NAVY = PALETTE["ink"]
NAVY_HOVER = PALETTE["navy_hover"]
ACCENT = PALETTE["accent"]
RED = PALETTE["red"]
TOPBAR = PALETTE["ink"]
TOPBAR_LINE = "#3A4F74"
TOPBAR_TEXT = "#C9D6E8"
TOPBAR_MUTED = "#8FA3C4"

MODES = {"online": "线上双通道", "offline": "线下单麦"}
MODE_BY_LABEL = {label: key for key, label in MODES.items()}


def _pick_family(candidates: tuple[str, ...]) -> str:
    try:
        available = set(tkinter.font.families())
    except Exception:
        available = set()
    for name in candidates:
        if name in available:
            return name
    return candidates[-1]


def estimate_wrap_lines(
    text: str, font: tkinter.font.Font, width_px: int, max_lines: int = 14
) -> int:
    """Deterministic wrapped-line count for the first paint."""
    if not text:
        return 1
    if width_px <= 40:
        return max(1, min(max_lines, text.count("\n") + 1))
    total = 0
    for paragraph in text.split("\n"):
        tokens = re.findall(
            r"[\u3400-\u4dbf\u4e00-\u9fff]|[^\s\u3400-\u4dbf\u4e00-\u9fff]+|\s+",
            paragraph,
        )
        line_width = 0
        lines = 1
        for token in tokens:
            if token.isspace():
                if line_width > 0:
                    line_width += font.measure(" ")
                continue
            token_width = font.measure(token)
            if line_width + token_width > width_px and line_width > 0:
                lines += 1
                line_width = token_width
            else:
                line_width += token_width
        total += lines
    return max(1, min(max_lines, total))


# ---------------------------------------------------------------------------
# Transcript card
# ---------------------------------------------------------------------------
class TranscriptCard(ctk.CTkFrame):
    """One turn: quiet source line, dominant translation, subtle terms."""

    def __init__(
        self,
        master,
        *,
        index: int,
        timestamp: str,
        source: str,
        translation: str,
        terms: list[str],
        src_font: ctk.CTkFont,
        dst_font: ctk.CTkFont,
        meta_font: ctk.CTkFont,
        term_font: ctk.CTkFont,
        latency: float = 0.0,
        tag_text: str = "",
        copy_callback: Optional[Callable[[str], None]] = None,
        width_hint: int = 600,
    ) -> None:
        super().__init__(
            master,
            fg_color=CARD,
            border_width=1,
            border_color=LINE,
            corner_radius=10,
        )
        self._copy_callback = copy_callback
        self._translation = translation
        self._dst_font = dst_font
        self._width_hint = width_hint
        self.grid_columnconfigure(0, weight=1)

        body = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0)
        body.grid(row=0, column=0, sticky="nsew", padx=14, pady=(11, 12))
        body.grid_columnconfigure(0, weight=1)

        # ---- meta ----------------------------------------------------------
        meta = ctk.CTkFrame(body, fg_color=CARD, corner_radius=0, height=16)
        meta.grid(row=0, column=0, sticky="ew")
        meta.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(
            meta, text=f"{index:02d}", font=meta_font, text_color=MUTED, height=16
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            meta, text=timestamp, font=meta_font, text_color=MUTED, height=16
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))

        ctk.CTkLabel(
            meta, text=f"{latency:.1f}s", font=meta_font, text_color=MUTED, height=16
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

        if tag_text:
            ctk.CTkLabel(
                meta, text=tag_text, font=meta_font, text_color=MUTED, height=16
            ).grid(row=0, column=4, sticky="e", padx=(8, 6))

        self._copy_button = ctk.CTkButton(
            meta,
            text="复制",
            width=66,
            height=30,
            corner_radius=5,
            border_width=0,
            fg_color="transparent",
            hover_color=ICE,
            text_color=MUTED,
            font=meta_font,
            command=self._copy,
        )
        self._copy_button.grid(row=0, column=5, sticky="e")

        # ---- source --------------------------------------------------------
        self._source_label = ctk.CTkLabel(
            body,
            text="识别原文\n" + source,
            font=src_font,
            text_color=INK,
            justify="left",
            anchor="w",
            wraplength=max(180, int(self._reverse_widget_scaling(width_hint))),
        )
        self._source_label.grid(row=1, column=0, sticky="ew", pady=(3, 7))

        rule = ctk.CTkFrame(body, fg_color=LINE_SOFT, height=1, corner_radius=0)
        rule.grid(row=2, column=0, sticky="ew")

        # ---- translation ---------------------------------------------------
        lines = estimate_wrap_lines(translation, dst_font, max(240, width_hint - 4), 8)
        self._text = tkinter.Text(
            body,
            height=lines,
            width=1,
            wrap="word",
            bg=CARD,
            fg=INK,
            bd=0,
            highlightthickness=0,
            relief="flat",
            font=dst_font,
            cursor="arrow",
            padx=0,
            pady=0,
            spacing1=3,
            spacing3=2,
            insertwidth=0,
            takefocus=0,
            selectbackground=ICE,
            selectforeground=INK,
        )
        self._text.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self._text.insert("1.0", translation)
        self._text.tag_configure("glossary", background=ICE, foreground=INK)
        for term in sorted(terms, key=len)[:3]:
            self._highlight(term)
        self._text.configure(state="disabled")
        self.after_idle(self._autosize)

        # ---- terms ---------------------------------------------------------
        if terms:
            terms_row = ctk.CTkFrame(body, fg_color=CARD, corner_radius=0, height=14)
            terms_row.grid(row=4, column=0, sticky="ew", pady=(5, 0))
            ctk.CTkLabel(
                terms_row, text="术语", font=term_font, text_color=MUTED, height=14
            ).pack(side="left", padx=(0, 6))
            ctk.CTkLabel(
                terms_row,
                text="  ·  ".join(terms[:5]),
                font=term_font,
                text_color=ACCENT,
                height=14,
            ).pack(side="left")

    # -- behaviour -----------------------------------------------------------
    def _highlight(self, term: str) -> None:
        if not term:
            return
        start = "1.0"
        length = tkinter.IntVar()
        while True:
            pos = self._text.search(
                term, start, stopindex="end", nocase=True, count=length
            )
            if not pos or length.get() <= 0:
                break
            end = f"{pos}+{length.get()}c"
            self._text.tag_add("glossary", pos, end)
            start = end

    def _autosize(self) -> None:
        try:
            self._text.update_idletasks()
            result = self._text.count("1.0", "end-1c", "displaylines")
            if isinstance(result, tuple):
                lines = int(result[0]) + 1 if result else 1
            else:
                lines = int(result or 0) + 1
            if lines > 0 and lines != int(self._text.cget("height")):
                self._text.configure(height=lines)
        except Exception:
            pass

    def _copy(self) -> None:
        if self._copy_callback is not None:
            self._copy_callback(self._translation)
        self._copy_button.configure(text="已复制", text_color=ACCENT)
        self.after(
            1100,
            lambda: self._copy_button.configure(text="复制", text_color=MUTED),
        )

    def set_content_width(self, px: int) -> None:
        px = max(240, int(px))
        if abs(px - self._width_hint) < 6:
            return
        self._width_hint = px
        logical = max(180, int(self._reverse_widget_scaling(px - 4)))
        self._source_label.configure(wraplength=logical)
        lines = estimate_wrap_lines(self._translation, self._dst_font, px - 8, 8)
        self._text.configure(height=lines)
        self.after_idle(self._autosize)


# ---------------------------------------------------------------------------
# Channel panel
# ---------------------------------------------------------------------------
class ChannelPanel(ctk.CTkFrame):
    """Slim header + scrolling transcript list."""

    def __init__(
        self,
        master,
        *,
        number: str,
        title: str,
        subtitle: str,
        accent: str,
        fonts: dict[str, ctk.CTkFont],
        copy_callback: Optional[Callable[[str], None]] = None,
        empty_title: str = "",
        empty_sub: str = "",
        on_focus: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(
            master,
            fg_color=CARD,
            border_width=1,
            border_color=LINE,
            corner_radius=12,
        )
        self.number = number
        self.accent = accent
        self.fonts = fonts
        self.copy_callback = copy_callback
        self._card_count = 0
        self._cards: list[TranscriptCard] = []
        self._content_width = 600
        self._level = 0.0

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=82)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(2, weight=1)

        dot = ctk.CTkFrame(header, fg_color=accent, width=7, height=7, corner_radius=4)
        dot.grid(row=0, column=0, padx=(14, 8), pady=1)
        dot.grid_propagate(False)

        ctk.CTkLabel(header, text=number, font=fonts["meta"], text_color=MUTED).grid(
            row=0, column=1, padx=(0, 8)
        )

        ctk.CTkLabel(
            header, text=title, font=fonts["panel_title"], text_color=INK
        ).grid(row=0, column=2, sticky="w")

        self._subtitle_label = ctk.CTkLabel(
            header, text=subtitle, font=fonts["panel_sub"], text_color=MUTED
        )
        self._subtitle_label.grid(
            row=1, column=2, columnspan=3, sticky="w", pady=(0, 8)
        )

        self._meter = tkinter.Canvas(
            header, width=54, height=12, bg=CARD, highlightthickness=0, bd=0
        )
        self._meter.grid(row=0, column=4, padx=(4, 10))
        self._draw_meter()

        self._focus_button = ctk.CTkButton(
            header,
            text="专注",
            width=68,
            height=32,
            corner_radius=5,
            border_width=0,
            fg_color="transparent",
            hover_color=ICE,
            text_color=MUTED,
            font=fonts["meta"],
            command=on_focus,
        )
        self._focus_button.grid(row=0, column=5, padx=(0, 12))

        rule = ctk.CTkFrame(self, fg_color=LINE_SOFT, height=1, corner_radius=0)
        rule.grid(row=1, column=0, sticky="ew")

        self._stream = ctk.CTkScrollableFrame(
            self,
            fg_color=BACKGROUND,
            corner_radius=0,
            border_width=0,
            scrollbar_button_color="#DCE4EF",
            scrollbar_button_hover_color=ACCENT,
        )
        self._stream.grid(row=2, column=0, sticky="nsew", padx=2, pady=2)

        self._empty: Optional[ctk.CTkFrame] = ctk.CTkFrame(
            self._stream, fg_color="transparent"
        )
        self._empty.pack(anchor="center", pady=(64, 24), padx=16)
        ctk.CTkLabel(
            self._empty,
            text=empty_title,
            font=fonts["empty"],
            text_color=MUTED,
        ).pack(anchor="center")
        ctk.CTkLabel(
            self._empty,
            text=empty_sub,
            wraplength=340,
            justify="center",
            font=fonts["empty_sub"],
            text_color=MUTED,
        ).pack(anchor="center", pady=(5, 0))

    # -- content -------------------------------------------------------------
    def add_card(
        self,
        *,
        source: str,
        translation: str,
        terms: list[str],
        src_lang: str,
        dst_lang: str,
        timestamp: str,
        latency: float,
        channel_tag: str,
        engine: str = "",
        max_cards: int = 40,
    ) -> None:
        if self._empty is not None:
            self._empty.destroy()
            self._empty = None

        at_bottom = self._stream._parent_canvas.yview()[1] >= 0.98
        self._card_count += 1
        card = TranscriptCard(
            self._stream,
            index=self._card_count,
            timestamp=timestamp,
            source=source,
            translation=translation,
            terms=terms,
            src_font=(
                self.fonts["source_zh"] if src_lang == "zh" else self.fonts["source_en"]
            ),
            dst_font=(
                self.fonts["body_zh"] if dst_lang == "zh" else self.fonts["body_en"]
            ),
            meta_font=self.fonts["meta"],
            term_font=self.fonts["term"],
            latency=latency,
            tag_text=(
                "仅术语对照 · 翻译不可用"
                if engine == "term-align"
                else "英文译文"
                if dst_lang == "en"
                else "中文译文"
            ),
            copy_callback=self.copy_callback,
            width_hint=self._content_width,
        )
        card.pack(fill="x", padx=10, pady=(10, 0))
        self._cards.append(card)

        while len(self._cards) > max_cards:
            oldest = self._cards.pop(0)
            oldest.destroy()

        if at_bottom:
            self.after(40, self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        try:
            self._stream._parent_canvas.yview_moveto(1.0)  # type: ignore[attr-defined]
        except Exception:
            pass

    def set_content_width(self, px: int) -> None:
        px = max(240, int(px))
        if abs(px - self._content_width) < 6:
            return
        self._content_width = px
        for card in self._cards:
            card.set_content_width(px)

    def set_level(self, value: float) -> None:
        self._level = max(0.0, min(1.0, float(value)))
        self._draw_meter()

    def _draw_meter(self) -> None:
        canvas = self._meter
        canvas.delete("all")
        bars = 9
        width = 54
        height = 12
        gap = 2
        bar_w = max(2, (width - (bars - 1) * gap) / bars)
        active = int(round(self._level * bars))
        for i in range(bars):
            x0 = i * (bar_w + gap)
            x1 = x0 + bar_w
            level_h = 3 + (height - 3) * (i / max(1, bars - 1))
            y0 = height - level_h
            color = self.accent if i < active else "#E4EAF3"
            canvas.create_rectangle(x0, y0, x1, height, fill=color, outline="")

    def set_subtitle(self, text: str) -> None:
        self._subtitle_label.configure(text=text)

    def set_focus_mode(self, focused: bool) -> None:
        self._focus_button.configure(
            text="双栏" if focused else "专注",
            text_color=ACCENT if focused else FAINT,
        )


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class StemTransWindow(ctk.CTk):
    """A reading workspace and a separate settings page; all model work is external."""

    VERSION = "1.1.0"
    LANGUAGES = {"中文 → 英文": "zh", "英文 → 中文": "en", "中英自动互译": "auto"}

    def __init__(self, controller=None, settings=None, glossary=None, geometry=None):
        super().__init__(fg_color=BACKGROUND)
        self.controller, self.settings, self.glossary = (
            controller,
            settings or {},
            glossary,
        )
        self._queue = queue.Queue()
        self._closing = self._recording = self._transitioning = self._settings_open = (
            False
        )
        self._focus = None
        self._mode = "offline"
        self._session = 0
        self._record_started = 0.0
        self._blocked = False
        self._processing = ""
        self._pending = 0
        self._pending_total = 0
        self._last_level = 0.0
        self._jobs = []
        self.title("StemTrans · 实时双语同传")
        self.geometry(
            geometry or self.settings.get("ui", {}).get("geometry", "1280x800")
        )
        self.minsize(960, 640)
        self.protocol("WM_DELETE_WINDOW", self._on_close_request)
        self.bind("<F5>", lambda e: self._toggle_recording())
        self.bind("<Control-Shift-C>", lambda e: self._copy_latest())
        self.bind("<Escape>", lambda e: self.set_settings_open(False))
        self._build_fonts()
        self._build()
        self.apply_settings()
        self._jobs.append(self.after(45, self._pump))
        self._jobs.append(self.after(300, self._tick))
        self._jobs.append(self.after(220, self._relayout))

    def _build_fonts(self):
        cjk = _pick_family(("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"))
        latin = _pick_family(("Segoe UI", "Arial"))
        sizes = {
            "brand": 24,
            "title": 26,
            "panel_title": 18,
            "panel_sub": 13,
            "source_zh": 17,
            "source_en": 17,
            "body_zh": 25,
            "body_en": 26,
            "meta": 12,
            "term": 13,
            "label": 15,
            "button": 15,
            "footer": 13,
            "empty": 21,
            "empty_sub": 15,
        }
        self.fonts = {
            key: ctk.CTkFont(
                family=latin if key in ("brand", "body_en", "source_en") else cjk,
                size=size,
                weight="bold"
                if key
                in ("brand", "title", "panel_title", "body_zh", "body_en", "button")
                else "normal",
            )
            for key, size in sizes.items()
        }

    def _label(self, parent, text, font="label", color=INK, **kw):
        return ctk.CTkLabel(
            parent, text=text, font=self.fonts[font], text_color=color, **kw
        )

    def _button(self, parent, text, command, primary=False, **kw):
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            height=40,
            corner_radius=8,
            font=self.fonts["button"],
            fg_color=NAVY if primary else ICE,
            hover_color=NAVY_HOVER if primary else LINE,
            text_color=CARD if primary else INK,
            **kw,
        )

    def _menu(self, parent, variable, values, command, width=260):
        return ctk.CTkOptionMenu(
            parent,
            variable=variable,
            values=values,
            command=command,
            width=width,
            height=40,
            corner_radius=7,
            fg_color=ICE,
            button_color=ICE,
            button_hover_color=LINE,
            text_color=INK,
            text_color_disabled=MUTED,
            font=self.fonts["label"],
            dropdown_font=self.fonts["label"],
            dropdown_fg_color=CARD,
            dropdown_text_color=INK,
            dropdown_hover_color=ICE,
        )

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        top = ctk.CTkFrame(self, fg_color=NAVY, corner_radius=0)
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(1, weight=1)
        self._label(top, "StemTrans", "brand", CARD).grid(
            row=0, column=0, padx=24, pady=16
        )
        self._label(top, "实时双语同传", color=TOPBAR_TEXT).grid(
            row=0, column=1, sticky="w"
        )
        nav = ctk.CTkFrame(top, fg_color="transparent")
        nav.grid(row=0, column=2, padx=24)
        self.session_button = self._button(
            nav, "会话", lambda: self.set_settings_open(False), width=88
        )
        self.session_button.pack(side="left", padx=4)
        self.settings_button = self._button(
            nav, "设置", lambda: self.set_settings_open(True), width=88
        )
        self.settings_button.pack(side="left", padx=4)

        self.controls = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0)
        self.controls.grid(row=1, column=0, sticky="ew")
        self.controls.grid_columnconfigure(2, weight=1)
        self.mode_var = tkinter.StringVar(value="单麦对话")
        self.mode_menu = self._menu(
            self.controls,
            self.mode_var,
            ["单麦对话", "线上会议"],
            self._select_mode,
            width=150,
        )
        self.mode_menu.grid(row=0, column=0, padx=(24, 12), pady=(18, 6))
        self.language_var = tkinter.StringVar(value="中文 → 英文")
        self.language_menu = self._menu(
            self.controls,
            self.language_var,
            list(self.LANGUAGES),
            self._select_language,
            width=200,
        )
        self.language_menu.grid(row=0, column=1, pady=(18, 6))
        self.timer_label = self._label(top, "00:00", "label", TOPBAR_TEXT)
        self.timer_label.grid(row=0, column=3, padx=(0, 16))
        self.start_button = self._button(
            top, "开始监听  F5", self._toggle_recording, width=160
        )
        self.start_button.grid(row=0, column=4, padx=(0, 24))
        self._button(
            self.controls,
            "检查声音设备",
            lambda: self.set_settings_open(True),
            width=160,
        ).grid(row=0, column=4, padx=(12, 24), pady=(18, 6))
        self.direction_label = self._label(
            self.controls, "", "footer", MUTED, anchor="w"
        )
        self.direction_label.grid(
            row=1, column=0, columnspan=5, sticky="ew", padx=24, pady=(0, 14)
        )

        self.workspace = ctk.CTkFrame(self, fg_color=BACKGROUND, corner_radius=0)
        self.workspace.grid(row=2, column=0, sticky="nsew")
        self.workspace.grid_columnconfigure(0, weight=1)
        self.workspace.grid_rowconfigure(2, weight=1)
        self.activity_label = self._label(
            self.workspace, "准备好后，点击「开始监听」", "label", INK, anchor="w"
        )
        self.activity_label.grid(row=0, column=0, sticky="ew", padx=24, pady=(16, 4))
        self.draft_label = self._label(
            self.workspace,
            "",
            "source_zh",
            INK,
            anchor="w",
            justify="left",
            wraplength=1000,
        )
        self.draft_label.grid(row=1, column=0, sticky="ew", padx=24, pady=(4, 8))
        self.draft_label.grid_remove()
        self.body = ctk.CTkFrame(self.workspace, fg_color=BACKGROUND, corner_radius=0)
        self.body.grid(row=2, column=0, sticky="nsew", padx=24, pady=(8, 20))
        self.body.grid_columnconfigure((0, 1), weight=1, uniform="panel")
        self.body.grid_rowconfigure(0, weight=1)
        self.left_panel = ChannelPanel(
            self.body,
            number="",
            title="英文提词",
            subtitle="中文原文 → 英文译文",
            accent=NAVY,
            fonts=self.fonts,
            copy_callback=self.copy_text,
            empty_title="把一句话说完整",
            empty_sub="开始监听后，用中文自然说话。\n停顿约 1 秒，再查看原文和英文译文。\n这里只显示实际识别到的内容。",
            on_focus=lambda: self._toggle_focus("left"),
        )
        self.right_panel = ChannelPanel(
            self.body,
            number="",
            title="中文同传",
            subtitle="英文原文 → 中文译文",
            accent=ACCENT,
            fonts=self.fonts,
            copy_callback=self.copy_text,
            empty_title="听懂对方的意思",
            empty_sub="这里显示英文识别原文与中文译文。\n线上会议请先在设置中选择\n与会议扬声器相同的系统声音设备。",
            on_focus=lambda: self._toggle_focus("right"),
        )
        self._build_settings()
        footer = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0)
        footer.grid(row=3, column=0, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        self.status_label = self._label(
            footer, "", "footer", MUTED, anchor="w", wraplength=820, justify="left"
        )
        self.status_label.grid(row=0, column=0, sticky="ew", padx=24, pady=10)
        self.engine_label = self._label(footer, "等待模型", "footer", MUTED)
        self.engine_label.grid(row=0, column=1, padx=(8, 24), pady=10)

    def _build_settings(self):
        self.drawer = ctk.CTkFrame(self, fg_color=BACKGROUND, corner_radius=0)
        self.drawer.grid(row=2, column=0, sticky="nsew")
        self.drawer.grid_columnconfigure(0, weight=1)
        self.drawer.grid_rowconfigure(2, weight=1)
        self._label(self.drawer, "设置", "title").grid(
            row=0, column=0, sticky="w", padx=30, pady=(24, 4)
        )
        self._label(
            self.drawer,
            "设备、语音和阅读偏好。切换设备或断句时长前，请先停止监听。",
            color=MUTED,
        ).grid(row=1, column=0, sticky="w", padx=30, pady=(0, 16))
        tabs = ctk.CTkTabview(
            self.drawer,
            fg_color=CARD,
            corner_radius=12,
            segmented_button_fg_color=ICE,
            segmented_button_selected_color=NAVY,
            segmented_button_selected_hover_color=NAVY_HOVER,
            segmented_button_unselected_color=ICE,
            text_color=INK,
        )
        tabs.grid(row=2, column=0, sticky="nsew", padx=24, pady=(0, 24))
        tabs._segmented_button.configure(
            font=self.fonts["label"], selected_color=LINE, selected_hover_color=LINE
        )
        self.config_controls = []
        audio = tabs.add("声音与断句")
        voice = tabs.add("语音朗读")
        reading = tabs.add("阅读与窗口")
        for tab in (audio, voice, reading):
            tab.grid_columnconfigure(0, weight=1)
            tab.grid_rowconfigure(0, weight=1)
        audio = ctk.CTkScrollableFrame(audio, fg_color=CARD)
        audio.grid(row=0, column=0, sticky="nsew")
        audio.grid_columnconfigure(1, weight=1)
        self._label(audio, "声音从哪里来", "panel_title").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=18, pady=(18, 16)
        )
        self.mic_var = tkinter.StringVar(value="正在查找麦克风…")
        self.loop_var = tkinter.StringVar(value="正在查找系统声音…")
        for row, title, var, callback in [
            (1, "麦克风", self.mic_var, self._on_mic_selected),
            (3, "系统声音", self.loop_var, self._on_loop_selected),
        ]:
            self._label(audio, title).grid(
                row=row, column=0, sticky="w", padx=18, pady=8
            )
            menu = self._menu(audio, var, [var.get()], callback, width=520)
            menu.grid(row=row, column=1, sticky="ew", padx=18, pady=8)
            self.config_controls.append(menu)
            if row == 1:
                self.mic_menu = menu
            else:
                self.loop_menu = menu
        self._label(audio, "选择你实际说话时使用的麦克风。", "footer", MUTED).grid(
            row=2, column=1, sticky="w", padx=18
        )
        self._label(
            audio,
            "仅线上会议使用；请选择会议正在播放声音的扬声器或耳机。",
            "footer",
            MUTED,
        ).grid(row=4, column=1, sticky="w", padx=18)
        self.refresh_button = self._button(
            audio, "刷新设备", lambda: self._notify("refresh_devices"), width=120
        )
        self.refresh_button.grid(row=5, column=1, sticky="w", padx=18, pady=(12, 24))
        self.config_controls.append(self.refresh_button)
        self._label(audio, "停顿多久算一句说完", "panel_title").grid(
            row=6, column=0, columnspan=2, sticky="w", padx=18, pady=8
        )
        self.pause_var = tkinter.StringVar(value="自然对话 · 1.1 秒")
        self.pause_menu = self._menu(
            audio,
            self.pause_var,
            ["紧凑发言 · 0.7 秒", "自然对话 · 1.1 秒", "从容表达 · 1.6 秒"],
            self._select_pause,
        )
        self.pause_menu.grid(row=7, column=1, sticky="w", padx=18, pady=8)
        self.config_controls.append(self.pause_menu)
        self._label(
            audio,
            "说话中常有思考停顿时，选择「从容表达」。连续发言最长约 24 秒一段。",
            "footer",
            MUTED,
        ).grid(row=8, column=0, columnspan=2, sticky="w", padx=18, pady=12)

        voice_content = ctk.CTkScrollableFrame(voice, fg_color=CARD)
        voice_content.grid(row=0, column=0, sticky="nsew")
        self._label(
            voice_content, "默认只显示译文，安静地听你说完", "panel_title"
        ).pack(anchor="w", padx=24, pady=(24, 16))
        self.tts_var = tkinter.BooleanVar(value=False)
        self.tts_switch = ctk.CTkSwitch(
            voice_content,
            text="自动朗读英文译文（仅本次运行）",
            variable=self.tts_var,
            command=self._on_tts_toggle,
            font=self.fonts["label"],
            text_color=INK,
            progress_color=NAVY,
            button_color=CARD,
            switch_width=40,
            switch_height=22,
        )
        self.tts_switch.pack(anchor="w", padx=24, pady=12)
        self._label(
            voice_content,
            "开启后，整个朗读期间及结束后的 0.35 秒都会暂停收音。\n这段时间内说的话不会被记录；请等界面恢复「正在收音」后再说。\n如果你需要连续讲话，请保持关闭。每次重新打开软件都会关闭自动朗读。",
            color=MUTED,
            justify="left",
            wraplength=760,
        ).pack(anchor="w", padx=24, pady=12)

        reading_content = ctk.CTkScrollableFrame(reading, fg_color=CARD)
        reading_content.grid(row=0, column=0, sticky="nsew")
        self._label(reading_content, "让阅读更轻松", "panel_title").pack(
            anchor="w", padx=24, pady=(24, 16)
        )
        self.text_size_var = tkinter.StringVar(value="标准")
        self._label(reading_content, "字幕字号").pack(anchor="w", padx=24, pady=(8, 4))
        self._menu(
            reading_content,
            self.text_size_var,
            ["标准", "大字", "特大"],
            self._set_text_size,
        ).pack(anchor="w", padx=24, pady=(0, 20))
        self.pin_var = tkinter.BooleanVar(value=False)
        ctk.CTkSwitch(
            reading_content,
            text="窗口始终置顶",
            variable=self.pin_var,
            command=self._on_pin_toggle,
            font=self.fonts["label"],
            text_color=INK,
            progress_color=NAVY,
        ).pack(anchor="w", padx=24, pady=12)
        self.opacity_value_label = self._label(reading_content, "窗口不透明度 97%")
        self.opacity_value_label.pack(anchor="w", padx=24, pady=(20, 8))
        self.opacity_slider = ctk.CTkSlider(
            reading_content,
            from_=0.5,
            to=1.0,
            number_of_steps=50,
            width=340,
            fg_color=LINE,
            progress_color=ACCENT,
            button_color=NAVY,
            command=lambda v: self.set_opacity(v, True),
        )
        self.opacity_slider.pack(anchor="w", padx=24, pady=8)
        self._label(
            reading_content,
            "快捷键：F5 开始 / 停止监听，Ctrl + Shift + C 复制最新译文，Esc 返回会话。",
            "footer",
            MUTED,
        ).pack(anchor="w", padx=24, pady=24)
        self.drawer.grid_remove()

    def apply_settings(self):
        ui = self.settings.get("ui", {})
        language = self.settings.get("asr", {}).get("language_single", "zh")
        self.language_var.set(
            next(
                (label for label, value in self.LANGUAGES.items() if value == language),
                "中文 → 英文",
            )
        )
        self.set_mode(ui.get("mode", "offline"), notify=False)
        self.pin_var.set(bool(ui.get("always_on_top", True)))
        self.attributes("-topmost", self.pin_var.get())
        self.set_opacity(float(ui.get("opacity", 0.97)))
        self.tts_var.set(bool(self.settings.get("tts", {}).get("enabled", False)))
        ms = self.settings.get("audio", {}).get("vad", {}).get("min_silence_ms", 1100)
        self.pause_var.set(
            {
                700: "紧凑发言 · 0.7 秒",
                1100: "自然对话 · 1.1 秒",
                1600: "从容表达 · 1.6 秒",
            }.get(ms, f"自定义 · {ms / 1000:.1f} 秒")
        )
        self._set_text_size(ui.get("text_size", "标准"))
        self.set_settings_open(False)

    def _notify(self, name, *args):
        if self.controller is not None:
            callback = getattr(self.controller, name, None)
            if callable(callback):
                callback(*args)

    def set_settings_open(self, open_):
        self._settings_open = bool(open_)
        if open_:
            self.workspace.grid_remove()
            self.drawer.grid()
            self.controls.grid_remove()
        else:
            self.drawer.grid_remove()
            self.workspace.grid()
            self.controls.grid()
        self.settings_button.configure(
            fg_color=CARD if open_ else NAVY_HOVER, text_color=NAVY if open_ else CARD
        )
        self.session_button.configure(
            fg_color=CARD if not open_ else NAVY_HOVER,
            text_color=NAVY if not open_ else CARD,
        )

    def _toggle_settings(self):
        self.set_settings_open(not self._settings_open)

    def _toggle_recording(self):
        if self._transitioning:
            return
        self._notify("stop_listening" if self._recording else "start_listening")

    def _select_mode(self, label):
        self.set_mode("online" if label == "线上会议" else "offline")

    def set_mode(self, mode, notify=True):
        self._mode = "online" if mode == "online" else "offline"
        self.mode_var.set("线上会议" if self._mode == "online" else "单麦对话")
        self._focus = None
        self._update_direction()
        self._layout_panels()
        self._lock_controls()
        if notify:
            self._notify("set_mode", self._mode)

    def _select_language(self, label):
        self._focus = None
        self._update_direction()
        self._layout_panels()
        self._notify("set_language", self.LANGUAGES[label])

    def _update_direction(self):
        if self._mode == "online":
            text = "麦克风：中文 → 英文提词    /    系统声音：英文 → 中文同传"
            self.left_panel.set_subtitle("麦克风 · 中文 → 英文")
            self.right_panel.set_subtitle("系统声音 · 英文 → 中文")
        else:
            text = (
                "只听麦克风 · "
                + self.language_var.get()
                + "。原文是识别结果，译文是根据原文生成的翻译。"
            )
            self.left_panel.set_subtitle("麦克风 · 中文 → 英文")
            self.right_panel.set_subtitle("麦克风 · 英文 → 中文")
        self.direction_label.configure(text=text)

    def _layout_panels(self):
        single = self._focus
        if not single and self._mode == "offline":
            lang = self.LANGUAGES.get(self.language_var.get(), "zh")
            single = {"zh": "left", "en": "right"}.get(lang)
        self.left_panel.grid_remove()
        self.right_panel.grid_remove()
        if single:
            panel = self.left_panel if single == "left" else self.right_panel
            panel.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=0)
        else:
            self.left_panel.grid(
                row=0, column=0, columnspan=1, sticky="nsew", padx=(0, 8)
            )
            self.right_panel.grid(
                row=0, column=1, columnspan=1, sticky="nsew", padx=(8, 0)
            )
        self.left_panel.set_focus_mode(self._focus == "left")
        self.right_panel.set_focus_mode(self._focus == "right")
        can_focus = self._mode == "online" or self.language_var.get() == "中英自动互译"
        for panel in (self.left_panel, self.right_panel):
            panel._focus_button.configure(state="normal" if can_focus else "disabled")

    def _toggle_focus(self, which):
        self._focus = None if self._focus == which else which
        self._layout_panels()

    def _lock_controls(self):
        busy = self._recording or self._transitioning
        for control in [self.mode_menu, self.language_menu] + self.config_controls:
            control.configure(state="disabled" if busy else "normal")
        if self._mode == "online":
            self.language_menu.configure(state="disabled")
        else:
            self.loop_menu.configure(state="disabled")

    def _on_mic_selected(self, value):
        self._notify("set_mic_device", value)

    def _on_loop_selected(self, value):
        self._notify("set_loopback_device", value)

    def _select_pause(self, value):
        self._notify(
            "set_pause",
            {
                "紧凑发言 · 0.7 秒": 700,
                "自然对话 · 1.1 秒": 1100,
                "从容表达 · 1.6 秒": 1600,
            }[value],
        )

    def _on_pin_toggle(self):
        self.attributes("-topmost", self.pin_var.get())
        self._notify("set_topmost", bool(self.pin_var.get()))

    def _on_tts_toggle(self):
        enabled = bool(self.tts_var.get())
        self._notify("set_tts_enabled", enabled)
        self.set_status(
            "自动朗读已开启：整个朗读期间暂停收音，请等朗读结束再说。"
            if enabled
            else "自动朗读已关闭。",
            "warn" if enabled else "info",
        )

    def _set_text_size(self, value):
        self.text_size_var.set(value)
        offset = {"标准": 0, "大字": 4, "特大": 8}.get(value, 0)
        for key, base in [
            ("body_zh", 25),
            ("body_en", 26),
            ("source_zh", 17),
            ("source_en", 17),
        ]:
            self.fonts[key].configure(size=base + offset)
        self.settings.setdefault("ui", {})["text_size"] = value
        for panel in (self.left_panel, self.right_panel):
            for card in panel._cards:
                card.after_idle(card._autosize)

    def set_opacity(self, value, notify=False):
        value = max(0.5, min(1.0, float(value)))
        self.opacity_slider.set(value)
        self.attributes("-alpha", value)
        self.opacity_value_label.configure(text=f"窗口不透明度 {value * 100:.0f}%")
        if notify:
            self._notify("set_opacity", value)

    def set_devices(
        self, mic_devices, loopback_devices, mic_value="", loopback_value=""
    ):
        for menu, var, devices, selected in [
            (self.mic_menu, self.mic_var, mic_devices, mic_value),
            (self.loop_menu, self.loop_var, loopback_devices, loopback_value),
        ]:
            devices = list(dict.fromkeys(devices)) or ["未找到设备"]
            menu.configure(values=devices)
            var.set(selected if selected in devices else devices[0])

    def set_network_source(self, source):
        pass  # Download mirrors are unrelated to live, fully local inference.

    def begin_session(self, session):
        self._session = session
        self._processing = ""
        self._blocked = False
        self.draft_label.grid_remove()
        self.set_settings_open(False)

    def add_segment(
        self,
        *,
        column,
        source,
        translation,
        src_lang,
        dst_lang,
        terms=None,
        latency=0.0,
        timestamp=None,
        channel_tag="麦克风",
        engine="",
    ):
        panel = self.left_panel if column == "left" else self.right_panel
        panel.add_card(
            source=source,
            translation=translation,
            terms=terms or [],
            src_lang=src_lang,
            dst_lang=dst_lang,
            timestamp=time.strftime(
                "%H:%M:%S", time.localtime(timestamp or time.time())
            ),
            latency=latency,
            channel_tag=channel_tag,
            engine=engine,
            max_cards=int(self.settings.get("ui", {}).get("max_cards", 40)),
        )
        self._latest = translation
        self.draft_label.grid_remove()

    def _copy_latest(self):
        self.copy_text(getattr(self, "_latest", ""))

    def copy_text(self, text):
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.set_status("译文已复制。", "ok")
            self._notify("copy_text", text)

    def set_status(self, message, kind="info"):
        self.status_label.configure(
            text=message.replace("\n", " "),
            text_color={"error": RED, "warn": PALETTE["amber"]}.get(kind, MUTED),
        )

    def set_engine_state(self, state):
        if state.startswith("READY"):
            text = "本地模型已就绪"
        elif "LOADING" in state:
            text = "正在加载本地模型…"
        elif "ERROR" in state or "MISSING" in state or "TERM-ALIGN" in state:
            text = "模型不可用，请检查提示"
        else:
            text = "正在准备…" if state != "ASR READY" else "识别已就绪，正在准备翻译…"
        self.engine_label.configure(text=text)

    def set_transition(self, text):
        self._transitioning = True
        self.start_button.configure(state="disabled", text="请稍候…")
        self._lock_controls()
        self.activity_label.configure(text=text)

    def set_recording(self, recording):
        self._transitioning = False
        changed = self._recording != bool(recording)
        self._recording = bool(recording)
        if recording and changed:
            self._record_started = time.monotonic()
        self.start_button.configure(
            state="normal",
            text="停止监听  F5" if recording else "开始监听  F5",
            fg_color=RED if recording else CARD,
            text_color=CARD if recording else NAVY,
            hover_color="#923D39" if recording else ICE,
        )
        if not recording and self._pending_total:
            self.start_button.configure(state="disabled", text="正在处理尾句…")
        self._lock_controls()
        self._update_activity()

    def set_elapsed(self, seconds):
        self._record_started = time.monotonic() - seconds
        self.timer_label.configure(
            text=f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"
        )

    def set_level(self, channel, level):
        (self.left_panel if channel == "mic" else self.right_panel).set_level(level)
        if level > 0.15:
            self._last_level = time.monotonic()

    def _update_activity(self):
        if self._transitioning:
            return
        if self._blocked:
            text = "正在朗读，收音已暂停。请等朗读结束后再说。"
        elif self._recording:
            text = "正在收音 · 请自然说话，停顿后会生成译文"
            if time.monotonic() - max(self._last_level, self._record_started) > 8:
                text = "正在监听，但没有检测到明显声音。请检查麦克风选择和系统静音。"
        else:
            text = (
                "已停止收音"
                if self._record_started
                else "选择场景和翻译方向，然后点击「开始监听」"
            )
        if self._processing:
            text += "    " + self._processing
        if self._pending:
            text += f"    还有 {self._pending} 段等待处理"
        self.activity_label.configure(
            text=text, text_color=RED if self._blocked else INK
        )

    def toast(self, message):
        self.set_status(message, "ok")

    def post(self, event):
        self._queue.put(event)

    def _handle_event(self, event):
        if "session" in event and event["session"] != self._session:
            return
        kind = event.get("type")
        if kind == "segment":
            self.add_segment(**event["payload"])
        elif kind == "draft":
            self.draft_label.configure(
                text="识别原文：" + event["text"] + "\n正在翻译…"
            )
            self.draft_label.grid()
            self._processing = "正在翻译刚才的发言…"
        elif kind == "processing":
            self._processing = event.get("text", "")
        elif kind == "pending":
            self._pending = event.get("count", 0)
            self._pending_total = event.get("total", self._pending)
            if not self._transitioning:
                self.set_recording(self._recording)
        elif kind == "gate":
            self._blocked = event.get("blocked", False)
        elif kind == "overload":
            self._notify(
                "stop_listening", "处理速度跟不上，本段未保存。请等待已有语音处理完成。"
            )
        elif kind == "capture_failure":
            self._notify("stop_listening")
        elif kind == "status":
            self.set_status(event.get("message", ""), event.get("kind", "info"))
        elif kind == "engine":
            self.set_engine_state(event.get("state", ""))
        elif kind == "recording":
            self.set_recording(event.get("value", False))
        elif kind == "level":
            self.set_level(event["channel"], event["level"])
        elif kind == "devices":
            self.set_devices(
                event.get("mic", []),
                event.get("loopback", []),
                event.get("mic_value", ""),
                event.get("loopback_value", ""),
            )
        elif kind == "mode":
            self.set_mode(event["value"], notify=False)
        elif kind == "toast":
            self.toast(event["message"])

    def _pump(self):
        for _ in range(150):
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle_event(event)
            except Exception as exc:
                self.set_status(f"界面更新失败：{exc}", "error")
        if not self._closing:
            self._pump_job = self.after(45, self._pump)

    def _tick(self):
        if self._recording:
            elapsed = int(time.monotonic() - self._record_started)
            self.timer_label.configure(text=f"{elapsed // 60:02d}:{elapsed % 60:02d}")
        self._update_activity()
        if not self._closing:
            self._tick_job = self.after(300, self._tick)

    def _relayout(self):
        for panel in (self.left_panel, self.right_panel):
            if panel.winfo_ismapped() and panel.winfo_width() > 100:
                panel.set_content_width(max(240, panel.winfo_width() - 92))
        logical = int(self._reverse_window_scaling(self.winfo_width()))
        self.draft_label.configure(wraplength=max(500, logical - 64))
        self.activity_label.configure(wraplength=max(500, logical - 64))
        self.status_label.configure(wraplength=max(400, logical - 280))
        if not self._closing:
            self._relayout_job = self.after(220, self._relayout)

    def _on_close_request(self):
        if self._closing:
            return
        self._closing = True
        for name in ("_pump_job", "_tick_job", "_relayout_job"):
            job = getattr(self, name, None)
            if job:
                self.after_cancel(job)
        if self.controller is not None:
            self._notify("shutdown")
        else:
            self.destroy()

    def destroy(self):
        self._closing = True
        # Includes card autosize/copy timers and CTk's DPI polling, not just
        # the three recurring window jobs. Avoid orphan Tcl callbacks.
        for job in self.tk.call("after", "info"):
            self.after_cancel(job)
        super().destroy()
