#!/usr/bin/env python3
"""Navigate THS simulated trading pages with macOS AppleScript tools.

This bridge opens THS, navigates to simulated trading, captures the screen,
runs Apple Vision OCR, and writes verification JSON. Buy and sell submission
are limited to THS simulation mode and only run when explicitly requested.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
import re

from apple_account_snapshot import (
    OcrText,
    SWIFT_WINDOWS,
    activate_app,
    build_snapshot,
    capture_screenshot,
    frontmost_process_name,
    get_coregraphics_window_id,
    get_window_rect,
    normalize_ocr_text,
    run,
    run_cached_swift_helper,
    run_vision_ocr,
)
from AppBridge_OCRPositionCalculation import (
    AppWindowGuardError,
    WindowRect,
    activate_app_for_stable_capture,
    ensure_app_window_ready,
    image_size_from_file,
    ocr_box_to_click_point,
    verify_app_window_state,
)


ROOT = Path(__file__).resolve().parent
SCREENSHOTS = ROOT / "screenshots"
DEFAULT_OUTPUT = SCREENSHOTS / "latest_applescript_bridge_holdings.json"
DEFAULT_VERIFICATION = SCREENSHOTS / "latest_verified_order.json"
DEFAULT_ACCOUNT_SNAPSHOT = SCREENSHOTS / "latest_account_snapshot.json"
DEFAULT_APP_NAME = "同花顺"
DEFAULT_BUNDLE_ID = "cn.com.10jqka.macstock"
DEFAULT_PROCESS_NAME = "同花顺"
DEFAULT_INTERACTION_MODE = "accessibility_first"
APP_READY_TIMEOUT_SECONDS = 30.0


class AppleScriptBridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class AXControl:
    role: str
    name: str
    description: str
    value: str
    x: int
    y: int
    width: int
    height: int
    enabled: bool
    can_press: bool


def run_osascript_output(script: str) -> str:
    try:
        verify_app_window_state(DEFAULT_PROCESS_NAME)
    except AppWindowGuardError:
        ensure_app_window_ready(
            DEFAULT_APP_NAME,
            process_name=DEFAULT_PROCESS_NAME,
            timeout_seconds=APP_READY_TIMEOUT_SECONDS,
            stable_samples=1,
        )
    guarded_script = (
        'tell application "System Events"\n'
        f'  if (name of first process whose frontmost is true) is not {json.dumps(DEFAULT_PROCESS_NAME, ensure_ascii=False)} then error "THS focus guard failed"\n'
        'end tell\n'
        + script
    )
    try:
        return subprocess.run(
            ["osascript", "-e", guarded_script],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or str(exc)).strip()
        if "-25211" in message or "不允许辅助访问" in message:
            raise AppleScriptBridgeError(
                "macOS Accessibility denied UI actions for osascript. "
                "Enable Accessibility for Terminal and the Python.app used by trading_engine.py "
                "(and Codex when testing from Codex), then restart the launching app."
            ) from exc
        raise AppleScriptBridgeError(f"osascript failed: {message}") from exc


def run_osascript(script: str) -> None:
    run_osascript_output(script)


def read_accessibility_controls(process_name: str) -> list[AXControl]:
    script = f'''
tell application "System Events"
  tell application process {json.dumps(process_name, ensure_ascii=False)}
    set out to ""
    set elems to UI elements of window 1
    repeat with i from 1 to (count of elems)
      set e to item i of elems
      try
        set r to role of e as text
      on error
        set r to ""
      end try
      if r is "AXButton" or r is "AXTextField" or r is "AXStaticText" or r is "AXCheckBox" then
        try
          set n to name of e as text
        on error
          set n to ""
        end try
        try
          set d to description of e as text
        on error
          set d to ""
        end try
        try
          set v to value of e as text
        on error
          set v to ""
        end try
        try
          set p to position of e
          set px to item 1 of p
          set py to item 2 of p
        on error
          set px to -1
          set py to -1
        end try
        try
          set s to size of e
          set sw to item 1 of s
          set sh to item 2 of s
        on error
          set sw to -1
          set sh to -1
        end try
        try
          set en to enabled of e as text
        on error
          set en to "false"
        end try
        set out to out & r & "|" & n & "|" & d & "|" & v & "|" & px & "|" & py & "|" & sw & "|" & sh & "|" & en & linefeed
      end if
    end repeat
    return out
  end tell
end tell
'''
    controls: list[AXControl] = []
    for line in run_osascript_output(script).splitlines():
        parts = line.split("|")
        if len(parts) != 9:
            continue
        role, name, description, value, x, y, width, height, enabled = parts
        try:
            controls.append(
                AXControl(
                    role=role,
                    name="" if name == "missing value" else name,
                    description=description,
                    value="" if value == "missing value" else value,
                    x=int(float(x)),
                    y=int(float(y)),
                    width=int(float(width)),
                    height=int(float(height)),
                    enabled=enabled.lower() == "true",
                    can_press=role in {"AXButton", "AXCheckBox"},
                )
            )
        except ValueError:
            continue
    return controls


def accessibility_text(controls: list[AXControl]) -> str:
    values: list[str] = []
    for control in controls:
        for value in (control.name, control.value):
            value = value.strip()
            if value and value not in values:
                values.append(value)
    return " || ".join(values)


def read_accessibility_text(process_name: str) -> str:
    script = (
        'tell application "System Events" to tell application process '
        f'{json.dumps(process_name, ensure_ascii=False)} '
        "to return (name of every UI element of window 1) as text"
    )
    return run_osascript_output(script).replace("missing value", "")


def ax_press_named_control(process_name: str, targets: list[str]) -> dict[str, Any]:
    target_list = ", ".join(json.dumps(target, ensure_ascii=False) for target in targets)
    script = f'''
set targetNames to {{{target_list}}}
tell application "System Events"
  tell process {json.dumps(process_name, ensure_ascii=False)}
    repeat with targetName in targetNames
      set matchedElement to missing value
      try
        set matchedElement to first button of window 1 whose name is (targetName as text)
      end try
      if matchedElement is missing value then
        try
          set matchedElement to first checkbox of window 1 whose name is (targetName as text)
        end try
      end if
      if matchedElement is not missing value then
        try
          set elementEnabled to (enabled of matchedElement is true)
        on error
          set elementEnabled to false
        end try
        if elementEnabled then
          perform action "AXPress" of matchedElement
          return targetName as text
        end if
      end if
    end repeat
  end tell
end tell
return ""
'''
    matched = run_osascript_output(script)
    if not matched:
        raise AppleScriptBridgeError(f"Accessibility control not found: {targets}")
    return {"method": "accessibility", "action": "AXPress", "target": matched}


def ax_press_named_sheet_button(process_name: str, targets: list[str]) -> dict[str, Any]:
    """Press an enabled named button in the foremost window's AXSheet."""
    target_list = ", ".join(json.dumps(target, ensure_ascii=False) for target in targets)
    script = f'''
set targetNames to {{{target_list}}}
tell application "System Events"
  tell process {json.dumps(process_name, ensure_ascii=False)}
    if exists sheet 1 of window 1 then
      repeat with targetName in targetNames
        set matchedElement to missing value
        try
          set matchedElement to first button of sheet 1 of window 1 whose name is (targetName as text)
        end try
        if matchedElement is not missing value then
          try
            set elementEnabled to (enabled of matchedElement is true)
          on error
            set elementEnabled to false
          end try
          if elementEnabled then
            perform action "AXPress" of matchedElement
            return targetName as text
          end if
        end if
      end repeat
    end if
  end tell
end tell
return ""
'''
    matched = run_osascript_output(script)
    if not matched:
        raise AppleScriptBridgeError(f"Accessibility sheet button not found: {targets}")
    return {
        "method": "accessibility",
        "action": "AXPress",
        "container": "AXSheet",
        "target": matched,
    }


def ax_press_sidebar_button_near_point(
    process_name: str,
    point_x: int,
    point_y: int,
    *,
    max_distance: int = 72,
) -> dict[str, Any]:
    """Press the left-sidebar AX button nearest an OCR-confirmed label.

    THS exposes the desktop sidebar buttons without accessible names. The OCR
    label identifies the intended destination; Accessibility supplies the
    actual press target, avoiding an unreliable global mouse click.
    """
    script = f'''
set targetX to {point_x}
set targetY to {point_y}
set maxDistance to {max_distance}
tell application "System Events"
  tell process {json.dumps(process_name, ensure_ascii=False)}
    set bestButton to missing value
    set bestDistance to 1000000
    repeat with candidateButton in every button of window 1
      try
        set candidatePosition to position of candidateButton
        set candidateSize to size of candidateButton
        set buttonX to item 1 of candidatePosition
        set buttonY to item 2 of candidatePosition
        set buttonWidth to item 1 of candidateSize
        set buttonHeight to item 2 of candidateSize
        set centerX to buttonX + buttonWidth / 2
        set centerY to buttonY + buttonHeight / 2
        set candidateDistance to (centerX - targetX)
        if candidateDistance < 0 then set candidateDistance to -candidateDistance
        set verticalDistance to (centerY - targetY)
        if verticalDistance < 0 then set verticalDistance to -verticalDistance
        set candidateDistance to candidateDistance + verticalDistance
        if buttonX < 100 and candidateDistance < bestDistance then
          set bestDistance to candidateDistance
          set bestButton to candidateButton
        end if
      end try
    end repeat
    if bestButton is missing value or bestDistance > maxDistance then return ""
    set selectedPosition to position of bestButton
    set selectedSize to size of bestButton
    perform action "AXPress" of bestButton
    return (item 1 of selectedPosition as text) & "|" & (item 2 of selectedPosition as text) & "|" & (item 1 of selectedSize as text) & "|" & (item 2 of selectedSize as text) & "|" & (bestDistance as text)
  end tell
end tell
'''
    output = run_osascript_output(script)
    values = output.split("|")
    if len(values) != 5:
        raise AppleScriptBridgeError(
            f"Accessibility sidebar button not found near OCR point ({point_x}, {point_y})"
        )
    try:
        x, y, width, height, distance = [int(float(value)) for value in values]
    except ValueError as exc:
        raise AppleScriptBridgeError(f"invalid Accessibility sidebar button data: {output!r}") from exc
    return {
        "method": "accessibility_near_ocr_anchor",
        "action": "AXPress",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "distance": distance,
        "ocr_anchor": {"x": point_x, "y": point_y},
    }


def ax_set_text_field_near_label(process_name: str, labels: list[str], value: str) -> dict[str, Any]:
    label_list = ", ".join(json.dumps(label, ensure_ascii=False) for label in labels)
    script = f'''
set labelNames to {{{label_list}}}
set requestedValue to {json.dumps(value, ensure_ascii=False)}
tell application "System Events"
  tell process {json.dumps(process_name, ensure_ascii=False)}
    set fieldElements to every text field of window 1
    set labelElement to missing value
    set matchedLabel to ""
    repeat with labelName in labelNames
      try
        set labelElement to first static text of window 1 whose name is (labelName as text)
        set matchedLabel to labelName as text
      end try
      if labelElement is not missing value then exit repeat
    end repeat
    if labelElement is missing value then return ""
    set labelPosition to position of labelElement
    set labelX to item 1 of labelPosition
    set labelY to item 2 of labelPosition
    set bestField to missing value
    set bestDistance to 100000
    repeat with fieldElement in fieldElements
      try
        set fieldPosition to position of fieldElement
        set fieldX to item 1 of fieldPosition
        set fieldY to item 2 of fieldPosition
        set fieldDistance to (fieldY - labelY)
        if fieldDistance < 0 then set fieldDistance to -fieldDistance
        if fieldX is greater than or equal to labelX and fieldDistance < bestDistance then
          set bestDistance to fieldDistance
          set bestField to fieldElement
        end if
      end try
    end repeat
    if bestField is missing value or bestDistance > 45 then return ""
    set value of attribute "AXValue" of bestField to requestedValue
    delay 0.15
    return matchedLabel & tab & (value of bestField as text)
  end tell
end tell
'''
    output = run_osascript_output(script)
    if "\t" not in output:
        raise AppleScriptBridgeError(f"Accessibility text field not found near labels: {labels}")
    matched_label, readback = output.split("\t", 1)
    if readback != value:
        raise AppleScriptBridgeError(
            f"Accessibility field readback mismatch for {matched_label}: {readback!r} != {value!r}"
        )
    return {
        "method": "accessibility",
        "action": "AXValue",
        "label": matched_label,
        "value": readback,
    }


def ax_type_text_field_near_label(process_name: str, labels: list[str], value: str) -> dict[str, Any]:
    """Focus a nearby text field and type characters, without AXValue or paste."""
    label_list = ", ".join(json.dumps(label, ensure_ascii=False) for label in labels)
    script = f'''
set labelNames to {{{label_list}}}
set requestedValue to {json.dumps(value, ensure_ascii=False)}
tell application "System Events"
  tell process {json.dumps(process_name, ensure_ascii=False)}
    set fieldElements to every text field of window 1
    set labelElement to missing value
    set matchedLabel to ""
    repeat with labelName in labelNames
      try
        set labelElement to first static text of window 1 whose name is (labelName as text)
        set matchedLabel to labelName as text
      end try
      if labelElement is not missing value then exit repeat
    end repeat
    if labelElement is missing value then return ""
    set labelPosition to position of labelElement
    set labelX to item 1 of labelPosition
    set labelY to item 2 of labelPosition
    set bestField to missing value
    set bestDistance to 100000
    repeat with fieldElement in fieldElements
      try
        set fieldPosition to position of fieldElement
        set fieldX to item 1 of fieldPosition
        set fieldY to item 2 of fieldPosition
        set fieldDistance to (fieldY - labelY)
        if fieldDistance < 0 then set fieldDistance to -fieldDistance
        if fieldX is greater than or equal to labelX and fieldDistance < bestDistance then
          set bestDistance to fieldDistance
          set bestField to fieldElement
        end if
      end try
    end repeat
    if bestField is missing value or bestDistance > 45 then return ""
    set focused of bestField to true
    keystroke "a" using command down
    key code 51
    repeat with typedCharacter in characters of requestedValue
      keystroke (typedCharacter as text)
      delay 0.08
    end repeat
    delay 0.8
    return matchedLabel & tab & (value of bestField as text)
  end tell
end tell
'''
    output = run_osascript_output(script)
    if "\t" not in output:
        raise AppleScriptBridgeError(f"Accessibility text field not found near labels: {labels}")
    matched_label, readback = output.split("\t", 1)
    if readback != value:
        raise AppleScriptBridgeError(
            f"Accessibility typed field readback mismatch for {matched_label}: {readback!r} != {value!r}"
        )
    return {
        "method": "accessibility",
        "action": "AXType",
        "label": matched_label,
        "value": readback,
    }


def order_fields_from_controls(controls: list[AXControl]) -> dict[str, str]:
    labels = {
        "symbol": ["代码", "股票代码", "证券代码"],
        "price": ["价格", "限价"],
        "quantity": ["数量", "委托数量", "买入量", "卖出量"],
    }
    fields = [control for control in controls if control.role == "AXTextField"]
    static_text = [control for control in controls if control.role == "AXStaticText"]
    result: dict[str, str] = {}
    for field_name, candidates in labels.items():
        label = next(
            (
                control
                for control in static_text
                if control.name in candidates or control.value in candidates
            ),
            None,
        )
        if label is None:
            continue
        matches = [
            field
            for field in fields
            if field.x >= label.x and abs(field.y - label.y) <= 45
        ]
        if matches:
            matches.sort(key=lambda field: (abs(field.y - label.y), abs(field.x - label.x)))
            result[field_name] = matches[0].value
    result["raw_text"] = accessibility_text(controls)
    return result


def _ax_read_field_block(key: str, labels: list[str]) -> str:
    prefix = key.replace("_", "")
    label_attempts = "\n".join(
        f'''    if {prefix}Label is missing value then
      try
        set {prefix}Label to first static text of window 1 whose name is {json.dumps(label, ensure_ascii=False)}
      end try
    end if'''
        for label in labels
    )
    return f'''
    set {prefix}Label to missing value
{label_attempts}
    set {prefix}Value to ""
    if {prefix}Label is not missing value then
      set {prefix}Position to position of {prefix}Label
      set {prefix}X to item 1 of {prefix}Position
      set {prefix}Y to item 2 of {prefix}Position
      set {prefix}BestField to missing value
      set {prefix}BestDistance to 100000
      repeat with fieldElement in fieldElements
        try
          set fieldPosition to position of fieldElement
          set fieldX to item 1 of fieldPosition
          set fieldY to item 2 of fieldPosition
          set fieldDistance to fieldY - {prefix}Y
          if fieldDistance < 0 then set fieldDistance to -fieldDistance
          if fieldX is greater than or equal to {prefix}X and fieldDistance < {prefix}BestDistance then
            set {prefix}BestDistance to fieldDistance
            set {prefix}BestField to fieldElement
          end if
        end try
      end repeat
      if {prefix}BestField is not missing value and {prefix}BestDistance is less than or equal to 45 then
        set {prefix}Value to value of {prefix}BestField as text
      end if
    end if
'''


def read_accessibility_order_fields(process_name: str) -> dict[str, str]:
    field_specs = [
        ("symbol", ["代码", "股票代码", "证券代码"]),
        ("price", ["价格", "限价"]),
        ("quantity", ["数量", "委托数量", "买入量", "卖出量"]),
    ]
    blocks = "".join(_ax_read_field_block(key, labels) for key, labels in field_specs)
    output_lines = " & linefeed & ".join(
        f'{json.dumps(key + "|")} & {key.replace("_", "")}Value'
        for key, _ in field_specs
    )
    script = f'''
tell application "System Events"
  tell application process {json.dumps(process_name, ensure_ascii=False)}
    set fieldElements to every text field of window 1
{blocks}
    set rawText to (name of every UI element of window 1) as text
    return {output_lines} & linefeed & "raw_text|" & rawText
  end tell
end tell
'''
    result: dict[str, str] = {}
    for line in run_osascript_output(script).splitlines():
        if "|" not in line:
            continue
        key, value = line.split("|", 1)
        result[key] = value.replace("missing value", "")
    return result


def safe_read_accessibility_order_fields(process_name: str) -> dict[str, str]:
    try:
        return read_accessibility_order_fields(process_name)
    except (AppleScriptBridgeError, subprocess.SubprocessError):
        return {}


def fill_order_form_accessibility(
    process_name: str,
    symbol: str,
    quantity: int,
    limit_price: float,
) -> dict[str, str]:
    steps = [
        ax_type_text_field_near_label(process_name, ["代码", "股票代码", "证券代码"], symbol),
        ax_set_text_field_near_label(process_name, ["价格", "限价"], f"{limit_price:.3f}"),
        ax_set_text_field_near_label(
            process_name,
            ["数量", "委托数量", "买入量", "卖出量"],
            str(quantity),
        ),
    ]
    fields = read_accessibility_order_fields(process_name)
    expected = {
        "symbol": symbol,
        "price": f"{limit_price:.3f}",
        "quantity": str(quantity),
    }
    mismatches = {
        key: fields.get(key)
        for key, value in expected.items()
        if fields.get(key) != value
    }
    if mismatches:
        raise AppleScriptBridgeError(f"Accessibility order field readback mismatch: {mismatches}")
    fields["interaction_steps"] = json.dumps(steps, ensure_ascii=False)
    return fields


def click_at(x: int, y: int, *, expected_rect: tuple[int, int, int, int] | None = None) -> None:
    verify_app_window_state(DEFAULT_PROCESS_NAME, expected_rect=expected_rect)
    bounds_guard = ""
    if expected_rect is not None:
        expected_x, expected_y, expected_width, expected_height = expected_rect
        bounds_guard = (
            f"  set expectedBounds to {{{expected_x}, {expected_y}, {expected_width}, {expected_height}}}\n"
            f"  tell process {json.dumps(DEFAULT_PROCESS_NAME, ensure_ascii=False)}\n"
            "    set currentPosition to position of window 1\n"
            "    set currentSize to size of window 1\n"
            "    set currentBounds to {item 1 of currentPosition, item 2 of currentPosition, item 1 of currentSize, item 2 of currentSize}\n"
            "    if currentBounds is not expectedBounds then error \"THS bounds guard failed\"\n"
            "  end tell\n"
        )
    run_osascript(
        'tell application "System Events"\n'
        + bounds_guard
        + f"  click at {{{x}, {y}}}\n"
        "end tell"
    )


def input_text_at(x: int, y: int, text: str) -> None:
    run_osascript(
        f'tell application "{DEFAULT_APP_NAME}" to activate\n'
        'delay 0.1\n'
        f"set the clipboard to {json.dumps(text, ensure_ascii=False)}\n"
        'tell application "System Events"\n'
        f"  click at {{{x}, {y}}}\n"
        "  delay 0.25\n"
        f"  click at {{{x}, {y}}}\n"
        "  delay 0.35\n"
        '  keystroke "a" using command down\n'
        "  delay 0.2\n"
        '  keystroke "v" using command down\n'
        "  delay 0.2\n"
        "end tell"
    )


def type_text_at(x: int, y: int, text: str) -> None:
    """Type into a visually located field without using the clipboard."""
    run_osascript(
        f'tell application "{DEFAULT_APP_NAME}" to activate\n'
        'delay 0.1\n'
        'tell application "System Events"\n'
        f"  click at {{{x}, {y}}}\n"
        "  delay 0.25\n"
        '  keystroke "a" using command down\n'
        "  key code 51\n"
        f"  set requestedValue to {json.dumps(text, ensure_ascii=False)}\n"
        "  repeat with typedCharacter in characters of requestedValue\n"
        "    keystroke (typedCharacter as text)\n"
        "    delay 0.08\n"
        "  end repeat\n"
        "end tell"
    )


def click_relative(rect: tuple[int, int, int, int], rel_x: float, rel_y: float) -> None:
    x, y, width, height = rect
    click_at(int(x + width * rel_x), int(y + height * rel_y), expected_rect=rect)


def relative_point(rect: tuple[int, int, int, int], rel_x: float, rel_y: float) -> tuple[int, int]:
    x, y, width, height = rect
    return int(x + width * rel_x), int(y + height * rel_y)


def input_relative(rect: tuple[int, int, int, int], rel_x: float, rel_y: float, text: str) -> None:
    x, y = relative_point(rect, rel_x, rel_y)
    input_text_at(x, y, text)


def get_any_window_rect(process_name: str, owner_hint: str, title_hint: str) -> tuple[int, int, int, int] | None:
    rect = get_window_rect(process_name)
    if rect:
        return rect
    try:
        output = run_cached_swift_helper("window_list", SWIFT_WINDOWS, [], timeout=10).stdout
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None
    candidates: list[tuple[float, tuple[int, int, int, int]]] = []
    for window in json.loads(output):
        owner = str(window.get("owner_name", ""))
        title = str(window.get("window_name", ""))
        width = float(window.get("width", 0))
        height = float(window.get("height", 0))
        if width < 400 or height < 300:
            continue
        if owner != owner_hint:
            continue
        score = width * height
        if window.get("is_onscreen"):
            score *= 2
        candidates.append(
            (
                score,
                (
                    int(float(window.get("x", 0))),
                    int(float(window.get("y", 0))),
                    int(width),
                    int(height),
                ),
            )
        )
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def captured_window_rect(
    snapshot: dict[str, Any],
    fallback: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """Use the bounds proven by the latest capture, not pre-activation bounds."""
    raw_rect = snapshot.get("window_evidence", {}).get("window_rect")
    if isinstance(raw_rect, (list, tuple)) and len(raw_rect) == 4:
        try:
            rect = tuple(int(value) for value in raw_rect)
        except (TypeError, ValueError):
            return fallback
        if rect[2] > 0 and rect[3] > 0:
            return rect
    return fallback


def ocr_center_to_screen(
    image_path: Path,
    item: OcrText,
    rect: tuple[int, int, int, int],
    *,
    target: str | None = None,
) -> tuple[int, int]:
    target_fraction = 0.5
    if target and target in item.text and len(item.text) > len(target):
        target_index = item.text.find(target)
        target_fraction = (target_index + len(target) / 2) / max(len(item.text), 1)
    result = ocr_box_to_click_point(
        item,
        image_size_from_file(image_path),
        WindowRect(*rect),
        target_fraction=target_fraction,
    )
    return int(result.click_point.x), int(result.click_point.y)


def item_screen_center(
    image_path: Path,
    item: OcrText,
    rect: tuple[int, int, int, int],
    target: str,
) -> tuple[int, int, float, float]:
    screen_x, screen_y = ocr_center_to_screen(image_path, item, rect, target=target)
    rel_x = (screen_x - rect[0]) / rect[2]
    rel_y = (screen_y - rect[1]) / rect[3]
    return screen_x, screen_y, rel_x, rel_y


def find_ocr_text(
    image_path: Path,
    items: list[OcrText],
    rect: tuple[int, int, int, int],
    targets: list[str],
    *,
    min_rel_y: float = 0.0,
    max_rel_y: float = 1.0,
    min_rel_x: float = 0.0,
    max_rel_x: float = 1.0,
) -> tuple[str, OcrText, int, int] | None:
    candidates: list[tuple[float, str, OcrText, int, int]] = []
    for item in items:
        for target in targets:
            if target not in item.text:
                continue
            screen_x, screen_y, rel_x, rel_y = item_screen_center(image_path, item, rect, target)
            if not (min_rel_x <= rel_x <= max_rel_x and min_rel_y <= rel_y <= max_rel_y):
                continue
            exact_bonus = 0 if item.text.strip() == target else 2.0
            center_bias = abs((min_rel_y + max_rel_y) / 2 - rel_y) + abs((min_rel_x + max_rel_x) / 2 - rel_x)
            candidates.append((exact_bonus + center_bias, target, item, screen_x, screen_y))
    if not candidates:
        return None
    candidates.sort(key=lambda candidate: candidate[0])
    _, target, item, screen_x, screen_y = candidates[0]
    return target, item, screen_x, screen_y


def click_ocr_text(
    image_path: Path,
    items: list[OcrText],
    rect: tuple[int, int, int, int],
    targets: list[str],
    *,
    min_rel_y: float = 0.0,
    max_rel_y: float = 1.0,
    min_rel_x: float = 0.0,
    max_rel_x: float = 1.0,
    offset_x: int = 0,
    offset_y: int = 0,
    fallback: tuple[float, float] | None = None,
) -> dict[str, Any]:
    match = find_ocr_text(
        image_path,
        items,
        rect,
        targets,
        min_rel_y=min_rel_y,
        max_rel_y=max_rel_y,
        min_rel_x=min_rel_x,
        max_rel_x=max_rel_x,
    )
    if match:
        target, item, screen_x, screen_y = match
        screen_x += offset_x
        screen_y += offset_y
        click_at(screen_x, screen_y, expected_rect=rect)
        return {
            "method": "ocr_text",
            "target": target,
            "matched_text": item.text,
            "x": screen_x,
            "y": screen_y,
        }
    if fallback:
        screen_x, screen_y = relative_point(rect, fallback[0], fallback[1])
        click_at(screen_x, screen_y, expected_rect=rect)
        return {
            "method": "fallback_relative",
            "target": targets,
            "rel_x": fallback[0],
            "rel_y": fallback[1],
            "x": screen_x,
            "y": screen_y,
        }
    raise RuntimeError(f"OCR text not found: {targets}")


def find_order_form_input_points(
    image_path: Path,
    items: list[OcrText],
    rect: tuple[int, int, int, int],
    symbol: str,
) -> dict[str, tuple[int, int]]:
    """Locate the three simulated order-form input rows from OCR anchors."""
    points: dict[str, tuple[int, int]] = {}
    price_candidates: list[tuple[float, tuple[int, int]]] = []
    input_x = int(rect[0] + rect[2] * 0.22)
    for item in items:
        text = item.text.strip()
        screen_x, screen_y, rel_x, rel_y = item_screen_center(image_path, item, rect, text)
        if ("股票代码" in text or symbol in text) and 0.04 <= rel_y <= 0.16 and 0.03 <= rel_x <= 0.86:
            points.setdefault("symbol", (input_x, screen_y))
        if any(marker in text for marker in ["买入量", "卖出量", "委托数量"]) and 0.14 <= rel_y <= 0.24 and 0.08 <= rel_x <= 0.86:
            points["quantity"] = (input_x, screen_y)
        if re.fullmatch(r"\d+(?:\.\d+)?", text) and 0.11 <= rel_y <= 0.20 and 0.18 <= rel_x <= 0.70:
            center_bias = abs(rel_x - 0.45) + abs(rel_y - 0.145)
            price_candidates.append((center_bias, (input_x, screen_y)))
    if price_candidates:
        price_candidates.sort(key=lambda candidate: candidate[0])
        points["price"] = price_candidates[0][1]
    return points


def capture_ocr(
    *,
    app_name: str,
    process_name: str,
    symbol: str,
    label: str,
) -> tuple[Path, list[OcrText], dict[str, Any]]:
    timings: dict[str, float] = {}
    started = time.monotonic()
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    image_path = SCREENSHOTS / f"apple_bridge_navigation_{timestamp}_{label}.png"
    ready_started = time.monotonic()
    ready_state = ensure_app_window_ready(
        app_name,
        process_name=process_name,
        timeout_seconds=APP_READY_TIMEOUT_SECONDS,
    )
    rect = tuple(ready_state["window_rect"])
    window_id = get_coregraphics_window_id(
        process_name,
        app_name,
        timeout_seconds=max(0.0, APP_READY_TIMEOUT_SECONDS - (time.monotonic() - ready_started)),
    )
    timings["app_ready_ms"] = round((time.monotonic() - ready_started) * 1000, 1)
    if window_id is None:
        raise AppWindowGuardError(f"cannot identify {process_name} CoreGraphics window")
    verify_app_window_state(process_name, expected_rect=rect)
    capture_started = time.monotonic()
    capture_mode = capture_screenshot(image_path, window_id, rect)
    timings["capture_ms"] = round((time.monotonic() - capture_started) * 1000, 1)
    state_after_capture = verify_app_window_state(process_name, expected_rect=rect)
    ocr_started = time.monotonic()
    items = run_vision_ocr(image_path)
    timings["ocr_ms"] = round((time.monotonic() - ocr_started) * 1000, 1)
    state_after_ocr = verify_app_window_state(process_name, expected_rect=rect)
    current_window_id = get_coregraphics_window_id("同花顺", app_name)
    if current_window_id != window_id:
        raise AppWindowGuardError(
            f"{process_name} window id changed; expected={window_id} actual={current_window_id}; recapture required"
        )
    parse_started = time.monotonic()
    snapshot = build_snapshot(items, symbol, image_path, capture_mode, frontmost_process_name())
    timings["snapshot_parse_ms"] = round((time.monotonic() - parse_started) * 1000, 1)
    image_size = image_size_from_file(image_path)
    timings["total_ms"] = round((time.monotonic() - started) * 1000, 1)
    snapshot["window_evidence"] = {
        "process_name": process_name,
        "frontmost_process": frontmost_process_name(),
        "window_id": window_id,
        "window_rect": list(rect),
        "image_size": [int(image_size.width), int(image_size.height)],
        "capture_mode": capture_mode,
        "frontmost_after_capture": state_after_capture["frontmost"],
        "frontmost_after_ocr": state_after_ocr["frontmost"],
    }
    snapshot["timings_ms"] = timings
    return image_path, items, snapshot


def raw_text(items: list[OcrText]) -> str:
    return " || ".join(item.text for item in normalize_ocr_text(items))


def order_table_text(items: list[OcrText]) -> str:
    """Return OCR text from the center-left order/trade table, excluding the form and watchlist."""
    table_items = []
    for item in items:
        center_x = item.x + item.width / 2
        center_y_from_top = 1.0 - (item.y + item.height / 2)
        if 0.15 <= center_x <= 0.72 and 0.34 <= center_y_from_top <= 0.90:
            table_items.append(item)
    return raw_text(table_items)


def submission_record_matches(
    items: list[OcrText],
    *,
    page: str,
    side: str,
    symbol: str,
    quantity: int,
) -> bool:
    text = order_table_text(items)
    action = "买入" if side == "BUY" else "卖出"
    page_marker = "委托数量" if page == "orders" else "成交数量"
    return all(marker in text for marker in [page_marker, action, symbol, str(quantity)])


def verified_trade_fill_price(items: list[OcrText], *, symbol: str) -> float | None:
    """Read 成交均价 from the verified target-symbol row on the trades page."""
    headers = [item for item in items if item.text.replace(" ", "") == "成交均价"]
    symbol_items = [item for item in items if symbol in item.text]
    if not headers or not symbol_items:
        return None
    candidates: list[tuple[float, float]] = []
    for header in headers:
        header_x = header.x + header.width / 2
        row_items = [
            item
            for item in symbol_items
            if 0 < header.y - item.y <= 0.06
        ]
        for row_item in row_items:
            for item in items:
                text = item.text.replace(",", "").replace("，", "").strip()
                if not re.fullmatch(r"\d+(?:\.\d+)?", text):
                    continue
                item_x = item.x + item.width / 2
                if abs(item.y - row_item.y) > 0.018 or abs(item_x - header_x) > 0.035:
                    continue
                value = float(text)
                if value > 0:
                    candidates.append((abs(item_x - header_x) + abs(item.y - row_item.y), value))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


def is_trade_page(items: list[OcrText]) -> bool:
    text = raw_text(items)
    # The home-page news feed regularly contains words such as 大笔买入/卖出
    # while the sidebar always contains 交易. Those are not proof that the
    # trade workspace is open. Require account/order controls unique to it.
    strong_markers = [
        "确定买入",
        "资金明细",
        "添加开户券商",
        "模拟练习",
        "模拟交易",
        "模拟炒股",
    ]
    return any(marker in text for marker in strong_markers) or (
        "A股" in text
        and "持仓" in text
        and any(marker in text for marker in ["买入", "卖出", "撤单"])
    )


def is_search_page(items: list[OcrText]) -> bool:
    text = raw_text(items)
    return "搜索" in text and any(key in text for key in ["搜索历史", "搜索发现", "识股", "AI问答"])


def is_login_page(items: list[OcrText]) -> bool:
    text = raw_text(items)
    return "正在登录" in text or (
        any(marker in text for marker in ["密码登录", "短信登录"])
        and any(marker in text for marker in ["记住密码", "自动登录", "忘记密码"])
    )


def is_symbol_search_overlay(items: list[OcrText]) -> bool:
    text = raw_text(items)
    return "股票代码/简拼" in text and "取消" in text and ("最近搜索" in text or "搜索" in text)


def close_known_popup(rect: tuple[int, int, int, int], items: list[OcrText]) -> bool:
    text = raw_text(items)
    if any(marker in text for marker in ["委托买入确认", "确认买入", "委托卖出确认", "确认卖出"]):
        return False
    modal_markers = ["确定", "取消", "我知道", "关闭", "暂不", "以后再说"]
    if not any(marker in text for marker in modal_markers):
        return False
    # Conservative generic close points: center OK first, then top-right dialog area.
    click_relative(rect, 0.50, 0.54)
    time.sleep(0.5)
    return True


def read_order_intent(intent_path: Path) -> dict[str, Any]:
    payload = json.loads(intent_path.read_text(encoding="utf-8"))
    order = payload.get("order", payload)
    if not isinstance(order, dict):
        raise RuntimeError(f"invalid order intent: {intent_path}")
    return order


def _intent_side(intent_path: Path) -> str | None:
    try:
        return str(read_order_intent(intent_path).get("side") or "").upper() or None
    except Exception:
        return None


def expected_order_fields(order: dict[str, Any]) -> tuple[str, str, int, float]:
    symbol = str(order.get("symbol") or "").strip()
    side = str(order.get("side") or "").strip().upper()
    quantity = int(order.get("quantity"))
    limit_price = float(order.get("limit_price"))
    if not symbol:
        raise RuntimeError("order symbol is missing")
    if side not in {"BUY", "SELL"}:
        raise RuntimeError(f"AppleScript bridge only supports BUY or SELL, got {side}")
    if quantity <= 0:
        raise RuntimeError(f"invalid {side.lower()} quantity: {quantity}")
    if limit_price <= 0:
        raise RuntimeError(f"invalid {side.lower()} price: {limit_price}")
    return symbol, side, quantity, limit_price


def write_verification(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def is_simulated_order_form(items: list[OcrText], side: str) -> bool:
    text = raw_text(items)
    has_simulation = any(marker in text for marker in ["模拟交易", "模拟炒股", "模拟练习", "模拟账户"])
    action = "买入" if side == "BUY" else "卖出"
    availability = "可买" if side == "BUY" else "可卖"
    has_action = any(
        marker in text
        for marker in [f"{action}（模拟账户）", f"{action}(模拟账户)", f"确定{action}"]
    )
    has_inputs = (
        any(marker in text for marker in ["股票代码", "证券代码", "代码"])
        and any(marker in text for marker in ["限价", "价格"])
        and any(marker in text for marker in [availability, "数量"])
    )
    return has_simulation and (has_action or has_inputs)


def is_simulation_context(text: str) -> bool:
    return any(
        marker in text
        for marker in ["模拟交易", "模拟炒股", "模拟账户", "模拟练习", "楧拟练习", "梗拟练习"]
    )


def is_accessibility_trade_context(text: str) -> bool:
    """Return whether the AX tree looks like the trade area, not just its tab."""
    if is_simulation_context(text):
        return True
    return any(marker in text for marker in ["买入", "卖出", "撤单", "持仓", "添加账户", "账户管理"])


def navigate_accessibility_to_simulation_page(process_name: str, target: str) -> list[dict[str, Any]]:
    """Use semantic AX controls in the required 交易 -> 模拟 -> target order."""
    actions: list[dict[str, Any]] = []
    ax_text = read_accessibility_text(process_name)
    if not is_accessibility_trade_context(ax_text):
        actions.append(ax_press_named_control(process_name, ["交易"]))
        time.sleep(0.8)
        ax_text = read_accessibility_text(process_name)
    if not is_simulation_context(ax_text):
        actions.append(ax_press_named_control(process_name, ["模拟"]))
        time.sleep(0.8)
    actions.append(ax_press_named_control(process_name, [target]))
    return actions


def is_accessibility_order_form(fields: dict[str, str], side: str) -> bool:
    text = fields.get("raw_text", "")
    action = "买入" if side == "BUY" else "卖出"
    return (
        all(key in fields for key in ["symbol", "price", "quantity"])
        and is_simulation_context(text)
        and any(marker in text for marker in [action, f"确定{action}"])
    )


def is_simulated_buy_form(items: list[OcrText]) -> bool:
    return is_simulated_order_form(items, "BUY")


def is_simulated_sell_form(items: list[OcrText]) -> bool:
    return is_simulated_order_form(items, "SELL")


def confirm_fields_match(text: str, side: str, symbol: str, quantity: int, limit_price: float) -> tuple[bool, list[str]]:
    errors: list[str] = []
    action = "买入" if side == "BUY" else "卖出"
    confirmation_markers = [
        f"委托{action}确认",
        f"确认{action}",
        f"{action}委托",
        f"是否确认以上{action}委托",
    ]
    if not any(marker in text for marker in confirmation_markers):
        errors.append(f"missing {side.lower()} confirmation dialog")
    if symbol not in text:
        errors.append(f"missing symbol {symbol}")
    if str(quantity) not in text:
        errors.append(f"missing quantity {quantity}")
    price_text = f"{limit_price:.3f}".rstrip("0").rstrip(".")
    if price_text not in text and f"{limit_price:.3f}" not in text:
        errors.append(f"missing price {limit_price}")
    return not errors, errors


def buy_confirm_fields_match(text: str, symbol: str, quantity: int, limit_price: float) -> tuple[bool, list[str]]:
    return confirm_fields_match(text, "BUY", symbol, quantity, limit_price)


def sell_confirm_fields_match(text: str, symbol: str, quantity: int, limit_price: float) -> tuple[bool, list[str]]:
    return confirm_fields_match(text, "SELL", symbol, quantity, limit_price)


def extract_sellable_quantity(text: str) -> int | None:
    compact = re.sub(r"[\s,，]", "", text)
    for pattern in [r"可卖(?:数量)?[:：]?(\d+)(?:股)?", r"可用(?:数量)?[:：]?(\d+)(?:股)?"]:
        match = re.search(pattern, compact)
        if match:
            return int(match.group(1))
    return None


def verified_sellable_quantity_from_snapshot(snapshot: dict[str, Any], symbol: str) -> int | None:
    """Return a sellable quantity only from a fully validated simulation snapshot.

    This is a visual OCR fallback for form layouts that separate ``可用`` from
    its numeric value. Accessibility readback remains diagnostic evidence only;
    it does not replace the independently verified screenshot snapshot.
    """
    anchors = snapshot.get("anchors", {})
    if (
        snapshot.get("source") != "apple_vision_ocr"
        or snapshot.get("account_mode") != "simulation"
        or snapshot.get("validation_errors")
        or not all(anchors.get(key) for key in ("simulation", "position_area", "target_symbol"))
    ):
        return None
    positions = snapshot.get("positions")
    if not isinstance(positions, list):
        return None
    position = next(
        (
            item
            for item in positions
            if isinstance(item, dict) and item.get("symbol") == symbol
        ),
        None,
    )
    quantity = position.get("sellable_quantity") if isinstance(position, dict) else None
    return int(quantity) if isinstance(quantity, (int, float)) and int(quantity) >= 0 else None


def load_verified_sellable_quantity(snapshot_path: Path, symbol: str) -> int | None:
    """Load the account-synchronization sellable quantity for a sell."""
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return verified_sellable_quantity_from_snapshot(snapshot, symbol)


def final_order_quantity(
    side: str,
    requested_quantity: int,
    sellable_quantity: int | None,
) -> int:
    """Resolve the quantity sent to THS without changing the strategy intent."""
    if side != "SELL":
        return requested_quantity
    if sellable_quantity is None:
        raise RuntimeError("cannot verify sellable quantity from synchronized account data")
    quantity = min(requested_quantity, sellable_quantity)
    if quantity <= 0:
        raise RuntimeError(
            f"no sellable quantity available: requested={requested_quantity} "
            f"sellable={sellable_quantity}"
        )
    return quantity


def confirmation_dialog_items(items: list[OcrText]) -> list[OcrText]:
    """Restrict confirmation-field OCR to the centered native modal."""
    dialog_items = []
    for item in items:
        center_x = item.x + item.width / 2
        center_y_from_top = 1.0 - (item.y + item.height / 2)
        if 0.30 <= center_x <= 0.70 and 0.25 <= center_y_from_top <= 0.80:
            dialog_items.append(item)
    # Synthetic/unit OCR fixtures lack real Vision coordinates.
    return dialog_items or items


def resolved_interaction_method(interaction_mode: str, steps: list[dict[str, Any]]) -> str:
    if interaction_mode == "visual_only":
        return "visual_only"
    if any("accessibility_fallback" in str(step.get("label", "")) for step in steps):
        return "visual_fallback"
    return "accessibility"


def navigate_to_order_form(
    app_name: str,
    bundle_id: str | None,
    process_name: str,
    symbol: str,
    side: str,
    *,
    interaction_mode: str = DEFAULT_INTERACTION_MODE,
    allow_visual_fallback: bool = True,
) -> tuple[tuple[int, int, int, int], Path, list[OcrText], list[dict[str, Any]], bool]:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    activate_app(app_name, bundle_id)
    time.sleep(1.0)

    rect = get_any_window_rect(process_name, "同花顺", app_name)
    if rect is None:
        raise RuntimeError(f"cannot locate {process_name} window")

    steps: list[dict[str, Any]] = []
    symbol_selected = False

    def record(label: str) -> tuple[Path, list[OcrText], dict[str, Any]]:
        nonlocal rect
        image_path, items, snapshot = capture_ocr(
            app_name=app_name,
            process_name=process_name,
            symbol=symbol,
            label=label,
        )
        rect = captured_window_rect(snapshot, rect)
        diagnostic_path = image_path.with_suffix(".json")
        diagnostic = {
            "label": label,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "screenshot_path": str(image_path),
            "snapshot": snapshot,
            "ocr_items": [asdict(item) for item in normalize_ocr_text(items)],
        }
        diagnostic_path.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
        steps.append(
            {
                "label": label,
                "screenshot_path": str(image_path),
                "diagnostic_path": str(diagnostic_path),
                "account_mode": snapshot.get("account_mode"),
                "anchors": snapshot.get("anchors", {}),
                "warnings": snapshot.get("warnings", []),
                "validation_errors": snapshot.get("validation_errors", []),
            }
        )
        return image_path, items, snapshot

    label = side.lower()
    action = "买入" if side == "BUY" else "卖出"
    image_path, items, _ = record(f"{label}_opened")

    for attempt in range(12):
        if not is_login_page(items):
            break
        time.sleep(2.0)
        image_path, items, _ = record(f"{label}_login_wait_{attempt + 1}")
    if is_login_page(items):
        raise RuntimeError("THS login did not complete within 24 seconds; refusing navigation")

    if interaction_mode == "accessibility_first":
        try:
            steps[-1]["accessibility_actions"] = navigate_accessibility_to_simulation_page(
                process_name,
                action,
            )
            time.sleep(0.8)
            image_path, items, _ = record(f"{label}_accessibility_form")
            fields = read_accessibility_order_fields(process_name)
            steps[-1]["accessibility_fields"] = fields
            if not is_accessibility_order_form(fields, side):
                raise AppleScriptBridgeError("Accessibility order form verification failed")
            if not is_simulated_order_form(items, side):
                raise RuntimeError(
                    "Accessibility navigation succeeded but OCR simulation verification failed; "
                    "refusing visual fallback"
                )
            return rect, image_path, items, steps, False
        except (AppleScriptBridgeError, subprocess.SubprocessError) as exc:
            steps.append(
                {
                    "label": f"{label}_accessibility_fallback",
                    "interaction_method": "accessibility",
                    "error": str(exc),
                }
            )
            if not allow_visual_fallback:
                raise RuntimeError(
                    f"Accessibility navigation failed and visual fallback is disabled: {exc}"
                ) from exc

    close_known_popup(rect, items)
    if is_search_page(items):
        back_x, back_y = relative_point(rect, 0.030, 0.052)
        click_at(back_x, back_y, expected_rect=rect)
        steps[-1]["click"] = {"method": "fallback_relative", "target": ["<", "＜"], "rel_x": 0.030, "rel_y": 0.052, "x": back_x, "y": back_y}
        time.sleep(1.0)
        image_path, items, _ = record(f"{label}_search_back")
        close_known_popup(rect, items)

    if not is_trade_page(items):
        trade_x, trade_y = relative_point(rect, 0.728, 0.988)
        click_at(trade_x, trade_y, expected_rect=rect)
        steps[-1]["click"] = {"method": "fallback_relative", "target": ["交易"], "rel_x": 0.728, "rel_y": 0.988, "x": trade_x, "y": trade_y}
        time.sleep(1.2)
        image_path, items, _ = record(f"{label}_bottom_trade")
        close_known_popup(rect, items)

    if not is_trade_page(items):
        raise RuntimeError(f"failed to enter THS trade page; refusing to continue {label} flow")

    if "模拟交易" not in raw_text(items):
        click_meta = click_ocr_text(
            image_path,
            items,
            rect,
            ["模拟"],
            min_rel_y=0.0,
            max_rel_y=0.14,
            offset_y=16,
            fallback=(0.612, 0.042),
        )
        steps[-1]["click"] = click_meta
        time.sleep(1.5)
        image_path, items, _ = record(f"{label}_top_simulation")
        close_known_popup(rect, items)

    for attempt in range(2):
        click_meta = click_ocr_text(
            image_path,
            items,
            rect,
            [action],
            min_rel_y=0.07,
            max_rel_y=0.14,
            min_rel_x=0.05 if side == "BUY" else 0.20,
            max_rel_x=0.25 if side == "BUY" else 0.40,
            offset_y=20,
            fallback=(0.102, 0.102) if side == "BUY" else (0.300, 0.102),
        )
        steps[-1]["click"] = click_meta
        time.sleep(1.0)
        image_path, items, _ = record(f"{label}_form" if attempt == 0 else f"{label}_form_retry")
        if is_symbol_search_overlay(items):
            if symbol in raw_text(items):
                click_relative(rect, 0.142, 0.219)
                steps[-1]["click"] = {"method": "fallback_relative", "target": ["first_symbol_search_result"], "rel_x": 0.142, "rel_y": 0.219}
                symbol_selected = True
            else:
                click_relative(rect, 0.966, 0.154)
                steps[-1]["click"] = {"method": "fallback_relative", "target": ["cancel_symbol_search_overlay"], "rel_x": 0.966, "rel_y": 0.154}
            time.sleep(1.0)
            image_path, items, _ = record(f"{label}_symbol_selected")
        if is_simulated_order_form(items, side):
            break
    if not is_simulated_order_form(items, side):
        raise RuntimeError(f"failed to verify simulated {label} form")
    return rect, image_path, items, steps, symbol_selected


def navigate_to_buy_form(
    app_name: str, bundle_id: str | None, process_name: str, symbol: str
) -> tuple[tuple[int, int, int, int], Path, list[OcrText], list[dict[str, Any]], bool]:
    return navigate_to_order_form(app_name, bundle_id, process_name, symbol, "BUY")


def fill_order_form(
    rect: tuple[int, int, int, int],
    image_path: Path,
    items: list[OcrText],
    symbol: str,
    quantity: int,
    limit_price: float,
    side: str,
    *,
    symbol_selected: bool = False,
    process_name: str = DEFAULT_PROCESS_NAME,
    interaction_mode: str = DEFAULT_INTERACTION_MODE,
    allow_visual_fallback: bool = True,
) -> None:
    if interaction_mode == "accessibility_first":
        try:
            fill_order_form_accessibility(process_name, symbol, quantity, limit_price)
            return
        except (AppleScriptBridgeError, subprocess.SubprocessError) as exc:
            if not allow_visual_fallback:
                raise RuntimeError(
                    f"Accessibility form fill failed and visual fallback is disabled: {exc}"
                ) from exc

    input_points = find_order_form_input_points(image_path, items, rect, symbol)
    if not symbol_selected:
        symbol_x, symbol_y = input_points.get("symbol", relative_point(rect, 0.400, 0.112))
        type_text_at(symbol_x, symbol_y, symbol)
        time.sleep(0.8)
        click_relative(rect, 0.142, 0.219)
        time.sleep(1.0)
    price_x, price_y = input_points.get("price", relative_point(rect, 0.480, 0.145))
    input_text_at(price_x, price_y, f"{limit_price:.3f}")
    time.sleep(0.3)
    quantity_x, quantity_y = input_points.get("quantity", relative_point(rect, 0.480, 0.186))
    input_text_at(quantity_x, quantity_y, str(quantity))
    time.sleep(0.3)


def fill_buy_form(
    rect: tuple[int, int, int, int], image_path: Path, items: list[OcrText], symbol: str,
    quantity: int, limit_price: float, *, symbol_selected: bool = False,
) -> None:
    fill_order_form(rect, image_path, items, symbol, quantity, limit_price, "BUY", symbol_selected=symbol_selected)


def run_order(
    *,
    app_name: str,
    bundle_id: str | None,
    process_name: str,
    intent_path: Path,
    verification_path: Path,
    submit: bool,
    interaction_mode: str = DEFAULT_INTERACTION_MODE,
    allow_visual_fallback: bool = True,
) -> dict[str, Any]:
    order = read_order_intent(intent_path)
    symbol, side, requested_quantity, limit_price = expected_order_fields(order)
    label = side.lower()
    action = "买入" if side == "BUY" else "卖出"
    account_sellable_quantity = (
        load_verified_sellable_quantity(DEFAULT_ACCOUNT_SNAPSHOT, symbol)
        if side == "SELL"
        else None
    )
    quantity = final_order_quantity(side, requested_quantity, account_sellable_quantity)
    quantity_adjusted = quantity != requested_quantity

    # A validation-only invocation deliberately leaves the confirmation dialog
    # open. On the submit invocation, resume that exact OCR-verified dialog
    # instead of navigating through it or dismissing it blindly.
    activate_app(app_name, bundle_id)
    time.sleep(0.5)
    rect = get_any_window_rect(process_name, "同花顺", app_name)
    if rect is None:
        raise RuntimeError(f"cannot locate {process_name} window")
    image_path, items, snapshot = capture_ocr(
        app_name=app_name, process_name=process_name, symbol=symbol, label=f"{label}_resume"
    )
    text = raw_text(items)
    dialog_text = raw_text(confirmation_dialog_items(items))
    resumed_fields_match, _ = confirm_fields_match(dialog_text, side, symbol, quantity, limit_price)
    resumed = resumed_fields_match and is_simulation_context(text)
    verified_sellable_quantity = account_sellable_quantity
    steps: list[dict[str, Any]] = []

    if not resumed:
        rect, image_path, items, steps, symbol_selected = navigate_to_order_form(
            app_name,
            bundle_id,
            process_name,
            symbol,
            side,
            interaction_mode=interaction_mode,
            allow_visual_fallback=allow_visual_fallback,
        )
        fill_order_form(
            rect,
            image_path,
            items,
            symbol,
            quantity,
            limit_price,
            side,
            symbol_selected=symbol_selected,
            process_name=process_name,
            interaction_mode=interaction_mode,
            allow_visual_fallback=allow_visual_fallback,
        )
        image_path, items, snapshot = capture_ocr(
            app_name=app_name, process_name=process_name, symbol=symbol, label=f"{label}_filled"
        )
        if interaction_mode == "accessibility_first":
            accessibility_fields = read_accessibility_order_fields(process_name)
            expected_ax_fields = {
                "symbol": symbol,
                "price": f"{limit_price:.3f}",
                "quantity": str(quantity),
            }
            mismatched_ax_fields = {
                key: accessibility_fields.get(key)
                for key, expected_value in expected_ax_fields.items()
                if accessibility_fields.get(key) != expected_value
            }
            if mismatched_ax_fields:
                raise RuntimeError(f"Accessibility field readback failed: {mismatched_ax_fields}")
        if not is_simulated_order_form(items, side):
            raise RuntimeError(f"lost simulated {label} form after filling fields")
        button_targets = [f"确定{action}", f"{action}（模拟账户）", f"{action}(模拟账户)"]
        if interaction_mode == "accessibility_first":
            try:
                click_meta = ax_press_named_control(process_name, button_targets)
            except AppleScriptBridgeError:
                if not allow_visual_fallback:
                    raise
                click_meta = click_ocr_text(
                    image_path,
                    items,
                    rect,
                    button_targets,
                    min_rel_y=0.18,
                    max_rel_y=0.40,
                    min_rel_x=0.05,
                    max_rel_x=0.80,
                    fallback=(0.50, 0.248),
                )
        else:
            click_meta = click_ocr_text(
                image_path,
                items,
                rect,
                button_targets,
                min_rel_y=0.18,
                max_rel_y=0.40,
                min_rel_x=0.05,
                max_rel_x=0.80,
                fallback=(0.50, 0.248),
            )
        steps.append({"label": f"{label}_submit_button", "click": click_meta})
        time.sleep(1.2)
        image_path, items, snapshot = capture_ocr(
            app_name=app_name, process_name=process_name, symbol=symbol, label=f"{label}_confirm"
        )
        text = raw_text(items)
        dialog_text = raw_text(confirmation_dialog_items(items))

    ok, validation_errors = confirm_fields_match(dialog_text, side, symbol, quantity, limit_price)
    if not is_simulation_context(text):
        ok = False
        validation_errors.append("missing simulation account context")
    if not ok:
        result = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source": "applescript_vision_ocr",
            "account_mode": "simulation" if is_simulation_context(text) else "unknown",
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "requested_quantity": requested_quantity,
            "quantity_adjusted": quantity_adjusted,
            "limit_price": limit_price,
            "sellable_quantity": verified_sellable_quantity,
            "submitted": False,
            "screenshot_path": str(image_path),
            "validation_errors": validation_errors,
            "interaction_method": resolved_interaction_method(interaction_mode, steps),
            "accessibility_fields": (
                safe_read_accessibility_order_fields(process_name)
                if interaction_mode == "accessibility_first"
                else {}
            ),
            "steps": steps,
            "raw_ui_text": text,
        }
        write_verification(verification_path, result)
        return result

    submitted = False
    receipt_text = ""
    receipt_path: str | None = None
    order_path: str | None = None
    trade_path: str | None = None
    order_record_verified = False
    trade_record_verified = False
    fill_price: float | None = None
    submission_evidence: list[str] = []
    if submit:
        confirmation_targets = ["确认", f"确认{action}", f"确定{action}"]
        if interaction_mode == "accessibility_first":
            try:
                click_meta = ax_press_named_sheet_button(process_name, confirmation_targets)
            except AppleScriptBridgeError:
                if not allow_visual_fallback:
                    raise
                click_meta = click_ocr_text(
                    image_path,
                    items,
                    rect,
                    confirmation_targets,
                    min_rel_y=0.35,
                    max_rel_y=0.75,
                    min_rel_x=0.45,
                    max_rel_x=0.90,
                )
        else:
            click_meta = click_ocr_text(
                image_path,
                items,
                rect,
                confirmation_targets,
                min_rel_y=0.35,
                max_rel_y=0.75,
                min_rel_x=0.45,
                max_rel_x=0.90,
            )
        steps.append({"label": f"confirm_{label}", "click": click_meta})
        time.sleep(1.5)
        receipt_image_path, receipt_items, _ = capture_ocr(app_name=app_name, process_name=process_name, symbol=symbol, label=f"{label}_receipt")
        receipt_path = str(receipt_image_path)
        receipt_text = raw_text(receipt_items)
        if any(marker in receipt_text for marker in ["委托已提交", "委托成功", "合同号"]):
            submission_evidence.append("receipt")
            if interaction_mode == "accessibility_first":
                try:
                    receipt_click = ax_press_named_sheet_button(
                        process_name, ["确认", "确定", "我知道"]
                    )
                    steps.append({"label": "receipt_ok", "click": receipt_click})
                    time.sleep(0.5)
                except AppleScriptBridgeError:
                    pass

        if interaction_mode == "accessibility_first":
            orders_click = ax_press_named_control(process_name, ["委托"])
            steps.append({"label": "open_orders_for_verification", "click": orders_click})
            time.sleep(0.8)
            order_image, order_items, _ = capture_ocr(
                app_name=app_name,
                process_name=process_name,
                symbol=symbol,
                label=f"{label}_orders_verification",
            )
            order_path = str(order_image)
            order_record_verified = submission_record_matches(
                order_items,
                page="orders",
                side=side,
                symbol=symbol,
                quantity=quantity,
            )
            if order_record_verified:
                submission_evidence.append("orders")
            steps.append(
                {
                    "label": "verify_orders",
                    "screenshot_path": order_path,
                    "matched": order_record_verified,
                    "table_text": order_table_text(order_items),
                }
            )

            trades_click = ax_press_named_control(process_name, ["成交"])
            steps.append({"label": "open_trades_for_verification", "click": trades_click})
            time.sleep(0.8)
            trade_image, trade_items, _ = capture_ocr(
                app_name=app_name,
                process_name=process_name,
                symbol=symbol,
                label=f"{label}_trades_verification",
            )
            trade_path = str(trade_image)
            trade_record_verified = submission_record_matches(
                trade_items,
                page="trades",
                side=side,
                symbol=symbol,
                quantity=quantity,
            )
            if trade_record_verified:
                submission_evidence.append("trades")
                fill_price = verified_trade_fill_price(trade_items, symbol=symbol)
            steps.append(
                {
                    "label": "verify_trades",
                    "screenshot_path": trade_path,
                    "matched": trade_record_verified,
                    "fill_price": fill_price,
                    "table_text": order_table_text(trade_items),
                }
            )

        submitted = bool(submission_evidence)

    result = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": "applescript_vision_ocr",
        "account_mode": "simulation",
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "requested_quantity": requested_quantity,
        "quantity_adjusted": quantity_adjusted,
        "limit_price": limit_price,
        "fill_price": fill_price,
        "sellable_quantity": verified_sellable_quantity,
        "submitted": submitted,
        "screenshot_path": receipt_path or str(image_path),
        "confirmation_screenshot_path": str(image_path),
        "receipt_screenshot_path": receipt_path,
        "order_screenshot_path": order_path,
        "trade_screenshot_path": trade_path,
        "order_record_verified": order_record_verified,
        "trade_record_verified": trade_record_verified,
        "submission_evidence": submission_evidence,
        "validation_errors": (
            []
            if (not submit or submitted)
            else ["missing submission evidence from receipt, order list, and trade list"]
        ),
        "interaction_method": resolved_interaction_method(interaction_mode, steps),
        "accessibility_fields": (
            safe_read_accessibility_order_fields(process_name)
            if interaction_mode == "accessibility_first"
            else {}
        ),
        "steps": steps,
        "raw_ui_text": receipt_text or text,
    }
    write_verification(verification_path, result)
    return result


def run_buy_order(**kwargs: Any) -> dict[str, Any]:
    order = read_order_intent(kwargs["intent_path"])
    if str(order.get("side", "")).upper() != "BUY":
        raise RuntimeError(f"buy action requires BUY intent, got {order.get('side')}")
    return run_order(**kwargs)


def run_sell_order(**kwargs: Any) -> dict[str, Any]:
    order = read_order_intent(kwargs["intent_path"])
    if str(order.get("side", "")).upper() != "SELL":
        raise RuntimeError(f"sell action requires SELL intent, got {order.get('side')}")
    return run_order(**kwargs)


def navigate_to_holdings(
    app_name: str,
    bundle_id: str | None,
    process_name: str,
    symbol: str,
    *,
    interaction_mode: str = DEFAULT_INTERACTION_MODE,
    allow_visual_fallback: bool = True,
) -> dict[str, Any]:
    flow_started = time.monotonic()
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    activate_app(app_name, bundle_id)
    time.sleep(1.0)

    rect = get_any_window_rect(process_name, "同花顺", app_name)
    if rect is None:
        raise RuntimeError(f"cannot locate {process_name} window")

    steps: list[dict[str, Any]] = []

    def record(label: str) -> tuple[Path, list[OcrText], dict[str, Any]]:
        nonlocal rect
        image_path, items, snapshot = capture_ocr(
            app_name=app_name,
            process_name=process_name,
            symbol=symbol,
            label=label,
        )
        rect = captured_window_rect(snapshot, rect)
        diagnostic_path = image_path.with_suffix(".json")
        diagnostic = {
            "label": label,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "screenshot_path": str(image_path),
            "snapshot": snapshot,
            "ocr_items": [asdict(item) for item in normalize_ocr_text(items)],
        }
        diagnostic_path.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
        steps.append(
            {
                "label": label,
                "screenshot_path": str(image_path),
                "diagnostic_path": str(diagnostic_path),
                "account_mode": snapshot.get("account_mode"),
                "anchors": snapshot.get("anchors", {}),
                "warnings": snapshot.get("warnings", []),
                "validation_errors": snapshot.get("validation_errors", []),
            }
        )
        return image_path, items, snapshot

    image_path, items, snapshot = record("opened")

    for attempt in range(12):
        if not is_login_page(items):
            break
        time.sleep(2.0)
        image_path, items, snapshot = record(f"login_wait_{attempt + 1}")
    if is_login_page(items):
        raise RuntimeError("THS login did not complete within 24 seconds; refusing navigation")

    def holdings_result(
        current_items: list[OcrText],
        current_snapshot: dict[str, Any],
        *,
        method: str,
    ) -> dict[str, Any] | None:
        ordered_text = raw_text(current_items)
        if not (is_simulation_context(ordered_text) and "持仓" in ordered_text):
            return None
        position = next(
            (
                item
                for item in current_snapshot.get("positions", [])
                if item and item.get("symbol") == symbol
            ),
            None,
        )
        quantity = position.get("quantity") if isinstance(position, dict) else None
        sellable_quantity = (
            position.get("sellable_quantity") if isinstance(position, dict) else None
        )
        anchors = {
            "simulation": current_snapshot.get("account_mode") == "simulation",
            "holdings_page": True,
            "target_symbol": symbol in ordered_text
            or (
                isinstance(current_snapshot.get("market_value"), (int, float))
                and abs(float(current_snapshot["market_value"])) <= 0.01
            ),
            "target_position_quantity": isinstance(quantity, int)
            or (
                isinstance(current_snapshot.get("market_value"), (int, float))
                and abs(float(current_snapshot["market_value"])) <= 0.01
            ),
        }
        result = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source": "applescript_vision_ocr",
            "app": app_name,
            "process_name": process_name,
            "account_mode": "simulation" if anchors["simulation"] else "unknown",
            "page": "holdings",
            "symbol": symbol,
            "quantity": quantity,
            "sellable_quantity": sellable_quantity,
            "position": position,
            "account_snapshot": current_snapshot,
            "window_evidence": current_snapshot.get("window_evidence", {}),
            "anchors": anchors,
            "validation_errors": [name for name, ok in anchors.items() if not ok],
            "interaction_method": method,
            "steps": steps,
            "raw_ui_text": ordered_text,
        }
        result["timings_ms"] = {
            "navigation_total_ms": round((time.monotonic() - flow_started) * 1000, 1),
            **current_snapshot.get("timings_ms", {}),
        }
        return result

    initial_result = holdings_result(items, snapshot, method="already_on_holdings")
    if initial_result is not None and not snapshot.get("validation_errors"):
        return initial_result

    if interaction_mode == "accessibility_first":
        try:
            steps[-1]["accessibility_actions"] = navigate_accessibility_to_simulation_page(
                process_name,
                "持仓",
            )
            time.sleep(0.8)
            image_path, items, snapshot = record("holdings_accessibility")
            result = holdings_result(items, snapshot, method="accessibility")
            if result is not None:
                return result
            raise RuntimeError(
                "Accessibility holdings navigation succeeded but OCR verification failed; "
                "refusing visual fallback"
            )
        except (AppleScriptBridgeError, subprocess.SubprocessError) as exc:
            steps.append(
                {
                    "label": "holdings_accessibility_fallback",
                    "interaction_method": "accessibility",
                    "error": str(exc),
                }
            )
            # The sidebar's buttons are unnamed AXButtons; use OCR when it can
            # see the vertical label, otherwise bind the known slot geometry.
            image_path, items, _ = record("accessibility_failed_state")
            if not is_trade_page(items):
                # Press the nearest left-sidebar AX button; never use a global
                # coordinate click for this control.
                trade_match = find_ocr_text(
                    image_path,
                    items,
                    rect,
                    ["交易"],
                    min_rel_x=0.0,
                    max_rel_x=0.08,
                    min_rel_y=0.18,
                    max_rel_y=0.40,
                )
                if trade_match is None:
                    trade_x, trade_y = relative_point(rect, 0.015, 0.344)
                else:
                    _, _, trade_x, trade_y = trade_match
                trade_action = ax_press_sidebar_button_near_point(
                    process_name,
                    trade_x,
                    trade_y,
                )
                if trade_match is None:
                    trade_action["method"] = "accessibility_near_sidebar_geometry"
                    trade_action["geometry_anchor"] = trade_action.pop("ocr_anchor")
                steps[-1]["trade_navigation_accessibility"] = trade_action
                time.sleep(1.0)
                image_path, items, _ = record("trade_navigation_accessibility")
            if not is_trade_page(items):
                raise RuntimeError("Accessibility sidebar AXPress did not reach the trade page")

            if not is_simulation_context(raw_text(items)):
                try:
                    simulation_action = ax_press_named_control(process_name, ["模拟"])
                except AppleScriptBridgeError:
                    if not allow_visual_fallback:
                        raise RuntimeError(
                            "Accessibility simulation control unavailable and visual fallback is disabled"
                        )
                    simulation_action = click_ocr_text(
                        image_path,
                        items,
                        rect,
                        ["模拟"],
                        min_rel_y=0.0,
                        max_rel_y=0.14,
                        offset_y=16,
                    )
                steps[-1]["simulation_navigation_accessibility"] = simulation_action
                time.sleep(1.0)
                image_path, items, _ = record("simulation_navigation_accessibility")
            if not is_simulation_context(raw_text(items)):
                raise RuntimeError("Accessibility simulation navigation did not reach simulated trading")

            steps[-1]["accessibility_action"] = ax_press_named_control(process_name, ["持仓"])
            time.sleep(0.8)
            image_path, items, snapshot = record("holdings_after_accessibility_navigation")
            result = holdings_result(items, snapshot, method="ocr_anchor_accessibility")
            if result is not None:
                return result
            raise RuntimeError(
                "Safe OCR navigation reached THS trade area but holdings verification failed"
            )

    close_known_popup(rect, items)
    if is_search_page(items):
        back_x, back_y = relative_point(rect, 0.030, 0.052)
        click_at(back_x, back_y, expected_rect=rect)
        click_meta = {"method": "fallback_relative", "target": ["<", "＜"], "rel_x": 0.030, "rel_y": 0.052, "x": back_x, "y": back_y}
        steps[-1]["click"] = click_meta
        time.sleep(1.0)
        image_path, items, _ = record("search_back")
        close_known_popup(rect, items)

    # Bottom navigation is tiny and often missed by OCR; use the calibrated bottom tab position.
    trade_x, trade_y = relative_point(rect, 0.728, 0.988)
    click_at(trade_x, trade_y, expected_rect=rect)
    click_meta = {"method": "fallback_relative", "target": ["交易"], "rel_x": 0.728, "rel_y": 0.988, "x": trade_x, "y": trade_y}
    steps[-1]["click"] = click_meta
    time.sleep(1.2)
    image_path, items, _ = record("bottom_trade")
    close_known_popup(rect, items)
    if not is_trade_page(items):
        trade_x, trade_y = relative_point(rect, 0.728, 0.992)
        click_at(trade_x, trade_y, expected_rect=rect)
        click_meta = {
            "method": "fallback_relative",
            "target": ["交易"],
            "rel_x": 0.728,
            "rel_y": 0.992,
            "x": trade_x,
            "y": trade_y,
        }
        steps[-1]["click"] = click_meta
        time.sleep(1.2)
        image_path, items, _ = record("bottom_trade_retry")
        close_known_popup(rect, items)

    if not is_trade_page(items):
        raise RuntimeError("failed to enter THS trade page; refusing to click top navigation blindly")

    # Top title area: simulated trading entry/selector. Skip if already in simulated trading.
    if "模拟交易" not in raw_text(items):
        click_meta = click_ocr_text(
            image_path,
            items,
            rect,
            ["模拟"],
            min_rel_y=0.0,
            max_rel_y=0.14,
            offset_y=16,
            fallback=(0.612, 0.042),
        )
        steps[-1]["click"] = click_meta
        time.sleep(1.5)
        image_path, items, _ = record("top_simulation")
        close_known_popup(rect, items)

    # Simulated trading landing page shortcut: holdings.
    click_meta = click_ocr_text(
        image_path,
        items,
        rect,
        ["持仓", "特仓"],
        min_rel_y=0.18,
        max_rel_y=0.28,
        offset_y=-20,
        fallback=(0.699, 0.230),
    )
    steps[-1]["click"] = click_meta
    time.sleep(1.2)
    image_path, items, snapshot = record("holdings")

    ordered_text = raw_text(items)
    position = next((item for item in snapshot.get("positions", []) if item and item.get("symbol") == symbol), None)
    quantity = position.get("quantity") if isinstance(position, dict) else None
    sellable_quantity = position.get("sellable_quantity") if isinstance(position, dict) else None
    anchors = {
        "simulation": snapshot.get("account_mode") == "simulation"
        or any(key in ordered_text for key in ["模拟交易", "模拟练习区", "梗拟练习区", "模拟"]),
        "holdings_page": "持仓" in ordered_text,
        "target_symbol": symbol in ordered_text
        or (
            isinstance(snapshot.get("market_value"), (int, float))
            and abs(float(snapshot["market_value"])) <= 0.01
        ),
        "target_position_quantity": isinstance(quantity, int)
        or (
            isinstance(snapshot.get("market_value"), (int, float))
            and abs(float(snapshot["market_value"])) <= 0.01
        ),
    }
    validation_errors = [name for name, ok in anchors.items() if not ok]

    result = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": "applescript_vision_ocr",
        "app": app_name,
        "process_name": process_name,
        "account_mode": "simulation" if anchors["simulation"] else "unknown",
        "page": "holdings" if anchors["holdings_page"] else "unknown",
        "symbol": symbol,
        "quantity": quantity,
        "sellable_quantity": sellable_quantity,
        "position": position,
        "account_snapshot": snapshot,
        "window_evidence": snapshot.get("window_evidence", {}),
        "anchors": anchors,
        "validation_errors": validation_errors,
        "interaction_method": "visual_fallback",
        "steps": steps,
        "raw_ui_text": ordered_text,
    }
    result["timings_ms"] = {
        "navigation_total_ms": round((time.monotonic() - flow_started) * 1000, 1),
        **snapshot.get("timings_ms", {}),
    }
    return result


def main() -> int:
    global APP_READY_TIMEOUT_SECONDS
    parser = argparse.ArgumentParser(description="Open THS simulated trading pages and verify by OCR")
    parser.add_argument("--action", choices=["holdings", "order", "buy", "sell"], default="holdings")
    parser.add_argument("--app-name", default=DEFAULT_APP_NAME)
    parser.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID)
    parser.add_argument("--process-name", default=DEFAULT_PROCESS_NAME)
    parser.add_argument("--ready-timeout", type=float, default=APP_READY_TIMEOUT_SECONDS)
    parser.add_argument("--symbol", default="588330")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--intent", type=Path, default=SCREENSHOTS / "latest_order_intent.json")
    parser.add_argument("--verification", type=Path, default=DEFAULT_VERIFICATION)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument(
        "--interaction-mode",
        choices=["accessibility_first", "visual_only"],
        default=DEFAULT_INTERACTION_MODE,
    )
    parser.add_argument(
        "--no-visual-fallback",
        action="store_true",
        help="fail closed if Accessibility interaction is unavailable",
    )
    args = parser.parse_args()
    APP_READY_TIMEOUT_SECONDS = max(0.1, args.ready_timeout)

    try:
        if args.action in {"order", "buy", "sell"}:
            runner = {"order": run_order, "buy": run_buy_order, "sell": run_sell_order}[args.action]
            result = runner(
                app_name=args.app_name,
                bundle_id=args.bundle_id,
                process_name=args.process_name,
                intent_path=args.intent,
                verification_path=args.verification,
                submit=args.submit,
                interaction_mode=args.interaction_mode,
                allow_visual_fallback=not args.no_visual_fallback,
            )
        else:
            result = navigate_to_holdings(
                args.app_name,
                args.bundle_id,
                args.process_name,
                args.symbol,
                interaction_mode=args.interaction_mode,
                allow_visual_fallback=not args.no_visual_fallback,
            )
    except Exception as exc:
        result = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source": "applescript_vision_ocr",
            "app": args.app_name,
            "process_name": args.process_name,
            "account_mode": "unknown",
            "page": "unknown",
            "symbol": args.symbol,
            "side": _intent_side(args.intent) if args.action in {"order", "buy", "sell"} else None,
            "quantity": None,
            "limit_price": None,
            "sellable_quantity": None,
            "position": None,
            "submitted": False,
            "anchors": {
                "simulation": False,
                "holdings_page": False,
                "target_symbol": False,
                "target_position_quantity": False,
            },
            "validation_errors": ["bridge_runtime_error"],
            "error": str(exc),
        }
    output_path = args.verification if args.action in {"order", "buy", "sell"} else args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["validation_errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
