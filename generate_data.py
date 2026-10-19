"""Generate a synthetic healthcare billing dataset (NO real patient data).

Creates data/raw/billing_claims.csv and data/raw/dim_payer.csv.
A few deliberately dirty rows (duplicates, missing payer, bad amounts) are
injected so the validation/QC step has something real to catch.
"""
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 42
N_CLAIMS = 20_000
START, END = pd.Timestamp("2024-01-01"), pd.Timestamp("2025-12-31")  # END = data as-of date

rng = np.random.default_rng(SEED)
out = Path(__file__).parent / "data" / "raw"
out.mkdir(parents=True, exist_ok=True)

# service line -> (median billed amount, volume weight)
SERVICE_LINES = {
    "Primary Care": (250, 0.25),
    "Emergency": (1800, 0.15),
    "Radiology": (700, 0.15),
    "Laboratory": (180, 0.15),
    "Cardiology": (2400, 0.10),
    "Orthopedics": (3200, 0.10),
    "Oncology": (4500, 0.10),
}
# payer -> (payer_type, base denial probability, average payment lag in days)
PAYERS = {
    "Medicare": ("Government", 0.08, 30),
    "Medicaid": ("Government", 0.14, 45),
    "BlueShield": ("Commercial", 0.10, 28),
    "Aetna": ("Commercial", 0.12, 35),
    "UnitedCare": ("Commercial", 0.13, 38),
    "Self-Pay": ("Self-Pay", 0.05, 60),
}
DENIAL_REASONS = {
    "Missing/invalid information": 0.24,
    "Prior authorization missing": 0.20,
    "Not medically necessary": 0.15,
    "Duplicate claim": 0.10,
    "Coding error": 0.13,
    "Eligibility/coverage issue": 0.12,
    "Timely filing exceeded": 0.06,
}

sl_names = list(SERVICE_LINES)
sl_w = np.array([SERVICE_LINES[s][1] for s in sl_names])
payer_names = list(PAYERS)
payer_w = np.array([0.28, 0.17, 0.20, 0.13, 0.15, 0.07])

service_line = rng.choice(sl_names, N_CLAIMS, p=sl_w / sl_w.sum())
payer = rng.choice(payer_names, N_CLAIMS, p=payer_w)

days_span = (END - START).days
service_date = START + pd.to_timedelta(rng.integers(0, days_span + 1, N_CLAIMS), unit="D")
submit_date = service_date + pd.to_timedelta(rng.integers(1, 8, N_CLAIMS), unit="D")

median_billed = np.array([SERVICE_LINES[s][0] for s in service_line])
billed = np.round(median_billed * rng.lognormal(0, 0.35, N_CLAIMS), 2)
allowed = np.round(billed * rng.uniform(0.45, 0.80, N_CLAIMS), 2)

deny_p = np.array([PAYERS[p][1] for p in payer])
lag_mean = np.array([PAYERS[p][2] for p in payer])
lag = np.clip(rng.normal(lag_mean, 10), 7, 120).astype(int)
payment_date = submit_date + pd.to_timedelta(lag, unit="D")

u = rng.random(N_CLAIMS)
status = np.where(u < deny_p, "Denied",
         np.where(u < deny_p + 0.12, "Partially Paid", "Paid"))
# anything whose payment would land after the as-of date is still pending
status = np.where(payment_date > END, "Pending", status)

paid = np.zeros(N_CLAIMS)
paid = np.where(status == "Paid", allowed * rng.uniform(0.97, 1.0, N_CLAIMS), paid)
paid = np.where(status == "Partially Paid", allowed * rng.uniform(0.50, 0.90, N_CLAIMS), paid)
paid = np.round(paid, 2)

reason = np.where(
    status == "Denied",
    rng.choice(list(DENIAL_REASONS), N_CLAIMS, p=list(DENIAL_REASONS.values())),
    "",
)
payment_out = pd.Series(payment_date).where(np.isin(status, ["Paid", "Partially Paid"]))

df = pd.DataFrame({
    "claim_id": [f"CLM{i:07d}" for i in range(1, N_CLAIMS + 1)],
    "patient_id": [f"P{n:05d}" for n in rng.integers(1, 8000, N_CLAIMS)],  # synthetic IDs
    "service_line": service_line,
    "payer": payer,
    "service_date": service_date.date if hasattr(service_date, "date") else service_date,
    "submit_date": submit_date.date if hasattr(submit_date, "date") else submit_date,
    "payment_date": payment_out.dt.date,
    "billed_amount": billed,
    "allowed_amount": allowed,
    "paid_amount": paid,
    "status": status,
    "denial_reason": reason,
})

# ---- inject dirty rows so QC has something to find -------------------------
dupes = df.sample(40, random_state=1)
missing_payer = df.sample(25, random_state=2).copy()
missing_payer["claim_id"] = [f"CLM9{i:06d}" for i in range(len(missing_payer))]
missing_payer["payer"] = np.nan
bad_amount = df.sample(15, random_state=3).copy()
bad_amount["claim_id"] = [f"CLM8{i:06d}" for i in range(len(bad_amount))]
bad_amount["billed_amount"] = -bad_amount["billed_amount"]
overpaid = df.sample(10, random_state=4).copy()
overpaid["claim_id"] = [f"CLM7{i:06d}" for i in range(len(overpaid))]
overpaid["paid_amount"] = overpaid["allowed_amount"] * 1.5

dirty = pd.concat([df, dupes, missing_payer, bad_amount, overpaid], ignore_index=True)
dirty = dirty.sample(frac=1, random_state=7).reset_index(drop=True)
dirty.to_csv(out / "billing_claims.csv", index=False)

pd.DataFrame(
    [(p, v[0]) for p, v in PAYERS.items()], columns=["payer", "payer_type"]
).to_csv(out / "dim_payer.csv", index=False)

print(f"Wrote {len(dirty):,} rows ({len(dirty) - len(df)} injected dirty rows) to {out}")
