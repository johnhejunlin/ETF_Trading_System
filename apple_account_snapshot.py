#!/usr/bin/env python3
"""Create a read-only account snapshot from the THS window using macOS tools.

This is a diagnostic bridge only. It captures the THS window or screen, runs
Apple Vision OCR, and writes a JSON snapshot with source=apple_vision_ocr.
It does not click or submit orders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from AppBridge_OCRPositionCalculation import ensure_app_window_ready, image_size_from_file, verify_app_window_state


ROOT = Path(__file__).resolve().parent
SCREENSHOTS = ROOT / "screenshots"
DEFAULT_LATEST = SCREENSHOTS / "latest_account_snapshot.json"
DEFAULT_OCR = SCREENSHOTS / "latest_account_ocr.json"
SNAPSHOT_SOURCE = "apple_vision_ocr"
DEFAULT_APP_NAME = "同花顺"
DEFAULT_BUNDLE_ID = "cn.com.10jqka.macstock"
DEFAULT_PROCESS_NAME = "同花顺"
SWIFT_CACHE = ROOT / ".cache" / "app_bridge"


SWIFT_OCR = r'''
import Foundation
import Vision
import CoreGraphics
import ImageIO

struct Observation: Codable {
    let text: String
    let confidence: Float
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

let args = CommandLine.arguments
guard args.count >= 2 else {
    fputs("usage: ocr.swift image_path\n", stderr)
    exit(2)
}

let url = URL(fileURLWithPath: args[1])
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fputs("failed to load image\n", stderr)
    exit(1)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.minimumTextHeight = 0.008

let handler = VNImageRequestHandler(cgImage: image, options: [:])
try handler.perform([request])

let observations = (request.results ?? []).compactMap { item -> Observation? in
    guard let candidate = item.topCandidates(1).first else { return nil }
    let box = item.boundingBox
    return Observation(
        text: candidate.string,
        confidence: candidate.confidence,
        x: box.origin.x,
        y: box.origin.y,
        width: box.size.width,
        height: box.size.height
    )
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
let data = try encoder.encode(observations)
FileHandle.standardOutput.write(data)
'''


SWIFT_WINDOWS = r'''
import Foundation
import CoreGraphics

struct WindowInfo: Codable {
    let window_id: Int
    let owner_name: String
    let window_name: String
    let x: Double
    let y: Double
    let width: Double
    let height: Double
    let is_onscreen: Bool
}

let windows = (CGWindowListCopyWindowInfo(.optionAll, kCGNullWindowID) as? [[String: Any]] ?? []).compactMap { item -> WindowInfo? in
    guard let number = item[kCGWindowNumber as String] as? Int,
          let owner = item[kCGWindowOwnerName as String] as? String,
          let bounds = item[kCGWindowBounds as String] as? [String: Any] else {
        return nil
    }
    let name = item[kCGWindowName as String] as? String ?? ""
    let onscreen = (item[kCGWindowIsOnscreen as String] as? Int ?? 0) == 1
    let x = bounds["X"] as? Double ?? 0
    let y = bounds["Y"] as? Double ?? 0
    let width = bounds["Width"] as? Double ?? 0
    let height = bounds["Height"] as? Double ?? 0
    // macOS can expose transient system windows with infinite bounds. JSONEncoder
    // rejects non-finite Double values and would otherwise abort the entire
    // window inventory before the target app window can be selected.
    guard x.isFinite, y.isFinite, width.isFinite, height.isFinite else {
        return nil
    }
    return WindowInfo(window_id: number, owner_name: owner, window_name: name, x: x, y: y, width: width, height: height, is_onscreen: onscreen)
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
let data = try encoder.encode(windows)
FileHandle.standardOutput.write(data)
'''


@dataclass
class OcrText:
    text: str
    confidence: float
    x: float
    y: float
    width: float
    height: float


def run(command: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)


def run_cached_swift_helper(
    name: str,
    source: str,
    args: list[str],
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Compile a source-hashed Swift helper once, with the interpreter as fallback."""
    swift = shutil.which("swift")
    if not swift:
        raise RuntimeError("Swift runtime not found")
    swiftc = shutil.which("swiftc")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    binary = SWIFT_CACHE / f"{name}-{digest}"
    if swiftc and not binary.exists():
        SWIFT_CACHE.mkdir(parents=True, exist_ok=True)
        source_path = SWIFT_CACHE / f"{name}-{digest}.swift"
        temporary_binary = SWIFT_CACHE / f".{name}-{digest}.{os.getpid()}.tmp"
        source_path.write_text(source, encoding="utf-8")
        try:
            run([swiftc, "-O", str(source_path), "-o", str(temporary_binary)], timeout=max(30, timeout))
            temporary_binary.replace(binary)
        except (OSError, subprocess.SubprocessError):
            temporary_binary.unlink(missing_ok=True)
    if binary.exists():
        return run([str(binary), *args], timeout=timeout)
    with tempfile.TemporaryDirectory() as tempdir:
        swift_path = Path(tempdir) / f"{name}.swift"
        swift_path.write_text(source, encoding="utf-8")
        return run([swift, str(swift_path), *args], timeout=timeout)


def activate_app(app_name: str, bundle_id: str | None) -> None:
    scripts = []
    if bundle_id:
        scripts.append(f'tell application id "{bundle_id}" to activate')
    scripts.append(f'tell application "{app_name}" to activate')
    for script in scripts:
        try:
            run(["osascript", "-e", script], timeout=5)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue


def frontmost_process_name() -> str | None:
    script = 'tell application "System Events" to get name of first process whose frontmost is true'
    try:
        return run(["osascript", "-e", script], timeout=5).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def get_window_rect(process_name: str) -> tuple[int, int, int, int] | None:
    script = (
        'tell application "System Events"\n'
        f'  tell process "{process_name}"\n'
        '    set frontmost to true\n'
        '    set p to position of window 1\n'
        '    set s to size of window 1\n'
        '    return (item 1 of p as text) & "," & (item 2 of p as text) & "," & '
        '(item 1 of s as text) & "," & (item 2 of s as text)\n'
        '  end tell\n'
        'end tell'
    )
    try:
        output = run(["osascript", "-e", script], timeout=8).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    parts = [int(float(part.strip())) for part in output.split(",") if part.strip()]
    if len(parts) != 4:
        return None
    x, y, width, height = parts
    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def get_coregraphics_window_id(
    owner_hint: str,
    title_hint: str | None,
    *,
    timeout_seconds: float = 0.0,
) -> int | None:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            output = run_cached_swift_helper("window_list", SWIFT_WINDOWS, [], timeout=10).stdout
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return None
        windows = json.loads(output)
        candidates = []
        for window in windows:
            owner = str(window.get("owner_name", ""))
            title = str(window.get("window_name", ""))
            width = float(window.get("width", 0))
            height = float(window.get("height", 0))
            if width < 400 or height < 300 or owner != owner_hint:
                continue
            score = width * height
            if window.get("is_onscreen"):
                score *= 2
            if title_hint and title_hint in title:
                score *= 2
            candidates.append((score, int(window["window_id"])))
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def capture_screenshot(path: Path, window_id: int | None, rect: tuple[int, int, int, int] | None) -> str:
    screencapture = shutil.which("screencapture")
    if not screencapture:
        raise RuntimeError("macOS screencapture not found")
    if window_id is not None:
        run([screencapture, "-x", "-l", str(window_id), str(path)], timeout=10)
        return f"window_id:{window_id}"
    if rect:
        x, y, width, height = rect
        run([screencapture, "-x", "-R", f"{x},{y},{width},{height}", str(path)], timeout=10)
        return f"window:{x},{y},{width},{height}"
    run([screencapture, "-x", str(path)], timeout=10)
    return "fullscreen"


def run_vision_ocr(image_path: Path) -> list[OcrText]:
    output = run_cached_swift_helper("vision_ocr", SWIFT_OCR, [str(image_path)], timeout=40).stdout
    payload = json.loads(output)
    return [OcrText(**item) for item in payload]


def parse_money(value: str) -> float | None:
    cleaned = value.replace(",", "").replace("，", "").replace(" ", "")
    cleaned = cleaned.replace("−", "-").replace("—", "-")
    try:
        return float(cleaned)
    except ValueError:
        return None


NUMBER_RE = re.compile(r"[-−]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?")


def number_after_label(raw_text: str, labels: list[str], limit: int = 80) -> float | None:
    for label in labels:
        index = raw_text.find(label)
        if index < 0:
            continue
        chunk = raw_text[index + len(label) : index + len(label) + limit]
        match = NUMBER_RE.search(chunk)
        if match:
            value = parse_money(match.group(0))
            if value is not None:
                return value
    return None


def first_number(text: str) -> float | None:
    match = NUMBER_RE.search(text)
    if not match:
        return None
    return parse_money(match.group(0))


def first_present(*values: float | None) -> float | None:
    """Return the first parsed value, preserving valid numeric zeroes."""
    return next((value for value in values if value is not None), None)


def text_matches_label(text: str, label: str) -> bool:
    normalized = text.replace(" ", "")
    if label == "可用":
        return normalized.startswith("可用") and "/" not in normalized
    if label == "总市值":
        return normalized.startswith("总市值")
    if label == "总资产":
        return normalized.startswith("总资产")
    if label == "总盈亏":
        return normalized.startswith("总盈亏")
    return label in normalized


def value_below_label(
    items: list[OcrText],
    labels: list[str],
    *,
    max_dx: float = 0.08,
    max_dy: float = 0.08,
    min_label_y: float | None = None,
) -> float | None:
    labels_found = [
        item
        for item in items
        if (min_label_y is None or item.y >= min_label_y) and any(text_matches_label(item.text, label) for label in labels)
    ]
    if not labels_found:
        return None
    candidates: list[tuple[float, float]] = []
    for label in labels_found:
        label_x = label.x + label.width / 2
        for item in items:
            value = first_number(item.text)
            if value is None:
                continue
            item_x = item.x + item.width / 2
            dy = label.y - item.y
            dx = abs(label_x - item_x)
            if 0 < dy <= max_dy and dx <= max_dx:
                candidates.append((dy + dx, value))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


def value_right_of_label(
    items: list[OcrText],
    labels: list[str],
    *,
    max_dx: float = 0.12,
    max_dy: float = 0.018,
    max_value_x: float | None = None,
) -> float | None:
    """Read a numeric value on the same row to the right of an account label."""
    labels_found = [
        item
        for item in items
        if any(text_matches_label(item.text, label) for label in labels)
    ]
    candidates: list[tuple[float, float]] = []
    for label in labels_found:
        label_right = label.x + label.width
        label_y = label.y + label.height / 2
        for item in items:
            value = first_number(item.text)
            if value is None:
                continue
            item_x = item.x + item.width / 2
            item_y = item.y + item.height / 2
            dx = item_x - label_right
            dy = abs(item_y - label_y)
            if 0 <= dx <= max_dx and dy <= max_dy and (max_value_x is None or item_x <= max_value_x):
                candidates.append((dy * 4 + dx, value))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


def value_below_header(
    items: list[OcrText],
    labels: list[str],
    *,
    row_symbol_y: float | None = None,
    max_dx: float = 0.035,
    max_dy: float = 0.035,
) -> float | None:
    """Read a holdings-table value directly below a named column header."""
    headers = [item for item in items if item.text.replace(" ", "") in labels]
    candidates: list[tuple[float, float]] = []
    for header in headers:
        header_x = header.x + header.width / 2
        for item in items:
            value = first_number(item.text)
            if value is None:
                continue
            item_x = item.x + item.width / 2
            dy = header.y - item.y
            if not (0 < dy <= max_dy and abs(header_x - item_x) <= max_dx):
                continue
            if row_symbol_y is not None and abs(item.y - row_symbol_y) > 0.018:
                continue
            candidates.append((abs(header_x - item_x) + dy, value))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


def _component_has_single_hole(mask: list[list[bool]]) -> bool:
    """Return whether a glyph mask encloses exactly one background region."""
    if not mask or not mask[0]:
        return False
    height = len(mask)
    width = len(mask[0])
    padded = [[False] * (width + 2)]
    padded.extend([[False, *row, False] for row in mask])
    padded.append([False] * (width + 2))
    seen: set[tuple[int, int]] = set()
    holes = 0
    for start_y in range(height + 2):
        for start_x in range(width + 2):
            if padded[start_y][start_x] or (start_x, start_y) in seen:
                continue
            stack = [(start_x, start_y)]
            seen.add((start_x, start_y))
            touches_edge = False
            while stack:
                x, y = stack.pop()
                if x in (0, width + 1) or y in (0, height + 1):
                    touches_edge = True
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    nx, ny = x + dx, y + dy
                    if not (0 <= nx < width + 2 and 0 <= ny < height + 2):
                        continue
                    if padded[ny][nx] or (nx, ny) in seen:
                        continue
                    seen.add((nx, ny))
                    stack.append((nx, ny))
            if not touches_edge:
                holes += 1
    return holes == 1


def _zero_glyph_foreground_pixel(rgb: tuple[int, int, int]) -> bool:
    """Recognize THS numeric text colors without treating pale cell chrome as ink."""
    red, green, blue = rgb
    red_text = red >= 85 and red - green >= 18 and red - blue >= 12
    blue_text = blue >= 100 and blue - red >= 30 and blue - green >= 10
    green_text = green >= 85 and green - red >= 18 and green - blue >= 12
    neutral_dark_text = max(rgb) <= 180 and max(rgb) - min(rgb) <= 25
    return red_text or blue_text or green_text or neutral_dark_text


def detect_zero_in_holdings_cell(
    image_path: Path,
    items: list[OcrText],
    *,
    header_label: str,
    row_symbol_y: float | None,
) -> dict[str, Any] | None:
    """Recover an OCR-dropped colored zero from one anchored holdings-table cell.

    The fallback is deliberately visual rather than semantic: a missing value is
    never assumed to be zero.  It requires the named table header, the target
    security row, and one ring-shaped numeric glyph inside that exact cell.
    THS can render zero values in red, blue, green, or neutral gray depending on
    row state and focus, so color is only used to separate text from pale chrome.
    """
    if row_symbol_y is None or not image_path.is_file():
        return None
    headers = [item for item in items if item.text.replace(" ", "") == header_label]
    if not headers:
        return None
    header = headers[0]
    header_center_x = header.x + header.width / 2
    same_row_headers = [
        item
        for item in items
        if item is not header
        and abs((item.y + item.height / 2) - (header.y + header.height / 2)) <= 0.012
        and item.text.replace(" ", "")
        in {
            "证券代码", "证券名称", "市价", "成本价", "盈亏", "实际数量",
            "股票余额", "可用余额", "冻结数量", "市值", "仓位占比(%)",
        }
    ]
    left_centers = [item.x + item.width / 2 for item in same_row_headers if item.x + item.width / 2 < header_center_x]
    right_centers = [item.x + item.width / 2 for item in same_row_headers if item.x + item.width / 2 > header_center_x]
    left = (max(left_centers) + header_center_x) / 2 if left_centers else header_center_x - max(0.025, header.width)
    right = (min(right_centers) + header_center_x) / 2 if right_centers else header_center_x + max(0.025, header.width)
    bottom = max(0.0, row_symbol_y - 0.006)
    top = min(header.y - 0.002, row_symbol_y + 0.022)
    if left >= right or bottom >= top:
        return None

    try:
        with Image.open(image_path) as source:
            image = source.convert("RGB")
            width, height = image.size
            pixel_box = (
                max(0, int(left * width)),
                max(0, int((1.0 - top) * height)),
                min(width, int(right * width + 0.999)),
                min(height, int((1.0 - bottom) * height + 0.999)),
            )
            crop = image.crop(pixel_box)
    except (OSError, ValueError):
        return None

    crop_width, crop_height = crop.size
    if crop_width < 3 or crop_height < 3:
        return None
    pixels = crop.load()
    foreground_mask = [
        [
            _zero_glyph_foreground_pixel(pixels[x, y])
            for x in range(crop_width)
        ]
        for y in range(crop_height)
    ]

    seen: set[tuple[int, int]] = set()
    components: list[list[tuple[int, int]]] = []
    for y in range(crop_height):
        for x in range(crop_width):
            if not foreground_mask[y][x] or (x, y) in seen:
                continue
            component: list[tuple[int, int]] = []
            stack = [(x, y)]
            seen.add((x, y))
            while stack:
                px, py = stack.pop()
                component.append((px, py))
                for dx, dy in ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)):
                    nx, ny = px + dx, py + dy
                    if not (0 <= nx < crop_width and 0 <= ny < crop_height):
                        continue
                    if not foreground_mask[ny][nx] or (nx, ny) in seen:
                        continue
                    seen.add((nx, ny))
                    stack.append((nx, ny))
            if len(component) >= 6:
                components.append(component)
    if not components:
        return None

    # THS right-aligns numeric cells, so the glyph is not necessarily below the
    # header centre.  First discard grid lines and fragments from adjacent cells,
    # then require exactly one closed zero candidate in the target cell.
    candidates: list[list[tuple[int, int]]] = []
    for component in components:
        min_x = min(point[0] for point in component)
        max_x = max(point[0] for point in component)
        min_y = min(point[1] for point in component)
        max_y = max(point[1] for point in component)
        glyph_width = max_x - min_x + 1
        glyph_height = max_y - min_y + 1
        aspect_ratio = glyph_width / glyph_height
        fill_ratio = len(component) / (glyph_width * glyph_height)
        if min_x == 0 or max_x == crop_width - 1 or min_y == 0 or max_y == crop_height - 1:
            continue
        if not (3 <= glyph_width <= crop_width * 0.7 and 5 <= glyph_height <= crop_height * 0.95):
            continue
        if not (0.30 <= aspect_ratio <= 1.35 and 0.10 <= fill_ratio <= 0.70):
            continue
        glyph_mask = [
            [foreground_mask[y][x] for x in range(min_x, max_x + 1)]
            for y in range(min_y, max_y + 1)
        ]
        if _component_has_single_hole(glyph_mask):
            candidates.append(component)
    if len(candidates) != 1:
        return None
    component = candidates[0]
    return {
        "method": "anchored_colored_zero_glyph",
        "header": header_label,
        "normalized_cell": [round(left, 6), round(bottom, 6), round(right, 6), round(top, 6)],
        "glyph_pixels": len(component),
    }


def normalize_ocr_text(items: list[OcrText]) -> list[OcrText]:
    return sorted(items, key=lambda item: (-item.y, item.x))


def column_values(items: list[OcrText], labels: list[str], *, max_dx: float = 0.06, max_dy: float = 0.06) -> list[float]:
    headers = [item for item in items if any(label in item.text for label in labels)]
    values: list[tuple[float, float]] = []
    for header in headers:
        header_x = header.x + header.width / 2
        for item in items:
            value = first_number(item.text)
            if value is None:
                continue
            item_x = item.x + item.width / 2
            dy = header.y - item.y
            if 0 < dy <= max_dy and abs(header_x - item_x) <= max_dx:
                values.append((item.y, value))
    values.sort(reverse=True)
    return [value for _, value in values]


def parse_position(
    raw_text: str,
    symbol: str,
    items: list[OcrText],
    image_path: Path | None = None,
) -> dict[str, Any] | None:
    if symbol not in raw_text:
        return None
    symbol_items = [item for item in items if symbol in item.text]
    table_headers = [
        item
        for item in items
        if item.text.replace(" ", "") in {"证券代码", "证券名称", "实际数量", "股票余额", "可用余额"}
    ]
    table_header_y = max((item.y for item in table_headers), default=None)
    table_symbol_items = [
        item
        for item in symbol_items
        if table_header_y is not None and 0 < table_header_y - item.y <= 0.05
    ]
    if table_symbol_items:
        table_symbol_items.sort(key=lambda item: table_header_y - item.y)
        symbol_y = table_symbol_items[0].y
    else:
        symbol_y = symbol_items[0].y if symbol_items else None
    row_items = [
        item.text
        for item in items
        if symbol_y is not None and abs(item.y - symbol_y) <= 0.04
    ]

    market_values = column_values(items, ["市值"])
    pnl_values = column_values(items, ["盈亏"])
    quantity_values = column_values(items, ["持仓/可用"])
    cost_price_values = column_values(items, ["成本/现价"])
    position_ratio_values = column_values(items, ["个股仓位"])

    position: dict[str, Any] = {
        "symbol": symbol,
        "source": SNAPSHOT_SOURCE,
        "raw_position_text": " || ".join(row_items),
    }
    if quantity_values:
        position["quantity"] = int(quantity_values[0])
        position["sellable_quantity"] = int(quantity_values[1]) if len(quantity_values) > 1 else int(quantity_values[0])
        position["available_quantity"] = position["sellable_quantity"]
    if len(cost_price_values) >= 2:
        position["avg_cost"] = cost_price_values[0]
        position["current_price"] = cost_price_values[1]
    elif cost_price_values:
        position["current_price"] = cost_price_values[0]
    if market_values:
        position["market_value"] = market_values[0]
    if pnl_values:
        position["profit_loss"] = pnl_values[0]
    if position_ratio_values:
        position["position_ratio"] = position_ratio_values[0] / 100.0

    # Ordinary macOS THS exposes a desktop-style holdings table with separate
    # headers instead of the compact "持仓/可用" and "成本/现价" columns.
    actual_quantity = value_below_header(items, ["实际数量"], row_symbol_y=symbol_y)
    stock_balance = value_below_header(items, ["股票余额"], row_symbol_y=symbol_y)
    available_quantity = value_below_header(items, ["可用余额"], row_symbol_y=symbol_y)
    available_zero_evidence = None
    if available_quantity is None and image_path is not None:
        available_zero_evidence = detect_zero_in_holdings_cell(
            image_path,
            items,
            header_label="可用余额",
            row_symbol_y=symbol_y,
        )
        if available_zero_evidence is not None:
            available_quantity = 0.0
    avg_cost = value_below_header(items, ["成本价"], row_symbol_y=symbol_y)
    current_price = value_below_header(items, ["市价"], row_symbol_y=symbol_y)
    profit_loss = value_below_header(items, ["盈亏"], row_symbol_y=symbol_y)
    if actual_quantity is not None:
        position["quantity"] = int(actual_quantity)
    elif stock_balance is not None:
        position["quantity"] = int(stock_balance)
    if available_quantity is not None:
        position["sellable_quantity"] = int(available_quantity)
        position["available_quantity"] = int(available_quantity)
        if available_zero_evidence is not None:
            position["sellable_quantity_source"] = "screenshot_cell_zero"
            position["sellable_quantity_evidence"] = available_zero_evidence
    if avg_cost is not None:
        position["avg_cost"] = avg_cost
    if current_price is not None:
        position["current_price"] = current_price
    if profit_loss is not None:
        position["profit_loss"] = profit_loss

    average_match = re.search(r"均价[:：]\s*(\d+(?:\.\d+)?)", raw_text)
    latest_match = re.search(r"最新[:：]\s*(\d+(?:\.\d+)?)", raw_text)
    # Prefer the target holding row's 成本价.  The full window can also contain
    # a chart-level "均价", which is not the account position cost basis.
    if average_match and "avg_cost" not in position:
        position["avg_cost"] = float(average_match.group(1))
    if latest_match and "current_price" not in position:
        position["current_price"] = float(latest_match.group(1))
    return position


def validate_snapshot(snapshot: dict[str, Any], symbol: str) -> list[str]:
    errors: list[str] = []
    anchors = snapshot.get("anchors", {})
    market_value = snapshot.get("market_value")
    required_anchors = ["simulation", "total_assets", "available_cash", "position_area"]
    if not isinstance(market_value, (int, float)) or abs(float(market_value)) > 0.01:
        required_anchors.append("target_symbol")
    missing_anchors = [key for key in required_anchors if not anchors.get(key)]
    if missing_anchors:
        errors.append("missing anchors: " + ", ".join(missing_anchors))
    if snapshot.get("account_mode") != "simulation":
        errors.append("account_mode is not simulation")
    if snapshot.get("source") != SNAPSHOT_SOURCE:
        errors.append("unexpected snapshot source")

    total_assets = snapshot.get("total_assets")
    available_cash = snapshot.get("available_cash")
    cash_balance = snapshot.get("cash_balance")
    for field, value in (
        ("total_assets", total_assets),
        ("available_cash/cash_balance", available_cash if available_cash is not None else cash_balance),
        ("market_value", market_value),
    ):
        if not isinstance(value, (int, float)):
            errors.append(f"{field} is missing")
    if isinstance(total_assets, (int, float)) and isinstance(market_value, (int, float)):
        cash = available_cash if isinstance(available_cash, (int, float)) else cash_balance
        if isinstance(cash, (int, float)) and abs(total_assets - cash - market_value) > max(5.0, abs(total_assets) * 0.01):
            errors.append("total_assets is not close to cash + market_value")

    positions = snapshot.get("positions")
    if not isinstance(positions, list):
        errors.append("positions is not a list")
        return errors
    position = next((item for item in positions if isinstance(item, dict) and item.get("symbol") == symbol), None)
    if position is None:
        if not isinstance(market_value, (int, float)) or abs(float(market_value)) > 0.01:
            errors.append(f"target position {symbol} is missing while market_value is non-zero")
        return errors
    for field in ("quantity", "sellable_quantity", "avg_cost", "current_price", "market_value"):
        if not isinstance(position.get(field), (int, float)):
            errors.append(f"position.{field} is missing")
    quantity = position.get("quantity")
    current_price = position.get("current_price")
    position_market_value = position.get("market_value")
    if all(isinstance(value, (int, float)) for value in (quantity, current_price, position_market_value)):
        implied_value = float(quantity) * float(current_price)
        if abs(implied_value - float(position_market_value)) > max(3.0, abs(float(position_market_value)) * 0.03):
            errors.append("position market_value is not close to quantity * current_price")
    if isinstance(quantity, (int, float)) and (int(quantity) <= 0 or int(quantity) % 100 != 0):
        errors.append("position.quantity is not a positive 100-share lot")
    return errors


def build_snapshot(
    items: list[OcrText],
    symbol: str,
    image_path: Path,
    capture_mode: str,
    frontmost_process: str | None,
) -> dict[str, Any]:
    ordered = normalize_ocr_text(items)
    raw_text = " || ".join(item.text for item in ordered)
    total_assets = first_present(
        value_right_of_label(ordered, ["总资产"], max_value_x=0.22),
        value_below_label(ordered, ["总资产"], min_label_y=0.75),
        number_after_label(raw_text, ["总资产"]),
    )
    available_cash = first_present(
        value_right_of_label(ordered, ["可用金额", "可用资金", "可用"], max_value_x=0.22),
        value_below_label(ordered, ["可用金额", "可用资金", "可用"], min_label_y=0.72),
        number_after_label(raw_text, ["可用金额", "可用资金", "可用"]),
    )
    cash_balance = first_present(
        value_right_of_label(ordered, ["资金余额", "余额"], max_value_x=0.22),
        value_below_label(ordered, ["资金余额", "余额"]),
        number_after_label(raw_text, ["资金余额", "余额"]),
    )
    market_value = first_present(
        value_right_of_label(ordered, ["总市值"], max_value_x=0.22),
        value_below_label(ordered, ["总市值"], min_label_y=0.72),
        number_after_label(raw_text, ["总市值"]),
    )
    profit_loss = first_present(
        value_right_of_label(ordered, ["总盈亏"], max_value_x=0.22),
        value_below_label(ordered, ["总盈亏"], min_label_y=0.75),
        number_after_label(raw_text, ["总盈亏"]),
    )
    position = parse_position(raw_text, symbol, ordered, image_path=image_path)
    if isinstance(market_value, (int, float)) and abs(float(market_value)) <= 0.01:
        position = None
    if (
        position
        and market_value is not None
        and any(item.text.replace(" ", "") == "实际数量" for item in ordered)
    ):
        position["market_value"] = market_value
        if profit_loss is not None:
            position["profit_loss"] = profit_loss

    anchors = {
        "simulation": any(
            key in raw_text
            for key in ["模拟炒股", "模拟账户", "模拟交易", "模拟练习", "楧拟练习", "梗拟练习", "大玩家"]
        ),
        "total_assets": total_assets is not None,
        "available_cash": available_cash is not None or cash_balance is not None,
        "position_area": any(key in raw_text for key in ["持仓股", "持仓"]),
        "target_symbol": symbol in raw_text,
    }
    warnings: list[str] = []
    if total_assets is not None and market_value is not None and (
        available_cash is not None or cash_balance is not None
    ):
        cash = available_cash if available_cash is not None else cash_balance
        assert cash is not None
        if abs(total_assets - cash - market_value) > max(5.0, total_assets * 0.01):
            warnings.append("total_assets is not close to available_cash + market_value")
    required_anchor_names = {"simulation", "total_assets", "available_cash", "position_area"}
    if not isinstance(market_value, (int, float)) or abs(float(market_value)) > 0.01:
        required_anchor_names.add("target_symbol")
    missing = [key for key, ok in anchors.items() if key in required_anchor_names and not ok]
    if missing:
        warnings.append("missing anchors: " + ", ".join(missing))

    snapshot: dict[str, Any] = {
        "account_mode": "simulation" if anchors["simulation"] else "unknown",
        "total_assets": total_assets,
        "available_cash": available_cash,
        "cash_balance": cash_balance if cash_balance is not None else available_cash,
        "market_value": market_value,
        "profit_loss": profit_loss,
        "positions": [position] if position else [],
        "source": SNAPSHOT_SOURCE,
        "submitted": False,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "frontmost_process": frontmost_process,
        "capture_mode": capture_mode,
        "screenshot_path": str(image_path),
        "anchors": anchors,
        "warnings": warnings,
        "raw_ui_text": raw_text,
    }
    validation_errors = validate_snapshot(snapshot, symbol)
    if validation_errors:
        snapshot["validation_errors"] = validation_errors
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Read THS simulated account snapshot with Apple Vision OCR")
    parser.add_argument("--app-name", default=DEFAULT_APP_NAME)
    parser.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID)
    parser.add_argument("--process-name", default=DEFAULT_PROCESS_NAME)
    parser.add_argument("--symbol", default="588330")
    parser.add_argument("--write-latest", action="store_true", help="also write screenshots/latest_account_snapshot.json")
    parser.add_argument("--output", type=Path, default=None, help="diagnostic JSON output path")
    parser.add_argument("--no-activate", action="store_true")
    args = parser.parse_args()

    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    image_path = SCREENSHOTS / f"apple_account_snapshot_{timestamp}.png"
    output_path = args.output or SCREENSHOTS / f"apple_account_snapshot_{timestamp}.json"

    if not args.no_activate:
        ready_state = ensure_app_window_ready(args.app_name, process_name=args.process_name)
        rect = tuple(ready_state["window_rect"])
    else:
        rect = get_window_rect(args.process_name)
    frontmost = frontmost_process_name()
    window_id = get_coregraphics_window_id("同花顺", args.app_name)
    if not args.no_activate and rect is not None:
        verify_app_window_state(args.process_name, expected_rect=rect)
    capture_mode = capture_screenshot(image_path, window_id, rect)
    if not args.no_activate and rect is not None:
        verify_app_window_state(args.process_name, expected_rect=rect)
    ocr_items = run_vision_ocr(image_path)
    if not args.no_activate and rect is not None:
        verify_app_window_state(args.process_name, expected_rect=rect)
    snapshot = build_snapshot(ocr_items, args.symbol, image_path, capture_mode, frontmost)
    image_size = image_size_from_file(image_path)
    snapshot["window_evidence"] = {
        "process_name": args.process_name,
        "frontmost_process": frontmost_process_name(),
        "window_id": window_id,
        "window_rect": list(rect) if rect else None,
        "image_size": [int(image_size.width), int(image_size.height)],
        "capture_mode": capture_mode,
    }

    diagnostic = {
        "snapshot": snapshot,
        "ocr_items": [asdict(item) for item in normalize_ocr_text(ocr_items)],
    }
    output_path.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
    DEFAULT_OCR.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
    validation_errors = snapshot.get("validation_errors", [])
    if args.write_latest and validation_errors:
        print("Refusing to write latest snapshot because validation failed: " + "; ".join(validation_errors), file=sys.stderr)
    elif args.write_latest:
        DEFAULT_LATEST.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    if snapshot.get("warnings") or validation_errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
