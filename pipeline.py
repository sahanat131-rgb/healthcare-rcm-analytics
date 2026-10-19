"""RCM pipeline: extract -> validate -> de-identify -> load -> model -> QC -> export.

Run it daily (see README for Windows Task Scheduler). It is idempotent: every run
rebuilds the tables from the files in data/raw, so a failed or partial run is
fixed simply by running it again.

    python pipeline.py
"""
import hashlib
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

BASE = Path(__file__).parent
RAW = BASE / "data" / "raw"
DB_PATH = BASE / "data" / "rcm.db"
OUT = BASE / "data" / "powerbi"
SQL_DIR = BASE / "sql"
LOG_DIR = BASE / "logs"
SALT = os.environ.get("RCM_SALT", "change-this-salt")  # keep the real salt out of git

LOG_DIR.mkdir(exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "pipeline.log"), logging.StreamHandler()],
)
log = logging.getLogger("rcm")

DATE_COLS = ["service_date", "submit_date", "payment_date"]
REQUIRED = ["claim_id", "patient_id", "service_line", "payer", "service_date",
            "submit_date", "billed_amount", "allowed_amount", "paid_amount", "status"]


def extract(retries: int = 3, wait: float = 2.0) -> pd.DataFrame:
    """Read every billing_claims*.csv. Retries so a file still being written does not fail the run."""
    for attempt in range(1, retries + 1):
        try:
            files = sorted(RAW.glob("billing_claims*.csv"))
            if not files:
                raise FileNotFoundError(f"No billing_claims*.csv in {RAW}")
            df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
            log.info("Extracted %s rows from %s file(s)", f"{len(df):,}", len(files))
            return df
        except Exception as exc:  # noqa: BLE001
            log.warning("Extract attempt %s/%s failed: %s", attempt, retries, exc)
            if attempt == retries:
                raise
            time.sleep(wait)


def validate(df: pd.DataFrame):
    """Split into clean rows and quarantined rows (with a reason for each)."""
    missing_cols = [c for c in REQUIRED if c not in df.columns]
    if missing_cols:
        raise ValueError(f"Source file is missing columns: {missing_cols}")

    df = df.copy()
    for c in DATE_COLS:
        df[c] = pd.to_datetime(df[c], errors="coerce")

    reason = pd.Series("", index=df.index)

    def flag(mask, text):
        nonlocal reason
        reason = reason.where(~(mask & (reason == "")), text)

    flag(df["claim_id"].duplicated(keep="first"), "duplicate_claim_id")
    flag(df["payer"].isna() | (df["payer"].astype(str).str.strip() == ""), "missing_payer")
    flag(df["billed_amount"].isna() | (df["billed_amount"] < 0)
         | df["allowed_amount"].isna() | (df["allowed_amount"] < 0), "invalid_amount")
    flag(df["paid_amount"] > df["allowed_amount"] + 0.01, "paid_exceeds_allowed")
    flag(df["submit_date"].isna() | df["service_date"].isna(), "invalid_date")
    flag(df["submit_date"] < df["service_date"], "submit_before_service")

    bad = reason != ""
    clean = df[~bad].copy()
    quarantine = df[bad].copy()
    quarantine["reject_reason"] = reason[bad]
    log.info("Validation: %s clean, %s quarantined %s", f"{len(clean):,}", len(quarantine),
             quarantine["reject_reason"].value_counts().to_dict())
    return clean, quarantine


def deidentify(df: pd.DataFrame) -> pd.DataFrame:
    """HIPAA-style handling: replace patient_id with a salted hash before the reporting layer."""
    df = df.copy()
    df["patient_key"] = df["patient_id"].astype(str).map(
        lambda v: hashlib.sha256((SALT + v).encode()).hexdigest()[:16])
    return df.drop(columns=["patient_id"])


def load(conn, clean, quarantine, source_rows, source_billed):
    for c in DATE_COLS:
        clean[c] = clean[c].dt.strftime("%Y-%m-%d")
        quarantine[c] = quarantine[c].dt.strftime("%Y-%m-%d")
    quarantine = quarantine.drop(columns=["patient_id"], errors="ignore")  # keep PHI out of side tables
    clean["denial_reason"] = clean["denial_reason"].fillna("")

    clean.to_sql("fact_claims", conn, if_exists="replace", index=False)
    quarantine.to_sql("quarantine_claims", conn, if_exists="replace", index=False)
    pd.read_csv(RAW / "dim_payer.csv").to_sql("dim_payer", conn, if_exists="replace", index=False)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_fact_month ON fact_claims(submit_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_fact_sl ON fact_claims(service_line, payer)")
    conn.executescript((SQL_DIR / "views.sql").read_text(encoding="utf-8"))
    conn.commit()
    log.info("Loaded fact_claims, quarantine_claims, dim_payer and reporting views")


def reconcile(conn, source_rows, source_billed) -> pd.DataFrame:
    """Compare the source file to the reporting layer and record PASS/FAIL for each check."""
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    fact_rows = q("SELECT COUNT(*) FROM fact_claims")
    quar_rows = q("SELECT COUNT(*) FROM quarantine_claims")
    fact_billed = q("SELECT COALESCE(SUM(billed_amount),0) FROM fact_claims")
    quar_billed = q("SELECT COALESCE(SUM(billed_amount),0) FROM quarantine_claims")
    view_rows = q("SELECT SUM(total_claims) FROM vw_monthly_kpis")
    view_paid = q("SELECT SUM(net_revenue) FROM vw_monthly_kpis")
    fact_paid = q("SELECT SUM(paid_amount) FROM fact_claims")
    sl_paid = q("SELECT SUM(net_revenue) FROM vw_service_line_kpis")
    orphan_payers = q("""SELECT COUNT(*) FROM fact_claims f
                         LEFT JOIN dim_payer p ON p.payer = f.payer WHERE p.payer IS NULL""")
    dup_ids = q("SELECT COUNT(*) - COUNT(DISTINCT claim_id) FROM fact_claims")

    checks = [
        ("row_count: source = clean + quarantined", source_rows, fact_rows + quar_rows),
        ("billed_total: source = clean + quarantined", round(source_billed, 2),
         round(fact_billed + quar_billed, 2)),
        ("row_count: clean fact = monthly KPI view", fact_rows, view_rows),
        ("net_revenue: fact = monthly KPI view", round(fact_paid, 2), round(view_paid, 2)),
        ("net_revenue: fact = service-line view", round(fact_paid, 2), round(sl_paid, 2)),
        ("claims with payer missing from dim_payer", 0, orphan_payers),
        ("duplicate claim_ids in fact", 0, dup_ids),
    ]
    res = pd.DataFrame(checks, columns=["check", "expected", "actual"])
    res["status"] = ["PASS" if abs(float(e) - float(a)) < 0.05 else "FAIL"
                     for e, a in zip(res.expected, res.actual)]
    res["run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    res.to_sql("qc_log", conn, if_exists="append", index=False)
    for _, r in res.iterrows():
        log.log(logging.INFO if r.status == "PASS" else logging.ERROR,
                "QC %s | %s | expected=%s actual=%s", r.status, r.check, r.expected, r.actual)
    return res


def export_for_powerbi(conn):
    """Power BI reads these CSVs directly (no ODBC driver needed)."""
    for name in ["vw_claims_enriched", "vw_monthly_kpis", "vw_monthly_trend", "vw_service_line_kpis",
                 "vw_payer_kpis", "vw_denial_reasons", "vw_ar_aging", "vw_days_in_ar"]:
        pd.read_sql(f"SELECT * FROM {name}", conn).to_csv(OUT / f"{name}.csv", index=False)
    pd.read_sql("SELECT * FROM qc_log", conn).to_csv(OUT / "qc_log.csv", index=False)
    pd.read_sql("SELECT reject_reason, COUNT(*) AS rows_rejected FROM quarantine_claims GROUP BY reject_reason",
                conn).to_csv(OUT / "quarantine_summary.csv", index=False)
    log.info("Exported views to %s", OUT)


def main() -> int:
    raw = extract()
    source_rows = len(raw)
    source_billed = float(raw["billed_amount"].fillna(0).sum())
    clean, quarantine = validate(raw)
    clean = deidentify(clean)
    quarantine = quarantine  # patient_id dropped in load()

    with sqlite3.connect(DB_PATH) as conn:
        load(conn, clean, quarantine, source_rows, source_billed)
        qc = reconcile(conn, source_rows, source_billed)
        export_for_powerbi(conn)

    if (qc.status == "FAIL").any():
        log.error("Pipeline finished with QC failures - do not publish the dashboard")
        return 1
    log.info("Pipeline finished OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
