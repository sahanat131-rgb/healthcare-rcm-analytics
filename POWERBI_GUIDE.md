# Power BI Build Guide (RCM Dashboard)

Power BI Desktop is Windows-only, which fits your setup. Use the free Desktop app.

## 1. Load the data
1. Run `python pipeline.py` so `data/powerbi/` is filled.
2. Power BI Desktop -> **Get data -> Text/CSV**. Load these (Transform -> check types, then Close & Apply):
   - `vw_claims_enriched.csv` (fact table)
   - `vw_monthly_kpis.csv`, `vw_service_line_kpis.csv`, `vw_payer_kpis.csv`
   - `vw_denial_reasons.csv`, `vw_ar_aging.csv`, `vw_days_in_ar.csv`
3. Set `service_date`, `submit_date`, `payment_date` to **Date**, `submit_month` to **Text**.

To refresh daily: put the CSV folder on a path Power BI can see, then **Home -> Refresh** (or publish and schedule refresh in Power BI Service).

## 2. Model
Create a Date table (Modeling -> New table):
```DAX
DimDate = ADDCOLUMNS(CALENDAR(DATE(2024,1,1), DATE(2026,1,31)),
    "Year", YEAR([Date]), "Month", FORMAT([Date], "yyyy-MM"), "MonthNo", MONTH([Date]))
```
Relate `DimDate[Date]` -> `vw_claims_enriched[submit_date]` (one-to-many). Use `vw_claims_enriched` as the main source for visuals so slicers (Service line, Payer, Payer type, Date) filter everything.

## 3. DAX measures (table: `vw_claims_enriched`)
```DAX
Total Claims = COUNTROWS(vw_claims_enriched)
Adjudicated Claims = CALCULATE([Total Claims], vw_claims_enriched[is_adjudicated] = 1)
Denied Claims = CALCULATE([Total Claims], vw_claims_enriched[is_denied] = 1)
Denial Rate = DIVIDE([Denied Claims], [Adjudicated Claims])

Net Revenue = SUM(vw_claims_enriched[paid_amount])
Billed Amount = SUM(vw_claims_enriched[billed_amount])
Allowed Amount = SUM(vw_claims_enriched[allowed_amount])

Collection Rate =
DIVIDE(
    CALCULATE(SUM(vw_claims_enriched[paid_amount]),
        vw_claims_enriched[status] IN {"Paid", "Partially Paid"}),
    CALCULATE(SUM(vw_claims_enriched[allowed_amount]),
        vw_claims_enriched[status] IN {"Paid", "Partially Paid"}))

Outstanding AR = SUM(vw_claims_enriched[outstanding_ar])
Underpaid Amount = SUM(vw_claims_enriched[underpaid_amount])
Avg Days to Pay = AVERAGE(vw_claims_enriched[days_to_pay])

Days in AR =
VAR AsOf = MAX(vw_claims_enriched[submit_date])
VAR Last90Allowed =
    CALCULATE(SUM(vw_claims_enriched[allowed_amount]),
        DATESINPERIOD(DimDate[Date], AsOf, -90, DAY))
RETURN DIVIDE(SUM(vw_claims_enriched[outstanding_ar]), DIVIDE(Last90Allowed, 90))

Net Revenue PM = CALCULATE([Net Revenue], DATEADD(DimDate[Date], -1, MONTH))
Revenue MoM % = DIVIDE([Net Revenue] - [Net Revenue PM], [Net Revenue PM])
Denial Rate Color = IF([Denial Rate] > 0.10, "#C0392B", "#2E7D32")
```
Note: Days in AR is meant for the whole data set (no month slicer). With a date slicer it will recompute for that window, so put a note on the card or use `ALL(DimDate)` inside the measure if you want it fixed.

## 4. Pages and visuals

**Page 1: Executive Summary**
- 5 KPI cards: Net Revenue, Denial Rate, Collection Rate, Days in AR, Outstanding AR (conditional color on Denial Rate using `Denial Rate Color`).
- Line chart: `DimDate[Month]` vs Net Revenue, with a second line for Denial Rate (secondary axis).
- Bar chart: Net Revenue by `service_line` (sorted descending).
- Slicers: Year, Payer type, Service line.

**Page 2: Denials**
- Bar chart: `denial_reason` vs Denied Claims (or `pct_of_denials` from `vw_denial_reasons`).
- Matrix: rows `payer`, columns `service_line`, values Denial Rate with a color scale (red = high).
- Table: top payers by denial rate (`vw_payer_kpis`, sort by `denial_rank`).
- Insight text box: Missing information and prior authorization account for roughly 44% of denials, which are the most fixable at front-end registration.

**Page 3: AR and Payments**
- Column chart: `aging_bucket` vs outstanding AR (`vw_ar_aging`), order 0-30, 31-60, 61-90, 90+.
- Bar chart: Avg Days to Pay by payer.
- Card: Days in AR; Underpaid Amount.

**Page 4: Data Quality (nice for the interview)**
- Load `qc_log.csv` and `quarantine_summary.csv` (exported by the pipeline) and show a table of PASS/FAIL checks plus a bar chart of rejected rows by reason.

## 5. Finish
- Rename pages, align visuals to a grid, one color theme (blue for revenue, red only for denials).
- Add tooltips and a title on each visual that states the takeaway.
- Save as `RCM_Dashboard.pbix`, export a PDF, and take screenshots for your GitHub README.
