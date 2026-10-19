"""Build an executive Excel summary (pivot tables + formulas) from the reporting layer.

    python excel_report.py   ->  data/RCM_Executive_Summary.xlsx
"""
import sqlite3
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

BASE = Path(__file__).parent
DB_PATH = BASE / "data" / "rcm.db"
OUT = BASE / "data" / "RCM_Executive_Summary.xlsx"

with sqlite3.connect(DB_PATH) as conn:
    claims = pd.read_sql("SELECT * FROM vw_claims_enriched", conn)
    monthly = pd.read_sql("SELECT * FROM vw_monthly_kpis ORDER BY submit_month", conn)
    service = pd.read_sql("SELECT * FROM vw_service_line_kpis ORDER BY revenue_rank", conn)
    payer = pd.read_sql("SELECT * FROM vw_payer_kpis ORDER BY denial_rank", conn)
    denials = pd.read_sql("SELECT * FROM vw_denial_reasons ORDER BY denied_claims DESC", conn)
    aging = pd.read_sql("SELECT * FROM vw_ar_aging", conn)
    dar = pd.read_sql("SELECT * FROM vw_days_in_ar", conn)

claims["year"] = claims["submit_month"].str[:4]
pivot_rev = claims.pivot_table(index="service_line", columns="year", values="paid_amount",
                               aggfunc="sum", margins=True, margins_name="Total").round(0)
pivot_den = claims[claims.is_adjudicated == 1].pivot_table(
    index="payer", columns="service_line", values="is_denied", aggfunc="mean").round(3)

summary = pd.DataFrame({
    "KPI": ["Total claims", "Net revenue (collected)", "Denial rate", "Collection rate",
            "Outstanding AR", "Days in AR"],
    "Value": [len(claims), claims.paid_amount.sum(),
              claims.is_denied.sum() / claims.is_adjudicated.sum(),
              claims.loc[claims.status.isin(["Paid", "Partially Paid"]), "paid_amount"].sum()
              / claims.loc[claims.status.isin(["Paid", "Partially Paid"]), "allowed_amount"].sum(),
              claims.outstanding_ar.sum(), float(dar.days_in_ar.iloc[0])],
})

with pd.ExcelWriter(OUT, engine="openpyxl") as xw:
    summary.to_excel(xw, sheet_name="Summary", index=False)
    monthly.to_excel(xw, sheet_name="Monthly KPIs", index=False)
    pivot_rev.to_excel(xw, sheet_name="Revenue Pivot")
    pivot_den.to_excel(xw, sheet_name="Denial Rate Pivot")
    service.to_excel(xw, sheet_name="Service Lines", index=False)
    payer.to_excel(xw, sheet_name="Payers", index=False)
    denials.to_excel(xw, sheet_name="Denial Reasons", index=False)
    aging.to_excel(xw, sheet_name="AR Aging", index=False)

wb = load_workbook(OUT)
head_fill = PatternFill("solid", fgColor="1F4E78")
for ws in wb.worksheets:
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    for i, col in enumerate(ws.columns, 1):
        width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
        ws.column_dimensions[get_column_letter(i)].width = min(max(12, width + 2), 34)
    ws.freeze_panes = "B2"

# live formulas on the Summary sheet
s = wb["Summary"]
s["D1"], s["E1"] = "Target", "Status"
targets = {2: None, 3: None, 4: 0.10, 5: 0.95, 6: None, 7: 40}
for r, t in targets.items():
    if t is not None:
        s[f"D{r}"] = t
# denial rate: lower is better; collection rate: higher is better; days in AR: lower is better
s["E4"] = '=IF(B4<=D4,"On target","Above target")'
s["E5"] = '=IF(B5>=D5,"On target","Below target")'
s["E7"] = '=IF(B7<=D7,"On target","Above target")'
s["B2"].number_format = "#,##0"
for c in ("B3", "B6"):
    s[c].number_format = "$#,##0"
for c in ("B4", "B5", "D4", "D5"):
    s[c].number_format = "0.0%"
s["B7"].number_format = "0.0"
s.column_dimensions["A"].width = 26

# monthly sheet: conditional-free formula column for YoY-style revenue share
m = wb["Monthly KPIs"]
last = m.max_row
m.cell(1, m.max_column + 1, "Revenue share of total").font = Font(bold=True, color="FFFFFF")
m.cell(1, m.max_column).fill = head_fill
rev_col = [c.value for c in m[1]].index("net_revenue") + 1
rev_letter = get_column_letter(rev_col)
for r in range(2, last + 1):
    cell = m.cell(r, m.max_column, f"={rev_letter}{r}/SUM({rev_letter}$2:{rev_letter}${last})")
    cell.number_format = "0.0%"

wb.save(OUT)
print(f"Wrote {OUT}")
