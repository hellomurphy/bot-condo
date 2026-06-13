import json
import os
from datetime import datetime

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter

import config

TIER_COLORS = {
    "must_call":  ("FFD700", True),   # Gold + bold
    "shortlist":  ("90EE90", False),  # Light Green
    "need_info":  ("ADD8E6", False),  # Light Blue
    "maybe":      ("FFFFFF", False),  # White
}

COLUMNS = [
    ("Tier",              "tier"),
    ("Score",             "final_total"),
    ("Rent (฿)",          "monthly_rent"),
    ("Move-in Cost",      "move_in_cost"),
    ("Condo / Area",      "condo_name"),
    ("Location",          "location_text"),
    ("Station",           "station_name"),
    ("Size (sqm)",        "size_sqm"),
    ("Room Type",         "room_type"),
    ("Floor",             "floor"),
    ("Furnishing",        "furnishing"),
    ("Washer",            "has_washer"),
    ("Parking",           "has_parking"),
    ("Contract (mo.)",    "contract_min_months"),
    ("Available",         "available_date"),
    ("Agent/Owner",       "agent_or_owner"),
    ("Duplicate",         "duplicate_flag"),
    ("Risk Flags",        "risk_flags"),
    ("Questions to Ask",  "questions_to_ask"),
    ("Vision Score",      "vision_score"),
    ("Post Link",         "post_url"),
    ("Group",             "group_url"),
    ("Scraped At",        "scraped_at"),
]

TIER_DISPLAY = {
    "must_call": "Must Call",
    "shortlist": "Shortlist",
    "need_info": "Need Info",
    "maybe": "Maybe",
    "skip": "Skip",
}


def _format_bool(val) -> str:
    if val is None:
        return ""
    return "Yes" if val else "No"


def _format_json_list(val) -> str:
    if not val:
        return ""
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except Exception:
            return val
    return "\n".join(f"• {item}" for item in val)


def _row_to_values(row) -> list:
    def get(field):
        try:
            return row[field]
        except (IndexError, KeyError):
            return None

    values = []
    for header, field in COLUMNS:
        val = get(field)
        if field == "tier":
            val = TIER_DISPLAY.get(val, val or "")
        elif field in ("has_washer", "has_parking", "has_fridge", "has_wifi", "pet_allowed", "duplicate_flag"):
            val = _format_bool(val)
        elif field in ("risk_flags", "questions_to_ask", "missing_fields"):
            val = _format_json_list(val)
        elif field == "final_total" and val is not None:
            val = round(float(val), 1)
        elif field in ("monthly_rent", "move_in_cost", "move_in_cost_stated") and val is not None:
            val = f"{int(val):,}"
        values.append(val)
    return values


def _apply_sheet_style(ws, rows_data: list[list], tier: str | None = None):
    headers = [h for h, _ in COLUMNS]

    # Header row
    header_fill = PatternFill("solid", fgColor="2F4F4F")
    header_font = Font(bold=True, color="FFFFFF", size=10)
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(wrap_text=True, vertical="center")

    # Data rows
    for row_vals in rows_data:
        ws.append(row_vals)

    # Color rows based on tier column (index 0)
    if tier is None:
        # All Active sheet — color by tier value in cell
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
            cell_tier = row[0].value
            slug = {v: k for k, v in TIER_DISPLAY.items()}.get(cell_tier, "")
            if slug in TIER_COLORS:
                color, bold = TIER_COLORS[slug]
                fill = PatternFill("solid", fgColor=color)
                for cell in row:
                    cell.fill = fill
                    if bold:
                        cell.font = Font(bold=True)
    else:
        if tier in TIER_COLORS:
            color, bold = TIER_COLORS[tier]
            fill = PatternFill("solid", fgColor=color)
            for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
                for cell in row:
                    cell.fill = fill
                    if bold:
                        cell.font = Font(bold=True)

    # Wrap text + auto-width for key columns
    for col_idx, (header, field) in enumerate(COLUMNS, 1):
        col_letter = get_column_letter(col_idx)
        ws.column_dimensions[col_letter].width = _col_width(field)
        for cell in ws[col_letter]:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def _col_width(field: str) -> int:
    widths = {
        "tier": 12,
        "final_total": 8,
        "monthly_rent": 12,
        "move_in_cost": 14,
        "condo_name": 24,
        "location_text": 22,
        "station_name": 18,
        "size_sqm": 10,
        "room_type": 12,
        "floor": 8,
        "furnishing": 12,
        "has_washer": 8,
        "has_parking": 8,
        "contract_min_months": 13,
        "available_date": 14,
        "agent_or_owner": 13,
        "duplicate_flag": 10,
        "risk_flags": 28,
        "questions_to_ask": 40,
        "vision_score": 12,
        "post_url": 30,
        "group_url": 30,
        "scraped_at": 20,
    }
    return widths.get(field, 14)


def export_excel(rows: list) -> str:
    os.makedirs(config.RESULTS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
    path = os.path.join(config.RESULTS_DIR, f"rental_{ts}.xlsx")

    # Separate by tier
    by_tier: dict[str, list] = {
        "must_call": [], "shortlist": [], "need_info": [], "maybe": [], "all": []
    }
    for row in rows:
        tier = row["tier"] if hasattr(row, "__getitem__") else getattr(row, "tier", "skip")
        vals = _row_to_values(row)
        if tier in by_tier:
            by_tier[tier].append(vals)
        by_tier["all"].append(vals)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        # Write placeholder sheets to init the workbook
        for sheet_name in ["Must Call", "Shortlist", "Need Info", "Maybe", "All Active"]:
            pd.DataFrame().to_excel(writer, sheet_name=sheet_name, index=False)

    wb = load_workbook(path)
    sheet_map = {
        "Must Call":  ("must_call",  by_tier["must_call"]),
        "Shortlist":  ("shortlist",  by_tier["shortlist"]),
        "Need Info":  ("need_info",  by_tier["need_info"]),
        "Maybe":      ("maybe",      by_tier["maybe"]),
        "All Active": (None,         by_tier["all"]),
    }
    for sheet_name, (tier_slug, data) in sheet_map.items():
        ws = wb[sheet_name]
        # Clear placeholder content
        for row in ws:
            for cell in row:
                cell.value = None
        _apply_sheet_style(ws, data, tier=tier_slug)

    wb.save(path)
    return path
