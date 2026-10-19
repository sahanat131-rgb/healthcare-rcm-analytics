-- Reporting layer for the RCM dashboard (SQLite dialect; T-SQL notes in README).
-- Definitions
--   Net revenue      = cash collected (paid_amount)
--   Denial rate      = denied claims / adjudicated claims (status <> 'Pending')
--   Collection rate  = paid_amount / allowed_amount on adjudicated, non-denied claims
--   Outstanding AR   = allowed_amount of Pending (not yet adjudicated) claims
--   Underpaid amount = allowed - paid on Partially Paid claims (tracked separately, not AR)
--   Days in AR       = Outstanding AR / (allowed charges of last 90 days / 90)

DROP VIEW IF EXISTS vw_claims_enriched;
CREATE VIEW vw_claims_enriched AS
SELECT
    c.claim_id,
    c.patient_key,
    c.service_line,
    c.payer,
    p.payer_type,
    c.service_date,
    c.submit_date,
    c.payment_date,
    strftime('%Y-%m', c.submit_date)                        AS submit_month,
    c.billed_amount,
    c.allowed_amount,
    c.paid_amount,
    c.status,
    c.denial_reason,
    CASE WHEN c.status = 'Denied'  THEN 1 ELSE 0 END        AS is_denied,
    CASE WHEN c.status <> 'Pending' THEN 1 ELSE 0 END       AS is_adjudicated,
    CASE WHEN c.status = 'Pending' THEN c.allowed_amount ELSE 0 END  AS outstanding_ar,
    CASE WHEN c.status = 'Partially Paid'
         THEN c.allowed_amount - c.paid_amount ELSE 0 END               AS underpaid_amount,
    CASE WHEN c.payment_date IS NOT NULL
         THEN CAST(julianday(c.payment_date) - julianday(c.submit_date) AS INTEGER) END AS days_to_pay
FROM fact_claims c
LEFT JOIN dim_payer p ON p.payer = c.payer;

DROP VIEW IF EXISTS vw_monthly_kpis;
CREATE VIEW vw_monthly_kpis AS
SELECT
    submit_month,
    COUNT(*)                                                         AS total_claims,
    SUM(is_denied)                                                   AS denied_claims,
    ROUND(1.0 * SUM(is_denied) / NULLIF(SUM(is_adjudicated), 0), 4)  AS denial_rate,
    ROUND(SUM(billed_amount), 2)                                     AS billed_amount,
    ROUND(SUM(allowed_amount), 2)                                    AS allowed_amount,
    ROUND(SUM(paid_amount), 2)                                       AS net_revenue,
    ROUND(SUM(CASE WHEN status IN ('Paid','Partially Paid') THEN paid_amount END)
        / NULLIF(SUM(CASE WHEN status IN ('Paid','Partially Paid') THEN allowed_amount END), 0), 4)
                                                                     AS collection_rate,
    ROUND(AVG(days_to_pay), 1)                                       AS avg_days_to_pay,
    ROUND(SUM(outstanding_ar), 2)                                    AS outstanding_ar,
    ROUND(SUM(underpaid_amount), 2)                                  AS underpaid_amount
FROM vw_claims_enriched
GROUP BY submit_month;

-- Month-over-month change with a window function
DROP VIEW IF EXISTS vw_monthly_trend;
CREATE VIEW vw_monthly_trend AS
SELECT
    submit_month,
    net_revenue,
    LAG(net_revenue) OVER (ORDER BY submit_month)                    AS prev_month_revenue,
    ROUND(100.0 * (net_revenue - LAG(net_revenue) OVER (ORDER BY submit_month))
          / NULLIF(LAG(net_revenue) OVER (ORDER BY submit_month), 0), 2) AS revenue_mom_pct,
    denial_rate,
    ROUND(AVG(denial_rate) OVER (ORDER BY submit_month
          ROWS BETWEEN 2 PRECEDING AND CURRENT ROW), 4)              AS denial_rate_3m_avg
FROM vw_monthly_kpis;

DROP VIEW IF EXISTS vw_service_line_kpis;
CREATE VIEW vw_service_line_kpis AS
SELECT
    service_line,
    COUNT(*)                                                         AS total_claims,
    ROUND(SUM(billed_amount), 2)                                     AS billed_amount,
    ROUND(SUM(paid_amount), 2)                                       AS net_revenue,
    ROUND(1.0 * SUM(is_denied) / NULLIF(SUM(is_adjudicated), 0), 4)  AS denial_rate,
    ROUND(SUM(outstanding_ar), 2)                                    AS outstanding_ar,
    ROUND(AVG(days_to_pay), 1)                                       AS avg_days_to_pay,
    RANK() OVER (ORDER BY SUM(paid_amount) DESC)                     AS revenue_rank
FROM vw_claims_enriched
GROUP BY service_line;

DROP VIEW IF EXISTS vw_payer_kpis;
CREATE VIEW vw_payer_kpis AS
SELECT
    payer,
    payer_type,
    COUNT(*)                                                         AS total_claims,
    ROUND(SUM(paid_amount), 2)                                       AS net_revenue,
    ROUND(1.0 * SUM(is_denied) / NULLIF(SUM(is_adjudicated), 0), 4)  AS denial_rate,
    ROUND(AVG(days_to_pay), 1)                                       AS avg_days_to_pay,
    RANK() OVER (ORDER BY 1.0 * SUM(is_denied) / NULLIF(SUM(is_adjudicated), 0) DESC) AS denial_rank
FROM vw_claims_enriched
GROUP BY payer, payer_type;

DROP VIEW IF EXISTS vw_denial_reasons;
CREATE VIEW vw_denial_reasons AS
SELECT
    denial_reason,
    COUNT(*)                                                         AS denied_claims,
    ROUND(SUM(allowed_amount), 2)                                    AS denied_allowed_amount,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2)               AS pct_of_denials
FROM vw_claims_enriched
WHERE is_denied = 1
GROUP BY denial_reason;

DROP VIEW IF EXISTS vw_ar_aging;
CREATE VIEW vw_ar_aging AS
WITH asof AS (SELECT MAX(submit_date) AS d FROM fact_claims),
aged AS (
    SELECT c.*, CAST(julianday((SELECT d FROM asof)) - julianday(c.submit_date) AS INTEGER) AS age_days
    FROM vw_claims_enriched c
    WHERE c.outstanding_ar > 0
)
SELECT
    CASE WHEN age_days <= 30 THEN '0-30'
         WHEN age_days <= 60 THEN '31-60'
         WHEN age_days <= 90 THEN '61-90'
         ELSE '90+' END                                              AS aging_bucket,
    COUNT(*)                                                         AS claims,
    ROUND(SUM(outstanding_ar), 2)                                    AS outstanding_ar
FROM aged
GROUP BY aging_bucket;

DROP VIEW IF EXISTS vw_days_in_ar;
CREATE VIEW vw_days_in_ar AS
WITH asof AS (SELECT MAX(submit_date) AS d FROM fact_claims)
SELECT
    (SELECT d FROM asof)                                             AS as_of_date,
    ROUND(SUM(outstanding_ar), 2)                                    AS total_outstanding_ar,
    ROUND(SUM(CASE WHEN submit_date > date((SELECT d FROM asof), '-90 days')
                   THEN allowed_amount END) / 90.0, 2)               AS avg_daily_allowed_90d,
    ROUND(SUM(outstanding_ar) /
          (SUM(CASE WHEN submit_date > date((SELECT d FROM asof), '-90 days')
                    THEN allowed_amount END) / 90.0), 1)             AS days_in_ar
FROM vw_claims_enriched;
