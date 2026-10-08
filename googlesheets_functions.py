from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import os
import re
import threading
import time
import pandas as pd

MAX_REQUESTS_PER_MINUTE = 60
# Concurrent values.append calls can be assigned overlapping insertion points and
# report the same updatedRange, so serialise appends within this process. Use
# findRowByValue for the authoritative row - other processes/workers still race.
_append_lock = threading.Lock()
TIME_INTERVAL = 60 / MAX_REQUESTS_PER_MINUTE
SERVICE_ACCOUNT_FILE = os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE', 'inbound-footing-xxx-123.json')
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

def _get_service():
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return build('sheets', 'v4', credentials=creds)


def writeDF2Sheet(df: pd.DataFrame, sheet_name: str, spreadsheet_id: str):

    if df.empty:
        return

    time.sleep(TIME_INTERVAL)
    # fillna first: a NaN survives .astype(str) as the literal token NaN in the
    # request body, which is invalid JSON and makes Google reject the append.
    body = {"values": df.fillna("").astype(str).values.tolist()}
    _get_service().spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A2",
        valueInputOption='RAW',
        insertDataOption='INSERT_ROWS',
        body=body
    ).execute()

def writeRow2Sheet(row, sheet_name, spreadsheet_id ):
    time.sleep(TIME_INTERVAL)
    
    service = _get_service()

    row_data = row.values.tolist()
    values = [row_data]

    range_name = f'{sheet_name}!A2'

    # Call the Sheets API
    sheet = service.spreadsheets()
    body = {
        'values': values
    }
    result = sheet.values().append(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        valueInputOption='RAW',
        insertDataOption='INSERT_ROWS',
        body=body).execute()

    print(f"{result.get('updates').get('updatedCells')} cells updated.")


def appendRow2Sheet(row: dict, sheet_name: str, spreadsheet_id: str):
    """Append one row and return the 1-based row number it landed on.

    Used when a row has to be written up front and filled in later - the row
    number is the handle for those later updates.
    """
    time.sleep(TIME_INTERVAL)
    body = {"values": [[str(v) for v in row.values()]]}
    with _append_lock:
        result = _get_service().spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A2",
            valueInputOption='RAW',
            insertDataOption='INSERT_ROWS',
            body=body
        ).execute()

    # updatedRange looks like "hearSourceBatches!A7:F7"
    updated_range = result.get("updates", {}).get("updatedRange", "")
    match = re.search(r"![A-Z]+(\d+)", updated_range)
    return int(match.group(1)) if match else None


def _col_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def updateCells(values: list, sheet_name: str, spreadsheet_id: str, row: int, start_col_index: int):
    """Overwrite a horizontal run of cells in an existing row.

    start_col_index is 0-based (0 = column A).
    """
    if row is None:
        return
    time.sleep(TIME_INTERVAL)
    start = _col_letter(start_col_index)
    end = _col_letter(start_col_index + len(values) - 1)
    _get_service().spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!{start}{row}:{end}{row}",
        valueInputOption='RAW',
        body={"values": [[str(v) for v in values]]}
    ).execute()


def findRowContaining(sheet_name: str, spreadsheet_id: str, col_range: str, needle: str):
    """Return the 1-based row number where `needle` appears in any cell of
    `col_range` (e.g. "F:Z").

    The row number reported by an append is not trustworthy when appends run
    concurrently - two of them can be told they landed on the same row. Matching
    on something unique already inside the row is authoritative.

    The range spans several columns because a long value is split across
    consecutive cells, and the unique text may land in any of them depending on
    where the split falls.

    Returns the LAST match, so if the same payload is ever re-sent the most
    recent row wins.
    """
    time.sleep(TIME_INTERVAL)
    result = _get_service().spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!{col_range}"
    ).execute()
    found = None
    for i, row in enumerate(result.get("values", []), start=1):
        if any(needle in cell for cell in row):
            found = i
    return found
