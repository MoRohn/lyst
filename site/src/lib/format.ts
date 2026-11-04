/** Formatting helpers for synthetic demo figures (USD thousands in, compact out). */

export function usdK(k: number, digits = 2): string {
  const sign = k < 0 ? '−' : '';
  const v = Math.abs(k);
  if (v >= 1000) return `${sign}$${(v / 1000).toFixed(digits)}M`;
  return `${sign}$${Math.round(v)}K`;
}

export function signedUsdK(k: number, digits = 2): string {
  return (k > 0 ? '+' : '') + usdK(k, digits);
}

export function pct(value: number, digits = 1): string {
  const sign = value > 0 ? '+' : value < 0 ? '−' : '';
  return `${sign}${Math.abs(value).toFixed(digits)}%`;
}

export function thousands(k: number): string {
  return k.toLocaleString('en-US');
}

/** Map a value range onto pixel space. */
export function scale(d0: number, d1: number, r0: number, r1: number) {
  return (v: number) => r0 + ((v - d0) / (d1 - d0)) * (r1 - r0);
}
