/**
 * Deterministic synthetic demonstration data. Every number on the page that
 * looks like a business figure comes from here. None of it is customer data,
 * and none of it is produced by live inference.
 */

// ---------------------------------------------------------------------------
// Hero walkthrough: "Why did our operating expenses increase last quarter?"
// Amounts in USD thousands. Q2 2026 vs Q3 2026.
// ---------------------------------------------------------------------------

export const opexQuestion = 'Why did our operating expenses increase last quarter?';

export const opexDatasets = [
  { name: 'gl_transactions', source: 'finance.duckdb', rows: '1.2M rows', why: 'General ledger, accrual basis', used: true },
  { name: 'cost_centers', source: 'finance.duckdb', rows: '184 rows', why: 'Maps cost centers to categories', used: true },
  { name: 'vendor_master', source: 'procurement.parquet', rows: '2.9k rows', why: 'Vendor start dates and terms', used: true },
  { name: 'headcount_monthly', source: 'hr_extract.csv', rows: '1.1k rows', why: 'Checked for personnel drivers', used: false },
] as const;

export const opexSql = `SELECT c.category,
       SUM(t.amount) FILTER (WHERE t.fiscal_qtr = '2026-Q2') AS q2,
       SUM(t.amount) FILTER (WHERE t.fiscal_qtr = '2026-Q3') AS q3,
       q3 - q2                                             AS variance
FROM gl_transactions t
JOIN cost_centers c USING (cost_center_id)
WHERE t.account_type = 'OPEX'
  AND t.gl_account <> '6990'   -- exclude one-time restructuring
GROUP BY ALL
ORDER BY variance DESC;`;

export const opexRows = [
  { category: 'Cloud infrastructure', q2: 1840, q3: 2460 },
  { category: 'Contract services', q2: 1210, q3: 1620 },
  { category: 'Marketing programs', q2: 1380, q3: 1660 },
  { category: 'Personnel', q2: 6920, q3: 6980 },
  { category: 'Travel & events', q2: 410, q3: 470 },
  { category: 'Facilities & other', q2: 640, q3: 490 },
] as const;

export const opexTotals = opexRows.reduce(
  (acc, r) => ({ q2: acc.q2 + r.q2, q3: acc.q3 + r.q3 }),
  { q2: 0, q3: 0 },
);

export const opexDrivers = [
  {
    label: 'Cloud infrastructure',
    delta: 620,
    note: '71% from one warehouse account after a usage-tier change in August.',
  },
  {
    label: 'Contract services',
    delta: 410,
    note: 'Two implementation vendors first invoiced in July, committed through Q4.',
  },
  {
    label: 'Marketing programs',
    delta: 280,
    note: 'Event spend moved from Q4 into September. Timing, not run-rate.',
  },
  {
    label: 'Facilities & other',
    delta: -150,
    note: 'Lease consolidation partly offsets the increase.',
  },
] as const;

export const opexFindings = [
  'Operating expenses rose $1.28M (+10.3%) quarter over quarter, to $13.68M.',
  'Cloud infrastructure and contract services explain 80% of the increase.',
  'About $280K is timing. The remaining increase is likely to recur in Q4.',
] as const;

export const opexEvidence = [
  { ref: 'Q1', label: 'opex_by_category.sql', detail: '48,212 ledger rows · 2026-Q2 to 2026-Q3' },
  { ref: 'Q2', label: 'warehouse_usage_by_account.sql', detail: 'Usage tier change on 2026-08-04' },
  { ref: 'D1', label: 'vendor_master', detail: '2 vendors with first invoice in 2026-07' },
  { ref: 'A1', label: 'Assumption', detail: 'USD, accrual basis, GL 6990 restructuring excluded' },
] as const;

export const opexSteps = [
  { id: 'discover', label: 'Discover data', short: 'Discover' },
  { id: 'query', label: 'Run DuckDB query', short: 'Query' },
  { id: 'variance', label: 'Variance analysis', short: 'Variance' },
  { id: 'drivers', label: 'Identify drivers', short: 'Drivers' },
  { id: 'findings', label: 'Chart & findings', short: 'Findings' },
  { id: 'evidence', label: 'Evidence', short: 'Evidence' },
] as const;

// ---------------------------------------------------------------------------
// Use case 1: Financial Intelligence
// ---------------------------------------------------------------------------

export const marginTrend = {
  months: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep'],
  values: [18.4, 18.1, 17.9, 17.6, 17.2, 16.5, 15.9, 15.4, 14.8],
};

export const marginBridge = [
  { label: 'Product mix', pts: -1.5 },
  { label: 'Freight & logistics', pts: -1.2 },
  { label: 'Promotional discounting', pts: -0.9 },
  { label: 'Other', pts: -0.3 },
  { label: 'Price realisation', pts: 0.3 },
] as const;

// ---------------------------------------------------------------------------
// Use case 2: Operational Intelligence (procure-to-pay cycle, median days)
// ---------------------------------------------------------------------------

export const processStages = [
  { stage: 'Requisition', actual: 1.2, target: 1.0, flag: false },
  { stage: 'Approval', actual: 4.6, target: 2.0, flag: true },
  { stage: 'PO issued', actual: 0.8, target: 1.0, flag: false },
  { stage: 'Goods receipt', actual: 3.4, target: 3.0, flag: false },
  { stage: 'Invoice match', actual: 6.2, target: 2.0, flag: true },
  { stage: 'Payment', actual: 2.1, target: 2.0, flag: false },
] as const;

export const invoiceMatchWeekly = [2.1, 2.3, 2.0, 2.4, 2.2, 2.5, 2.3, 2.6, 4.9, 6.8, 7.4, 6.9, 6.2];

// ---------------------------------------------------------------------------
// Use case 3: Analytical Engineering (weekly performance dashboard)
// ---------------------------------------------------------------------------

export const weeklyKpis = [
  { label: 'Revenue', value: '$4.82M', delta: '+3.1%', good: true },
  { label: 'Orders', value: '18.4k', delta: '+1.2%', good: true },
  { label: 'Avg. order value', value: '$262', delta: '+1.9%', good: true },
  { label: 'Return rate', value: '4.1%', delta: '+0.3 pt', good: false },
] as const;

export const weeklyRevenue = [4.21, 4.35, 4.3, 4.44, 4.39, 4.52, 4.48, 4.61, 4.57, 4.68, 4.67, 4.82];

export const weeklySql = `CREATE OR REPLACE TABLE weekly_performance AS
SELECT date_trunc('week', order_ts) AS week,
       SUM(net_amount)              AS revenue,
       COUNT(DISTINCT order_id)     AS orders,
       revenue / orders             AS aov,
       AVG(is_returned::INT)        AS return_rate
FROM orders
GROUP BY ALL;`;

export const weeklyPython = `checks = lyst.checks(weekly_performance)
checks.not_null("week", "revenue")
checks.reconciles("revenue", to="finance.gl_revenue", tolerance=0.005)
checks.run()          # 6 checks passed`;
