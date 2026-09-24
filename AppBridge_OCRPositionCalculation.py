#!/usr/bin/env python3
"""Coordinate conversion helpers for OCR-driven macOS App bridges.

This module mirrors the App Vision Coordinate Bridge workflow used by Codex:
activate the target app before window screenshots, convert Vision OCR boxes
from normalized image coordinates to screenshot pixels, then map screenshot
pixels into System Events click coordinates with Retina scale and screenshot
decoration offsets accounted for.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image


DEFAULT_RETINA_SCALE = 2.0
CAPTURE_SCALE_CANDIDATES = (DEFAULT_RETINA_SCALE, 1.0)


class VisionBoxLike(Protocol):
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class WindowRect:
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class ImageSize:
    width: float
    height: float


@dataclass(frozen=True)
class OcrClickResult:
    pixel_center: Point
    click_point: Point
    content_offset_px: Point
    scale_x: float
    scale_y: float


class AppWindowGuardError(RuntimeError):
    """Raised when a GUI action cannot prove it still targets the THS window."""


def read_app_window_state(process_name: str) -> dict[str, object] | None:
    """Return the frontmost/minimized/modal state and AX window bounds."""
    script = f'''
tell application "System Events"
  if not (exists process {json.dumps(process_name, ensure_ascii=False)}) then return ""
  tell process {json.dumps(process_name, ensure_ascii=False)}
    if not (exists window 1) then return ""
    set isFront to frontmost
    try
      set isMinimized to value of attribute "AXMinimized" of window 1
    on error
      set isMinimized to false
    end try
    try
      set sheetCount to count of sheets of window 1
    on error
      set sheetCount to 0
    end try
    set modalCount to 0
    repeat with candidateWindow in windows
      try
        if value of attribute "AXModal" of candidateWindow is true then set modalCount to modalCount + 1
      end try
    end repeat
    set p to position of window 1
    set s to size of window 1
    return (isFront as text) & "|" & (isMinimized as text) & "|" & (sheetCount as text) & "|" & (modalCount as text) & "|" & (item 1 of p as text) & "|" & (item 2 of p as text) & "|" & (item 1 of s as text) & "|" & (item 2 of s as text)
  end tell
end tell
'''
    try:
        output = subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    parts = output.split("|")
    if len(parts) != 8:
        return None
    try:
        rect = tuple(int(float(value)) for value in parts[4:8])
        sheet_count = int(float(parts[2]))
        modal_count = int(float(parts[3]))
    except ValueError:
        return None
    if rect[2] <= 0 or rect[3] <= 0:
        return None
    return {
        "frontmost": parts[0].lower() == "true",
        "minimized": parts[1].lower() == "true",
        "sheet_count": sheet_count,
        "modal_count": modal_count,
        "window_rect": rect,
    }


def ensure_app_window_ready(
    app_name: str,
    *,
    process_name: str | None = None,
    timeout_seconds: float = 30.0,
    stable_samples: int = 2,
) -> dict[str, object]:
    """Restore, raise and prove the target window is frontmost and stable."""
    process = process_name or app_name
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    last_rect: tuple[int, int, int, int] | None = None
    stable_count = 0
    open_attempted = False
    while time.monotonic() < deadline:
        activate_app_for_stable_capture(app_name, process_name=process, settle_seconds=0.0)
        state = read_app_window_state(process)
        if state is None and not open_attempted:
            open_attempted = True
            try:
                subprocess.run(
                    ["open", "-a", app_name],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=min(10.0, max(0.1, deadline - time.monotonic())),
                )
            except (OSError, subprocess.SubprocessError):
                pass
            time.sleep(0.3)
            continue
        if state and state["frontmost"] and not state["minimized"]:
            rect = tuple(int(value) for value in state["window_rect"])
            if rect[2] < 400 or rect[3] < 300:
                stable_count = 0
                last_rect = None
            elif rect == last_rect:
                stable_count += 1
            else:
                last_rect = rect
                stable_count = 1
            if stable_count >= max(1, stable_samples):
                return state
        else:
            stable_count = 0
            last_rect = None
        time.sleep(0.2)
    raise AppWindowGuardError(f"{process} window did not become frontmost and stable within {timeout_seconds:.1f}s")


def verify_app_window_state(
    process_name: str,
    *,
    expected_rect: tuple[int, int, int, int] | None = None,
    tolerance: int = 2,
) -> dict[str, object]:
    """Fail closed if focus or bounds changed since the verified capture."""
    state = read_app_window_state(process_name)
    if not state or not state["frontmost"] or state["minimized"]:
        raise AppWindowGuardError(f"{process_name} is not the active, unminimized foreground window")
    if expected_rect is not None:
        actual = state["window_rect"]
        if any(abs(int(a) - int(b)) > tolerance for a, b in zip(actual, expected_rect)):
            raise AppWindowGuardError(
                f"{process_name} window bounds changed; expected={expected_rect} actual={actual}; recapture required"
            )
    return state


def minimize_app_window_if_safe(process_name: str) -> bool:
    """Minimize only an unambiguous THS window without a native AXSheet."""
    state = read_app_window_state(process_name)
    if not state or int(state["sheet_count"]) > 0 or int(state.get("modal_count", 0)) > 0:
        return False
    script = f'''
tell application "System Events"
  tell process {json.dumps(process_name, ensure_ascii=False)}
    if exists window 1 then
      set value of attribute "AXMinimized" of window 1 to true
      return true
    end if
  end tell
end tell
return false
'''
    try:
        output = subprocess.run(
            ["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return output.lower() == "true"


def activate_app_for_stable_capture(
    app_name: str,
    *,
    process_name: str | None = None,
    settle_seconds: float = 0.3,
) -> None:
    """Make the target app frontmost so screencapture uses active-window extents."""
    process = process_name or app_name
    script = (
        f'tell application "{app_name}" to activate\n'
        "tell application \"System Events\"\n"
        f"  tell process {json.dumps(process, ensure_ascii=False)}\n"
        "    try\n"
        '      set value of attribute "AXMinimized" of window 1 to false\n'
        "      set frontmost to true\n"
        "      perform action \"AXRaise\" of window 1\n"
        "    end try\n"
        "  end tell\n"
        "end tell\n"
    )
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True, text=True, timeout=5)
    if settle_seconds > 0:
        time.sleep(settle_seconds)


def image_size_from_file(image_path: Path) -> ImageSize:
    with Image.open(image_path) as image:
        return ImageSize(float(image.size[0]), float(image.size[1]))


def vision_box_center_to_pixels(box: VisionBoxLike, image_size: ImageSize, *, target_fraction: float = 0.5) -> Point:
    """Convert Apple Vision normalized bottom-left coordinates to image pixels."""
    return Point(
        x=(box.x + box.width * target_fraction) * image_size.width,
        y=(1 - (box.y + box.height * 0.5)) * image_size.height,
    )


def infer_symmetric_content_offset(
    window: WindowRect,
    image_size: ImageSize,
    *,
    scale_x: float = DEFAULT_RETINA_SCALE,
    scale_y: float = DEFAULT_RETINA_SCALE,
) -> Point:
    """Infer screenshot decoration margins from current image and window sizes."""
    return Point(
        x=max(0.0, (image_size.width - window.width * scale_x) / 2),
        y=max(0.0, (image_size.height - window.height * scale_y) / 2),
    )


def infer_capture_scale_and_offset(
    window: WindowRect,
    image_size: ImageSize,
    *,
    preferred_scale: float = DEFAULT_RETINA_SCALE,
) -> tuple[float, float, Point]:
    """Infer whether the window capture is 1x or Retina-scaled, plus margins.

    Moving THS between displays can make ``screencapture -l`` return 1x
    images, for both active captures (for example 900x1191) and inactive
    captures with smaller decorations (for example 856x1147).
    """
    candidates = [preferred_scale]
    for scale in CAPTURE_SCALE_CANDIDATES:
        if scale not in candidates:
            candidates.append(scale)

    best_scale = candidates[0]
    best_offset = infer_symmetric_content_offset(window, image_size, scale_x=best_scale, scale_y=best_scale)
    best_error = capture_scale_error(window, image_size, best_scale)
    for scale in candidates[1:]:
        offset = infer_symmetric_content_offset(window, image_size, scale_x=scale, scale_y=scale)
        error = capture_scale_error(window, image_size, scale)
        if error < best_error:
            best_scale = scale
            best_offset = offset
            best_error = error
    return best_scale, best_scale, best_offset


def capture_scale_error(window: WindowRect, image_size: ImageSize, scale: float) -> float:
    """Return size mismatch after accounting for symmetric capture margins."""
    expected_width = window.width * scale
    expected_height = window.height * scale
    return abs(image_size.width - expected_width) + abs(image_size.height - expected_height)


def pixel_to_click_point(
    pixel: Point,
    window: WindowRect,
    *,
    scale_x: float = DEFAULT_RETINA_SCALE,
    scale_y: float = DEFAULT_RETINA_SCALE,
    content_offset_px: Point,
) -> Point:
    """Map screenshot pixels into System Events global click coordinates."""
    return Point(
        x=window.x + (pixel.x - content_offset_px.x) / scale_x,
        y=window.y + (pixel.y - content_offset_px.y) / scale_y,
    )


def ocr_box_to_click_point(
    box: VisionBoxLike,
    image_size: ImageSize,
    window: WindowRect,
    *,
    target_fraction: float = 0.5,
    scale_x: float = DEFAULT_RETINA_SCALE,
    scale_y: float = DEFAULT_RETINA_SCALE,
    content_offset_px: Point | None = None,
) -> OcrClickResult:
    if content_offset_px is None:
        scale_x, scale_y, content_offset_px = infer_capture_scale_and_offset(
            window,
            image_size,
            preferred_scale=scale_x,
        )
    pixel_center = vision_box_center_to_pixels(box, image_size, target_fraction=target_fraction)
    click_point = pixel_to_click_point(
        pixel_center,
        window,
        scale_x=scale_x,
        scale_y=scale_y,
        content_offset_px=content_offset_px,
    )
    return OcrClickResult(
        pixel_center=pixel_center,
        click_point=click_point,
        content_offset_px=content_offset_px,
        scale_x=scale_x,
        scale_y=scale_y,
    )


def parse_numbers(value: str, expected: int, name: str) -> list[float]:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) != expected:
        raise SystemExit(f"{name} expects {expected} comma-separated numbers")
    return [float(part) for part in parts]


@dataclass(frozen=True)
class VisionBox:
    x: float
    y: float
    width: float
    height: float


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert Vision OCR boxes to macOS click coordinates.")
    parser.add_argument("--window", required=True, help="Window rect in points: x,y,width,height")
    parser.add_argument("--image-size", required=True, help="Screenshot size in pixels: width,height")
    parser.add_argument("--ocr-box", required=True, help="Vision OCR normalized box: x,y,width,height")
    parser.add_argument("--scale-x", type=float, default=DEFAULT_RETINA_SCALE)
    parser.add_argument("--scale-y", type=float, default=DEFAULT_RETINA_SCALE)
    parser.add_argument("--content-offset", help="Override decoration offset in pixels: x,y")
    args = parser.parse_args()

    window_values = parse_numbers(args.window, 4, "--window")
    image_values = parse_numbers(args.image_size, 2, "--image-size")
    box_values = parse_numbers(args.ocr_box, 4, "--ocr-box")
    content_offset = None
    if args.content_offset:
        offset_values = parse_numbers(args.content_offset, 2, "--content-offset")
        content_offset = Point(offset_values[0], offset_values[1])

    result = ocr_box_to_click_point(
        VisionBox(*box_values),
        ImageSize(*image_values),
        WindowRect(*window_values),
        scale_x=args.scale_x,
        scale_y=args.scale_y,
        content_offset_px=content_offset,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
