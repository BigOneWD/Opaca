/**
 * Public-safe projection of docs/evidence/wheel-csp-proof-2026-09-03.json.
 *
 * Keep broker identifiers, account fingerprints, and client identifiers out of
 * the static Pages bundle. The immutable risk-capital base is the verified
 * Opaca bootstrap value documented in README.md; the reservation and lifecycle
 * fields come from the sanitized proof artifact above.
 */
export const verifiedProof = {
  underlying: "XLF",
  occSymbol: "XLF260910P00058000",
  strike: "58",
  expiry: "2026-09-10",
  contracts: 1,
  assignmentCapital: "5800.00",
  riskCapitalBase: "99999.94",
  authority: "AUTO",
  state: "SHORT_PUT_OPEN",
  reconciliation: "RECONCILED",
  feed: "INDICATIVE",
  productionGradeMarketData: false,
  checksPassed: 6,
  checksTotal: 6,
} as const;

export const capitalDerived = {
  available: "94199.94",
  usedPercent: "5.8",
} as const;
