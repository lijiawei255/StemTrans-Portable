#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Capture the *real* StemTrans window into ``assets/ui_live_capture.png``.

Why this script exists
----------------------
README mock-ups age badly: the code changes, the picture does not, and the
result looks fake.  This harness therefore boots the production
``ui.main_window.StemTransWindow`` (no model loading, no audio device), pushes
three realistic mechatronics interpretation turns into each column through the
same public API used at runtime, waits for the compositor, then grabs the
actual screen pixels of the actual window.

Windows specifics handled here
------------------------------
* Per-monitor-v2 DPI awareness is set **before** Tk is imported, otherwise a
  125 % / 150 % display produces a cropped or offset capture.
* ``DWMWA_EXTENDED_FRAME_BOUNDS`` is preferred over ``GetWindowRect`` because
  the latter includes the invisible DWM resize/shadow border (a few pixels of
  desktop would leak into the PNG).
* ``ImageGrab.grab(..., all_screens=True)`` supports negative coordinates on
  multi-monitor setups.
* The window is forced to 100 % opacity and raised before the grab, and the
  result is validated (size, contrast) so a locked/occluded desktop fails loudly
  instead of committing a black rectangle.

Usage
-----
    python capture_readme_screenshots.py
    python capture_readme_screenshots.py --check          # layout only
    python capture_readme_screenshots.py --keep-open      # manual inspection
    python capture_readme_screenshots.py --output assets/ui_live_capture.png
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
import traceback
from ctypes import wintypes

APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)
os.environ.setdefault("PYTHONUTF8", "1")
sys.path.insert(0, APP_DIR)


# ---------------------------------------------------------------------------
# DPI awareness MUST be configured before tkinter/customtkinter are imported
# ---------------------------------------------------------------------------
def enable_dpi_awareness() -> str:
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return "per-monitor"
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "none"


DPI_MODE = enable_dpi_awareness()

from PIL import Image, ImageGrab  # noqa: E402

from core.inference_engine import Glossary  # noqa: E402
from ui.main_window import StemTransWindow  # noqa: E402


# ---------------------------------------------------------------------------
# Demo transcript: three turns per column, all real mechatronics material
# ---------------------------------------------------------------------------
LEFT_TURNS = [
    {
        "source": "我们先在 STM32 上用 SPI 读取外部 ADC，采样率设到 20 kHz，并加了信号隔离来压制共模干扰。",
        "translation": "We read the external ADC over SPI on the STM32 at a 20 kHz sampling rate, and added signal isolation to suppress common-mode interference.",
        "terms": [
            "STM32",
            "SPI",
            "ADC",
            "signal isolation",
            "common-mode interference",
            "sampling rate",
        ],
        "latency": 0.9,
        "time": (10, 42, 7),
    },
    {
        "source": "PID 控制器仍然有大约 12% 的超调，我建议减小 Kp 并加入前馈项来缩短调节时间。",
        "translation": "The PID controller still shows about 12% overshoot, so I suggest lowering Kp and adding a feedforward term to shorten the settling time.",
        "terms": ["PID controller", "overshoot", "feedforward", "settling time"],
        "latency": 1.1,
        "time": (10, 42, 21),
    },
    {
        "source": "WAAM 焊缝出现未熔合缺陷，可能是因为热输入和送丝速度不匹配。",
        "translation": "The lack-of-fusion defect in the WAAM weld seam is likely caused by a mismatch between heat input and wire feed speed.",
        "terms": [
            "WAAM",
            "weld seam",
            "lack-of-fusion defect",
            "heat input",
            "wire feed speed",
        ],
        "latency": 1.3,
        "time": (10, 42, 34),
    },
]

RIGHT_TURNS = [
    {
        "source": "How do you keep the SPI clock clean and the jitter low when the ADC front end is galvanically isolated from the MCU?",
        "translation": "当 ADC 前端与 MCU 之间采用电气隔离时，你们如何保证 SPI 时钟干净、抖动足够低？",
        "terms": ["SPI", "ADC", "galvanic isolation", "电气隔离"],
        "latency": 1.2,
        "time": (10, 42, 12),
    },
    {
        "source": "Have you tried a notch filter for the resonance, or is most of the overshoot coming from integrator windup in the outer position loop?",
        "translation": "你们试过用陷波器抑制机械谐振吗？还是说，超调主要来自外环位置环的积分饱和？我们上次也遇到过。",
        "terms": [
            "notch filter",
            "resonance",
            "integrator windup",
            "陷波器",
            "积分饱和",
        ],
        "latency": 1.4,
        "time": (10, 42, 26),
    },
    {
        "source": "We will run a modal analysis first, then check the impedance of the force sensor and the dynamic stiffness of its mounting bracket before the next print.",
        "translation": "我们会先做模态分析，然后检查力传感器的阻抗，以及安装支架的刚度，确认不会引入额外的共振峰。",
        "terms": ["modal analysis", "impedance", "force sensor", "模态分析", "阻抗"],
        "latency": 1.5,
        "time": (10, 42, 39),
    },
]


def _work_area() -> wintypes.RECT:
    rect = wintypes.RECT()
    try:
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
    except Exception:
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, 1920, 1080
    return rect


def _visible_frame(hwnd: int) -> tuple[int, int, int, int]:
    """Visible window rectangle, excluding the invisible DWM shadow border."""
    rect = wintypes.RECT()
    try:
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd,
            9,
            ctypes.byref(rect),
            ctypes.sizeof(rect),  # EXTENDED_FRAME_BOUNDS
        )
        if result == 0 and rect.right > rect.left and rect.bottom > rect.top:
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def _force_foreground(root: StemTransWindow) -> int:
    hwnd = int(root.frame(), 16)
    try:
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.update()
    except Exception:
        pass
    try:
        ctypes.windll.user32.ShowWindow(hwnd, 5)  # SW_SHOW
        ctypes.windll.user32.BringWindowToTop(hwnd)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    try:
        root.focus_force()
    except Exception:
        pass
    return hwnd


def _settle(root: StemTransWindow, seconds: float) -> None:
    """Pump the Tk event loop for *seconds* so animations actually render."""
    deadline = time.perf_counter() + max(0.0, seconds)
    while time.perf_counter() < deadline:
        root.update_idletasks()
        root.update()
        time.sleep(0.03)


def build_demo(root: StemTransWindow, glossary: Glossary) -> None:
    root.set_mode("online", notify=False)
    root.set_devices(
        ["Microphone (USB Audio Device)", "Microphone Array (Intel® Smart Sound)"],
        ["Speakers (Realtek(R) Audio) [Loopback]", "Headphones (USB) [Loopback]"],
        mic_value="Microphone (USB Audio Device)",
        loopback_value="Speakers (Realtek(R) Audio) [Loopback]",
    )
    root.set_network_source("china")
    root.set_opacity(1.0, notify=False)
    root.set_engine_state("READY")

    now = time.time()
    for index, turn in enumerate(LEFT_TURNS):
        root.add_segment(
            column="left",
            source=turn["source"],
            translation=turn["translation"],
            src_lang="zh",
            dst_lang="en",
            terms=turn["terms"]
            or glossary.match_display_terms(turn["translation"], "en"),
            latency=turn["latency"],
            timestamp=now - (36 - index * 14),
            channel_tag="MIC · ZH→EN",
            engine="qwen2.5-1.5b",
        )
    for index, turn in enumerate(RIGHT_TURNS):
        root.add_segment(
            column="right",
            source=turn["source"],
            translation=turn["translation"],
            src_lang="en",
            dst_lang="zh",
            terms=turn["terms"]
            or glossary.match_display_terms(turn["translation"], "zh"),
            latency=turn["latency"],
            timestamp=now - (31 - index * 14),
            channel_tag="LOOPBACK · EN→ZH",
            engine="qwen2.5-1.5b",
        )

    root.set_recording(True)
    root.set_elapsed(42)
    root.set_level("mic", 0.62)
    root.set_level("loopback", 0.41)
    root.set_status("模型已就绪 · 正在实时同传 / LIVE", "ok")


def validate(image: Image.Image) -> None:
    width, height = image.size
    if width < 900 or height < 600:
        raise RuntimeError(f"capture too small: {width}x{height}")
    gray = image.convert("L")
    low, high = gray.getextrema()
    if high - low < 25:
        raise RuntimeError(
            "capture looks blank (no contrast). Is the desktop locked or the "
            "window occluded?"
        )
    # Check that the actual application frame contains visible content.
    sample = image.crop((0, 0, width, height)).resize((64, 64)).convert("RGB")
    colors = sample.getcolors(maxcolors=64 * 64) or []
    if not colors:
        raise RuntimeError("cannot read captured pixels")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture the real StemTrans window for README.md"
    )
    parser.add_argument(
        "--output",
        default=os.path.join(APP_DIR, "assets", "ui_live_capture.png"),
        help="destination PNG (default: assets/ui_live_capture.png)",
    )
    parser.add_argument(
        "--delay", type=float, default=1.0, help="settle time in seconds"
    )
    parser.add_argument("--max-width", type=int, default=1920)
    parser.add_argument("--check", action="store_true", help="render only, no capture")
    parser.add_argument("--keep-open", action="store_true", help="keep the window open")
    args = parser.parse_args(argv)

    output = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output), exist_ok=True)

    settings = {}
    settings_path = os.path.join(APP_DIR, "config", "settings.json")
    if os.path.isfile(settings_path):
        import json

        with open(settings_path, "r", encoding="utf-8") as handle:
            settings = json.load(handle)
    settings.setdefault("ui", {})["opacity"] = 1.0
    settings["ui"]["always_on_top"] = True

    glossary = Glossary.load(os.path.join(APP_DIR, "config", "glossary.json"))
    if len(glossary) == 0:
        print("[warn] config/glossary.json is empty or missing; terms will not match.")

    work = _work_area()
    # 1440x760 logical is safe on a 1920x1080 / 125 % laptop panel.
    geometry = "1440x760+24+8"

    root = StemTransWindow(
        controller=None, settings=settings, glossary=glossary, geometry=geometry
    )
    callback_errors = []
    root.report_callback_exception = lambda *error: callback_errors.append(error)
    code = 0
    try:
        scale = float(root.winfo_fpixels("1i")) / 96.0
        root.update_idletasks()
        root.update()
        # Shrink only if the frame would not fit the Windows work area.
        max_logical_h = int((work.bottom - 16 - 47) / max(0.8, scale))
        if max_logical_h >= 680 and root.winfo_height() > max_logical_h:
            root.geometry(f"1440x{max_logical_h}+24+8")
            root.update()

        build_demo(root, glossary)
        root.update_idletasks()
        root.update()

        if args.keep_open:
            root.mainloop()
            return 0

        hwnd = _force_foreground(root)
        _settle(root, args.delay)
        if callback_errors:
            raise RuntimeError(f"Tk callback failed: {callback_errors[0]}")

        if args.check:
            print(
                f"[ok] layout {root.winfo_width()}x{root.winfo_height()} "
                f"(scale {scale:.2f}, dpi {DPI_MODE})"
            )
            print(
                f"     cards: left={len(root.left_panel._cards)} "
                f"right={len(root.right_panel._cards)}"
            )
            print(
                f"     viewport: {root.left_panel._stream._parent_canvas.winfo_width()}x"
                f"{root.left_panel._stream._parent_canvas.winfo_height()}"
            )
            return 0

        # Re-assert the foreground right before the grab (the console may steal it).
        _force_foreground(root)
        _settle(root, 0.35)

        left, top, right, bottom = _visible_frame(hwnd)
        if right - left < 800 or bottom - top < 500:
            raise RuntimeError(f"invalid window rect: {(left, top, right, bottom)}")
        if left < -10000 or top < -10000:
            raise RuntimeError("window is minimised / off-screen; cannot capture")

        image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        validate(image)

        if args.max_width and image.width > args.max_width:
            ratio = args.max_width / float(image.width)
            image = image.resize(
                (args.max_width, int(round(image.height * ratio))), Image.LANCZOS
            )

        image.save(output, "PNG", optimize=True)
        print(f"[ok] real window captured -> {output}")
        print(
            f"     {image.width}x{image.height} px  "
            f"(window {root.winfo_width()}x{root.winfo_height()} logical, "
            f"DPI {DPI_MODE}, scale {scale:.2f})"
        )
        print(
            f"     cards: left={len(root.left_panel._cards)} right={len(root.right_panel._cards)}"
        )

        # Second real capture: the standalone settings page, with listening
        # stopped so the device/segmentation controls can actually be edited.
        settings_output = os.path.join(
            os.path.dirname(output), "ui_settings_capture.png"
        )
        root.set_recording(False)
        root.set_status("设备与断句设置会用于下一次监听。", "info")
        root.set_settings_open(True)
        _settle(root, 0.45)
        _force_foreground(root)
        _settle(root, 0.2)
        s_left, s_top, s_right, s_bottom = _visible_frame(int(root.frame(), 16))
        settings_image = ImageGrab.grab(
            bbox=(s_left, s_top, s_right, s_bottom), all_screens=True
        )
        validate(settings_image)
        if args.max_width and settings_image.width > args.max_width:
            ratio = args.max_width / float(settings_image.width)
            settings_image = settings_image.resize(
                (args.max_width, int(round(settings_image.height * ratio))),
                Image.LANCZOS,
            )
        settings_image.save(settings_output, "PNG", optimize=True)
        if callback_errors:
            raise RuntimeError(f"Tk callback failed: {callback_errors[0]}")
        print(f"[ok] settings page captured -> {settings_output}")
    except Exception:
        code = 2
        print("[error] screenshot capture failed:", file=sys.stderr)
        traceback.print_exc()
        print(
            "\nTips:\n"
            "  1. Run this from an unlocked, interactive Windows desktop.\n"
            "  2. Do not cover the window during the 1 s settle time.\n"
            "  3. If the desktop is scaled, keep the process DPI-aware (this script does).\n"
            "  4. On a headless CI machine the capture cannot work — run it locally.\n",
            file=sys.stderr,
        )
    finally:
        try:
            root.attributes("-topmost", False)
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

    return code


if __name__ == "__main__":
    raise SystemExit(main())
