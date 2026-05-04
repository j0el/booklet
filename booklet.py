#!/usr/bin/env python3
"""
calendar_booklet_clean.py

Create a 12-month pocket-planner style booklet PDF from Google Calendar.

What it does
------------
- Reads 12 consecutive months from ALL calendars in your Google account
- Starts from the current month by default, or from any month using MMYYYY
- Color-codes each event using that calendar's color from Google Calendar
- Makes one small booklet page per month
- Imposes pages into booklet order for printing on US Letter paper
- Produces 3 double-sided sheets total for 12 months

Print settings
--------------
- Paper size: Letter
- Orientation: Landscape
- Duplex: Flip on short edge
- Scale: 97%
- Pages per sheet in printer dialog: 1
  (the PDF is already imposed in booklet order)

Setup
-----
1) In Google Cloud, enable the Google Calendar API.
2) Create OAuth credentials for a Desktop app.
3) Download the OAuth JSON file and save it as credentials.json
   in the same folder as this script.
4) First run opens a browser to authorize access and saves token.json.

Install
-------
python3 -m pip install google-api-python-client google-auth-httplib2 \
    google-auth-oauthlib reportlab pypdf python-dateutil

Examples
--------
python3 calendar_booklet_fixed.py
python3 calendar_booklet_fixed.py --start 072026 --calendar-id primary --calendar-id "holidays@group.v.calendar.google.com"
python3 calendar_booklet_fixed.py --start 112026 --output planner.pdf
"""

from __future__ import annotations

import argparse
import calendar
import tempfile
import re
from dotenv import load_dotenv
import os
load_dotenv()
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple

from dateutil import parser as date_parser
from pypdf import PdfReader, PdfWriter, Transformation
from pypdf._page import PageObject
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# Physical sheet = letter landscape
SHEET_W, SHEET_H = landscape(letter)  # 792 x 612

# Each booklet page = half-letter portrait (fills one half of a landscape letter sheet)
BOOKLET_PAGE_W = landscape(letter)[0] / 2   # 396
BOOKLET_PAGE_H = landscape(letter)[1]       # 612

MARGIN_X = 12
MARGIN_Y = 12

HEADER_COLOR = HexColor("#17324d")
ACCENT_COLOR = HexColor("#3a86ff")
GRID_COLOR = HexColor("#d9e2ec")
TEXT_MUTED = HexColor("#5b6b7a")
WEEKEND_BG = HexColor("#f7f9fc")

DEFAULT_EVENT_COLOR = "#3a86ff"


@dataclass
class EventItem:
    start: datetime
    end: datetime
    title: str
    all_day: bool
    location: str = ""
    color: str = DEFAULT_EVENT_COLOR  # hex string from the calendar's backgroundColor


def parse_start_mmYYYY(value: str) -> Tuple[int, int]:
    if len(value) != 6 or not value.isdigit():
        raise argparse.ArgumentTypeError(
            "Start date must be in MMYYYY format, for example 042026"
        )
    month = int(value[:2])
    year = int(value[2:])
    if not 1 <= month <= 12:
        raise argparse.ArgumentTypeError("Month in MMYYYY must be between 01 and 12")
    return month, year


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a 12-month Google Calendar pocket-planner booklet PDF."
    )
    parser.add_argument(
        "--start",
        type=parse_start_mmYYYY,
        default=None,
        help="Starting month in MMYYYY format, e.g. 042026. "
             "If omitted, starts with the current month.",
    )
    parser.add_argument(
        "--calendar-id",
        action="append",
        default=None,
        help="Calendar ID to include. Repeat to include multiple calendars. "
             "If omitted, ALL calendars in your account are included.",
    )
    parser.add_argument(
        "--credentials",
        default="credentials.json",
        help="Path to OAuth client JSON",
    )
    parser.add_argument(
        "--token",
        default="token.json",
        help="Path to stored OAuth token",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output PDF path",
    )
    return parser.parse_args()


def get_calendar_service(credentials_path: Path, token_path: Path):
    creds = None

    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception:
            print(f"⚠️  Could not read token file ({token_path}). Re-authorizing...")
            token_path.unlink(missing_ok=True)
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                print(f"⚠️  Token refresh failed ({exc}). Removing stale token and re-authorizing...")
                token_path.unlink(missing_ok=True)
                creds = None

        if not creds or not creds.valid:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"Missing {credentials_path}. Download your Desktop OAuth JSON "
                    f"and save it as {credentials_path.name}."
                )
            print("🌐 Opening browser for Google authorization...")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path),
                SCOPES,
            )
            creds = flow.run_local_server(port=0)
            print("✅ Authorization successful.")

        token_path.write_text(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def add_months(year: int, month: int, offset: int) -> Tuple[int, int]:
    total = (year * 12 + (month - 1)) + offset
    return total // 12, (total % 12) + 1


def month_bounds(year: int, month: int) -> Tuple[datetime, datetime]:
    """Return local-time month bounds as timezone-aware datetimes.

    Google Calendar stores events in their own time zones.  Using the local
    timezone here avoids edge cases where UTC month boundaries can shift
    late-night events into or out of the requested month.
    """
    local_tz = datetime.now().astimezone().tzinfo
    start = datetime(year, month, 1, tzinfo=local_tz)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=local_tz)
    else:
        end = datetime(year, month + 1, 1, tzinfo=local_tz)
    return start, end


def parse_google_datetime(obj: Dict) -> Tuple[datetime, bool]:
    if "dateTime" in obj:
        return date_parser.isoparse(obj["dateTime"]), False
    if "date" in obj:
        return datetime.fromisoformat(obj["date"]).replace(tzinfo=timezone.utc), True
    raise ValueError(f"Unsupported Google date object: {obj}")


def list_all_calendars(service) -> List[dict]:
    items: List[dict] = []
    page_token = None
    while True:
        result = service.calendarList().list(pageToken=page_token, maxResults=250).execute()
        items.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return items


def resolve_calendars(service, requested_ids: List[str] | None) -> Dict[str, str]:
    """
    Returns a dict of {calendar_id: hex_color_string} for all calendars to include.

    If --calendar-id flags were given, only those IDs are used (color looked up from
    the account if available, else default blue). Otherwise ALL calendars in the
    account are included, each with its own backgroundColor.
    """
    available = list_all_calendars(service)
    id_to_color: Dict[str, str] = {}
    id_to_summary: Dict[str, str] = {}

    for item in available:
        cal_id = item.get("id")
        bg = item.get("backgroundColor") or DEFAULT_EVENT_COLOR
        summary = item.get("summary", cal_id)
        if cal_id:
            id_to_color[cal_id] = bg
            id_to_summary[cal_id] = summary

    if requested_ids:
        result: Dict[str, str] = {}
        for cal_id in requested_ids:
            color = id_to_color.get(cal_id, DEFAULT_EVENT_COLOR)
            result[cal_id] = color
            print(f"  📅 {id_to_summary.get(cal_id, cal_id)}  →  {color}")
        return result

    # Use all calendars
    print(f"  Including {len(id_to_color)} calendar(s):")
    for cal_id, color in id_to_color.items():
        print(f"    📅 {id_to_summary.get(cal_id, cal_id)}  →  {color}")

    if not id_to_color:
        raise ValueError("No calendars found in your Google account.")

    return id_to_color


def fetch_month_events(
    service,
    calendar_id: str,
    year: int,
    month: int,
    color: str = DEFAULT_EVENT_COLOR,
) -> Dict[int, List[EventItem]]:
    start, end = month_bounds(year, month)

    page_token = None
    raw_items: List[dict] = []

    while True:
        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                pageToken=page_token,
                maxResults=2500,
            )
            .execute()
        )
        raw_items.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    by_day: Dict[int, List[EventItem]] = {}
    _, last_day = calendar.monthrange(year, month)

    for raw in raw_items:
        if raw.get("status") == "cancelled":
            continue
        summary = (raw.get("summary") or "(No title)").strip() or "(No title)"
        location = (raw.get("location") or "").strip()

        # Individual events can override the calendar color via colorId;
        # we honour that if present, otherwise fall back to the calendar color.
        event_color = raw.get("colorId")  # Google uses named color IDs here
        # colorId maps to specific hex values in Google's palette; we ignore the
        # mapping complexity and just use the calendar-level color for simplicity.
        # If you want per-event color overrides, map colorId → hex here.
        resolved_color = normalize_hex_color(color)

        start_dt, start_all_day = parse_google_datetime(raw["start"])
        end_dt, _ = parse_google_datetime(raw["end"])

        if start_all_day:
            current = start_dt.date()
            last_inclusive = (end_dt - timedelta(days=1)).date()
            while current <= last_inclusive:
                if current.year == year and current.month == month:
                    by_day.setdefault(current.day, []).append(
                        EventItem(
                            start=datetime.combine(current, datetime.min.time(), tzinfo=timezone.utc),
                            end=datetime.combine(current, datetime.min.time(), tzinfo=timezone.utc),
                            title=summary,
                            all_day=True,
                            location=location,
                            color=resolved_color,
                        )
                    )
                current += timedelta(days=1)
        else:
            current = start_dt.date()
            last_touch = end_dt.date()
            while current <= last_touch:
                if current.year == year and current.month == month:
                    if current == start_dt.date():
                        day_start = start_dt
                    else:
                        day_start = datetime.combine(
                            current, datetime.min.time(), tzinfo=start_dt.tzinfo
                        )

                    if current == end_dt.date():
                        day_end = end_dt
                    else:
                        day_end = datetime.combine(
                            current, datetime.max.time(), tzinfo=end_dt.tzinfo
                        )

                    by_day.setdefault(current.day, []).append(
                        EventItem(
                            start=day_start,
                            end=day_end,
                            title=summary,
                            all_day=False,
                            location=location,
                            color=resolved_color,
                        )
                    )
                current += timedelta(days=1)

    for day in range(1, last_day + 1):
        by_day.setdefault(day, [])
        by_day[day].sort(key=lambda e: (not e.all_day, e.start, e.title.lower()))

    return by_day


def merge_month_events(month_event_maps: List[Dict[int, List[EventItem]]]) -> Dict[int, List[EventItem]]:
    merged: Dict[int, List[EventItem]] = {}
    for day_map in month_event_maps:
        for day, items in day_map.items():
            merged.setdefault(day, []).extend(items)

    for day in merged:
        merged[day].sort(key=lambda e: (not e.all_day, e.start, e.title.lower()))
    return merged


def compact_time(dt: datetime) -> str:
    hour = dt.strftime("%-I")
    minute = dt.strftime("%M")
    suffix = dt.strftime("%p").lower()[0]
    if minute == "00":
        return f"{hour}{suffix}"
    return f"{hour}:{minute}{suffix}"


def clean_title(text: str) -> str:
    text = re.sub(r"return\s+with\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bbirthday\b", "b'day", text, flags=re.IGNORECASE)
    return " ".join(text.split()).strip()


def format_event(event: EventItem) -> str:
    title = clean_title(event.title)
    if event.all_day:
        return f"• {title}"
    return f"{compact_time(event.start)} {title}"


def normalize_hex_color(value: str | None) -> str:
    """Return a safe #rrggbb color, falling back to DEFAULT_EVENT_COLOR."""
    if not value:
        return DEFAULT_EVENT_COLOR
    v = value.strip()
    if not v.startswith("#"):
        v = "#" + v
    if len(v) != 7:
        return DEFAULT_EVENT_COLOR
    try:
        int(v[1:], 16)
    except ValueError:
        return DEFAULT_EVENT_COLOR
    return v.lower()


def _luminance(hex_color: str) -> float:
    """Return perceptual luminance 0–1 for a hex color string like '#rrggbb'."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255
    return 0.299 * r + 0.587 * g + 0.114 * b


def _tint(hex_color: str, factor: float = 0.82) -> HexColor:
    """
    Return a light tint of hex_color by blending it toward white.
    factor=0 → white, factor=1 → original color.
    """
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r2 = int(r + (255 - r) * (1 - factor))
    g2 = int(g + (255 - g) * (1 - factor))
    b2 = int(b + (255 - b) * (1 - factor))
    return HexColor(f"#{r2:02x}{g2:02x}{b2:02x}")


def draw_month_page(
    pdf_path: Path,
    year: int,
    month: int,
    events_by_day: Dict[int, List[EventItem]],
    run_date_str: str,
) -> None:
    c = canvas.Canvas(str(pdf_path), pagesize=(BOOKLET_PAGE_W, BOOKLET_PAGE_H))
    width, height = BOOKLET_PAGE_W, BOOKLET_PAGE_H

    c.setFillColorRGB(1, 1, 1)
    c.rect(0, 0, width, height, fill=1, stroke=0)

    # Thin header band, flush to top
    header_h = 18
    c.setFillColor(HEADER_COLOR)
    c.rect(0, height - header_h, width, header_h, fill=1, stroke=0)

    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(
        width / 2,
        height - 12,
        f"{calendar.month_name[month]} {year} (as of {run_date_str})",
    )

    c.setStrokeColor(ACCENT_COLOR)
    c.setLineWidth(0.8)
    c.line(MARGIN_X, height - header_h, width - MARGIN_X, height - header_h)

    # Grid area pulled tight to header
    grid_x = MARGIN_X
    grid_y = MARGIN_Y
    grid_w = width - 2 * MARGIN_X
    grid_h = height - header_h - 1 - MARGIN_Y

    dow_h = 15
    rows = 6
    cols = 7
    cell_w = grid_w / cols
    cell_h = (grid_h - dow_h) / rows

    day_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

    c.setFillColor(ACCENT_COLOR)
    c.rect(grid_x, grid_y + rows * cell_h, grid_w, dow_h, fill=1, stroke=0)

    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 7.5)
    for i, name in enumerate(day_names):
        x = grid_x + i * cell_w + cell_w / 2
        c.drawCentredString(x, grid_y + rows * cell_h + 4.0, name)

    month_cal = calendar.Calendar(firstweekday=6)  # Sunday
    weeks = month_cal.monthdayscalendar(year, month)
    while len(weeks) < 6:
        weeks.append([0] * 7)

    for r in range(rows):
        for col in range(cols):
            x = grid_x + col * cell_w
            y = grid_y + (rows - 1 - r) * cell_h

            if col in (0, 6):
                c.setFillColor(WEEKEND_BG)
                c.rect(x, y, cell_w, cell_h, fill=1, stroke=0)

            c.setStrokeColor(GRID_COLOR)
            c.setLineWidth(0.45)
            c.rect(x, y, cell_w, cell_h, fill=0, stroke=1)

            day_num = weeks[r][col]
            if day_num == 0:
                continue

            c.setFillColor(HEADER_COLOR)
            c.setFont("Helvetica-Bold", 7.5)
            c.drawRightString(x + cell_w - 3, y + cell_h - 9, str(day_num))

            inner_x = x + 2
            inner_w = cell_w - 4
            event_top = y + cell_h - 16
            available_h = max(0, cell_h - 18)

            events = sorted(
                events_by_day.get(day_num, []),
                key=lambda ev: (not ev.all_day, ev.start, ev.title.lower()),
            )

            used_h = 0.0
            shown_count = 0

            for ev in events:
                event_text = format_event(ev)
                wrapped_all = simpleSplit(event_text, "Helvetica", 4.8, inner_w - 2.5)

                line_h = 5.0
                pad_top = 2.0
                pad_bottom = 1.5

                remaining_h = available_h - used_h
                max_lines_that_fit = int((remaining_h - pad_top - pad_bottom) // line_h)
                if max_lines_that_fit < 1:
                    break

                wrapped = wrapped_all[:max_lines_that_fit]
                line_count = max(1, len(wrapped))
                box_h = pad_top + pad_bottom + line_count * line_h

                if used_h + box_h > available_h:
                    break

                box_y = event_top - used_h - box_h

                # --- Color-coded event box ---
                # All-day: use a light tint of the calendar color as background,
                #          with the calendar color as text.
                # Timed:   use an even lighter tint as background,
                #          with the calendar color as text.
                cal_color_hex = normalize_hex_color(ev.color)

                if ev.all_day:
                    box_bg = _tint(cal_color_hex, factor=0.35)
                    c.setFillColor(box_bg)
                    c.roundRect(inner_x, box_y, inner_w, box_h, 1.6, fill=1, stroke=0)
                    # Left accent stripe in full calendar color
                    c.setFillColor(HexColor(cal_color_hex))
                    c.rect(inner_x, box_y, 2.0, box_h, fill=1, stroke=0)
                    c.setFillColor(black)
                    c.setFont("Helvetica-Bold", 4.8)
                else:
                    box_bg = _tint(cal_color_hex, factor=0.18)
                    c.setFillColor(box_bg)
                    c.roundRect(inner_x, box_y, inner_w, box_h, 1.6, fill=1, stroke=0)
                    # Left accent stripe in full calendar color
                    c.setFillColor(HexColor(cal_color_hex))
                    c.rect(inner_x, box_y, 2.0, box_h, fill=1, stroke=0)
                    c.setFillColor(black)
                    c.setFont("Helvetica", 4.8)

                text_y = box_y + box_h - pad_top - 3.5
                for i, line in enumerate(wrapped):
                    if i == len(wrapped) - 1 and len(wrapped_all) > len(wrapped) and len(line) > 1:
                        line = line[:-1] + "…"
                    c.drawString(inner_x + 3.0, text_y, line[:140])
                    text_y -= line_h

                used_h += box_h + 0.8
                shown_count += 1

            remaining = len(events) - shown_count
            if remaining > 0 and used_h + 5 <= available_h:
                c.setFillColor(TEXT_MUTED)
                c.setFont("Helvetica-Oblique", 4.7)
                c.drawString(inner_x + 1.2, event_top - used_h - 3.6, f"+{remaining}")

    c.showPage()
    c.save()


def build_month_pages(
    service,
    cal_id_to_color: Dict[str, str],
    start_year: int,
    start_month: int,
    temp_dir: Path,
    run_date_str: str,
) -> List[Path]:
    month_paths: List[Path] = []
    calendar_ids = list(cal_id_to_color.keys())

    for offset in range(12):
        year, month = add_months(start_year, start_month, offset)
        print(f"  Fetching {calendar.month_name[month]} {year}…")
        all_event_maps = [
            fetch_month_events(service, cal_id, year, month, color=cal_id_to_color[cal_id])
            for cal_id in calendar_ids
        ]
        events_by_day = merge_month_events(all_event_maps)
        event_count = sum(len(items) for items in events_by_day.values())
        print(f"    → {event_count} event instance(s) found")
        pdf_path = temp_dir / f"month_{offset + 1:02d}_{year}_{month:02d}.pdf"
        draw_month_page(pdf_path, year, month, events_by_day, run_date_str)
        month_paths.append(pdf_path)

    return month_paths


def booklet_pairs(total_pages: int) -> List[Tuple[int, int]]:
    if total_pages % 4 != 0:
        raise ValueError("Booklet page count must be a multiple of 4.")

    pairs: List[Tuple[int, int]] = []
    left = total_pages
    right = 1

    while right < left:
        pairs.append((left, right))
        right += 1
        left -= 1

        pairs.append((right, left))
        right += 1
        left -= 1

    return pairs


def impose_booklet(month_page_paths: List[Path], output_pdf: Path) -> None:
    total_pages = len(month_page_paths)
    if total_pages != 12:
        raise ValueError(f"Expected exactly 12 month pages, got {total_pages}.")

    page_map = {i + 1: PdfReader(str(path)).pages[0] for i, path in enumerate(month_page_paths)}
    writer = PdfWriter()

    for left_num, right_num in booklet_pairs(total_pages):
        sheet = PageObject.create_blank_page(width=SHEET_W, height=SHEET_H)

        left_src = page_map[left_num]
        right_src = page_map[right_num]

        left_tx = Transformation().translate(tx=0, ty=0)
        right_tx = Transformation().translate(tx=SHEET_W / 2, ty=0)

        sheet.merge_transformed_page(left_src, left_tx)
        sheet.merge_transformed_page(right_src, right_tx)

        writer.add_page(sheet)

    with output_pdf.open("wb") as f:
        writer.write(f)


def main() -> None:
    args = parse_args()

    if args.start is None:
        today = datetime.now().astimezone()
        start_month, start_year = today.month, today.year
        print(f"🗓  No --start supplied; starting with current month: {start_month:02d}{start_year}")
    else:
        start_month, start_year = args.start
    credentials_path = Path(
        os.getenv("GOOGLE_CREDENTIALS_JSON", args.credentials)
    ).expanduser().resolve()

    token_path = Path(
        os.getenv("GOOGLE_TOKEN_JSON", args.token)
    ).expanduser().resolve()



    if args.output:
        output_pdf = Path(args.output).expanduser().resolve()
    else:
        output_pdf = Path(f"calendar_booklet_{start_month:02d}{start_year}.pdf").resolve()

    run_date_str = datetime.now().strftime("%-m/%-d/%y")
    service = get_calendar_service(credentials_path, token_path)

    print("🗓  Resolving calendars…")
    cal_id_to_color = resolve_calendars(service, args.calendar_id)
    print(f"  → {len(cal_id_to_color)} calendar(s) selected.\n")

    print("📄 Building month pages…")
    with tempfile.TemporaryDirectory(prefix="calendar_booklet_") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        month_pages = build_month_pages(
            service,
            cal_id_to_color,
            start_year,
            start_month,
            tmpdir,
            run_date_str,
        )
        impose_booklet(month_pages, output_pdf)

    print(f"\n✅ Created: {output_pdf}")
    print()
    print("Print settings:")
    print("  Paper: Letter")
    print("  Orientation: Landscape")
    print("  Duplex: Flip on short edge")
    print("  Scale: 97%")
    print("  Pages per sheet in printer dialog: 1")
    print("  (PDF already imposed for booklet printing)")
    print()
    print("Booklet side order:")
    for i, (left, right) in enumerate(booklet_pairs(12), start=1):
        print(f"  Side {i}: left page {left}, right page {right}")


if __name__ == "__main__":
    main()
