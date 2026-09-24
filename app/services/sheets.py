"""One-way mirror of leads into a Google Sheet (DB stays the source of truth)."""
import json

from app.services.errors import ProviderError

LEAD_HEADERS = [
    "lead_id", "campaign", "industry", "company", "category", "city", "address", "phone", "website",
    "company_emails", "facebook", "linkedin_company", "decision_maker", "title", "dm_email", "email_status",
    "dm_linkedin", "confidence", "confidence_level", "source_url", "google_maps", "rating", "crm_status",
    "notes", "updated_at",
]
TAB = "Leads"


def _client(sa_json: str):
    import gspread

    try:
        info = json.loads(sa_json)
    except json.JSONDecodeError as exc:
        raise ProviderError("Service account JSON is not valid JSON") from exc
    return gspread.service_account_from_dict(info), info.get("client_email", "")


def _worksheet(sa_json: str, spreadsheet_id: str):
    import gspread

    gc, _ = _client(sa_json)
    try:
        sh = gc.open_by_key(spreadsheet_id)
    except gspread.exceptions.APIError as exc:
        raise ProviderError(f"Cannot open spreadsheet (shared with the service account?): {exc}") from exc
    except gspread.exceptions.SpreadsheetNotFound as exc:
        raise ProviderError("Spreadsheet not found - share it with the service account email") from exc
    try:
        ws = sh.worksheet(TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=TAB, rows=1000, cols=len(LEAD_HEADERS))
    if ws.row_values(1) != LEAD_HEADERS:
        ws.update(range_name="A1", values=[LEAD_HEADERS])
    return sh, ws


def test_connection(sa_json: str, spreadsheet_id: str) -> str:
    sh, _ = _worksheet(sa_json, spreadsheet_id)
    return f"OK - opened '{sh.title}'"


def _col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def upsert_rows(sa_json: str, spreadsheet_id: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    _, ws = _worksheet(sa_json, spreadsheet_id)
    ids = ws.col_values(1)  # includes header
    index = {v: i + 1 for i, v in enumerate(ids) if i > 0}
    last_col = _col_letter(len(LEAD_HEADERS))
    updates, appends = [], []
    for row in rows:
        values = ["" if row.get(h) is None else str(row.get(h)) for h in LEAD_HEADERS]
        rid = str(row["lead_id"])
        if rid in index:
            r = index[rid]
            updates.append({"range": f"A{r}:{last_col}{r}", "values": [values]})
        else:
            appends.append(values)
    if updates:
        ws.batch_update(updates, value_input_option="RAW")
    if appends:
        ws.append_rows(appends, value_input_option="RAW")
    return len(rows)
