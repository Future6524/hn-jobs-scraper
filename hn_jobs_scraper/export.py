"""CSV / Excel / Google Sheets writers."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .parser import EXPORT_COLUMNS, JobPost

URL_COLUMNS = {"apply_link", "hn_url"}
EMAIL_COLUMNS = {"apply_email"}
MAX_COL_WIDTH = 60
HEADER_FILL = PatternFill("solid", start_color="FF6600")  # HN orange


def to_dataframe(posts: list[JobPost]) -> pd.DataFrame:
    df = pd.DataFrame([p.to_row() for p in posts], columns=EXPORT_COLUMNS)
    if df["match"].eq("").all():
        df = df.drop(columns=["match", "matched_keywords"])
    return df


def write_csv(df: pd.DataFrame, path: Path) -> Path:
    df.to_csv(path, index=False, encoding="utf-8-sig")  # BOM so Excel opens UTF-8 cleanly
    return path


def write_xlsx(df: pd.DataFrame, path: Path, sheet_name: str = "Jobs") -> Path:
    df.to_excel(path, index=False, sheet_name=sheet_name, engine="openpyxl")

    wb = load_workbook(path)
    ws = wb[sheet_name]
    headers = [c.value for c in ws[1]]

    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center")

    link_font = Font(color="0563C1", underline="single")
    for col_idx, name in enumerate(headers, start=1):
        if name not in URL_COLUMNS | EMAIL_COLUMNS:
            continue
        for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx):
            cell = row[0]
            if not cell.value:
                continue
            target = f"mailto:{cell.value}" if name in EMAIL_COLUMNS else str(cell.value)
            cell.hyperlink = target
            cell.font = link_font

    for col_idx, name in enumerate(headers, start=1):
        values = [str(name)] + ["" if v is None else str(v) for v in df[name].tolist()]
        width = min(max(len(v) for v in values) + 2, MAX_COL_WIDTH)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)
    return path


def push_to_google_sheet(df: pd.DataFrame, sheet_id: str, worksheet: str = "Jobs") -> str:
    """Replace the given worksheet with the dataframe. Returns the sheet URL.

    GOOGLE_SHEETS_CREDENTIALS may be a path to a service-account JSON file or
    the JSON content itself. Share the sheet with the service account email.
    """
    import gspread  # optional dependency, imported lazily

    creds = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "").strip()
    if not creds:
        raise RuntimeError("GOOGLE_SHEETS_CREDENTIALS is not set")
    if creds.startswith("{"):
        client = gspread.service_account_from_dict(json.loads(creds))
    else:
        client = gspread.service_account(filename=os.path.expanduser(creds))

    spreadsheet = client.open_by_key(sheet_id)
    try:
        ws = spreadsheet.worksheet(worksheet)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=worksheet, rows=len(df) + 1, cols=len(df.columns))

    values = [df.columns.tolist()] + df.fillna("").astype(str).values.tolist()
    ws.update(values, "A1", value_input_option="USER_ENTERED")
    ws.format("1:1", {"textFormat": {"bold": True}})
    ws.freeze(rows=1)
    return spreadsheet.url
