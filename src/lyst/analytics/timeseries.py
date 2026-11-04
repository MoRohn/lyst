# Vendored from ai-analytics stkit (analytics/timeseries.py) at commit d86ee66, same author (Rohn Springfield).
# Changes for Lyst are marked 'lyst:'. Keep in sync deliberately, not automatically.
"""Time series: periods and comparisons, resampling, forecasts with honest error bars, and
anomaly detection.

    s = series(orders, "Order Date", "Sales", grain="month")          # regular, gap-filled
    fc = forecast(s, horizon=6)                                        # ETS + 80% interval + backtest
    a = anomalies(daily_signups, period=7)                             # robust STL residual z-scores
    cur, prev = year_bounds(2026), year_bounds(2025)
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = ["FREQ", "series", "period_key", "grain_of", "year_bounds", "month_bounds", "shift_year", "previous_period", "between",
           "Forecast", "forecast", "anomalies", "seasonal_period"]

FREQ = {"day": "D", "week": "W-MON", "month": "MS", "quarter": "QS", "year": "YS"}
_SEASON = {"day": 7, "week": 52, "month": 12, "quarter": 4, "year": 1}


def _freq(index: pd.DatetimeIndex) -> str | None:
    """The index's frequency as a string ("MS", "D"...): its own if set, else inferred.
    pandas 3 warns on every infer_freq call that pandas 4 returns an offset; this works on both."""
    if getattr(index, "freq", None) is not None:
        return index.freqstr
    if len(index) < 3:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        f = pd.infer_freq(index)
    return getattr(f, "freqstr", f)


def grain_of(index: pd.DatetimeIndex) -> str:
    """The grain of a regular index, from its frequency family: W-* week, M* month,
    Q* quarter, Y*/A* year, D/B day. Raises for frequencies the forecasts don't handle."""
    f = (_freq(index) or "").upper()
    for prefix, grain in (("W", "week"), ("Q", "quarter"), ("BQ", "quarter"), ("Y", "year"), ("A", "year"),
                          ("BY", "year"), ("BA", "year"), ("M", "month"), ("BM", "month"), ("SM", "month"),
                          ("D", "day"), ("B", "day"), ("C", "day")):
        if f.startswith(prefix):
            return grain
    if not f:
        raise ValueError("can't infer the series' frequency; pass grain= or use series()")
    raise ValueError(f"frequency {f!r} isn't supported (day, week, month, quarter or year)")


def seasonal_period(grain: str) -> int:
    return _SEASON[grain]


def series(df: pd.DataFrame, date: str, value: str | None = None, *, grain: str = "month", agg: str = "sum",
           start=None, end=None, fill: float | None = 0.0) -> pd.Series:
    """One regular series: `agg` of `value` per period (count of rows when value is None),
    with empty periods filled (fill=None keeps NaN). The index is the period start."""
    key = period_key(df[date], grain)
    g = (df.assign(_k=key).groupby("_k")[value].agg(agg) if value else df.assign(_k=key).groupby("_k").size())
    lo = pd.Timestamp(start) if start is not None else g.index.min()
    hi = pd.Timestamp(end) if end is not None else g.index.max()
    idx = pd.date_range(_floor(lo, grain), _floor(hi, grain), freq=FREQ[grain])
    out = g.reindex(idx)
    if fill is not None:
        out = out.fillna(fill)
    out.index.name = "period"
    return out.astype(float).rename(value or "count")


def period_key(dates: pd.Series, grain: str) -> pd.Series:
    """The start of each date's period (weeks start on Monday)."""
    d = pd.to_datetime(dates).dt.normalize()
    if grain == "day":
        return d
    if grain == "week":
        return d - pd.to_timedelta(d.dt.day_of_week, unit="D")
    return d.dt.to_period({"month": "M", "quarter": "Q", "year": "Y"}[grain]).dt.start_time


def _floor(ts: pd.Timestamp, grain: str) -> pd.Timestamp:
    ts = pd.Timestamp(ts).normalize()
    if grain == "day":
        return ts
    if grain == "week":
        return ts - pd.Timedelta(days=ts.day_of_week)
    if grain == "month":
        return ts.replace(day=1)
    if grain == "quarter":
        return ts.replace(day=1, month=3 * ((ts.month - 1) // 3) + 1)
    return ts.replace(day=1, month=1)


_EOD = pd.Timedelta(days=1) - pd.Timedelta(1, "ns")   # a bound's end covers its whole last day


def year_bounds(year: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    return pd.Timestamp(year, 1, 1), pd.Timestamp(year, 12, 31) + _EOD


def month_bounds(ts) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(ts).normalize().replace(day=1)
    return start, start + pd.offsets.MonthEnd(0) + _EOD


def _month_aligned(bounds: tuple) -> int | None:
    """Whole months covered when bounds run from a month's first day to a month's last."""
    start, end = (pd.Timestamp(b) for b in bounds)
    if start != start.normalize().replace(day=1) or end.normalize() != (end + pd.offsets.MonthEnd(0)).normalize():
        return None
    return (end.year - start.year) * 12 + end.month - start.month + 1


def shift_year(bounds: tuple, years: int = -1) -> tuple[pd.Timestamp, pd.Timestamp]:
    """The same window `years` earlier. Calendar months stay whole (Feb 2025 -> Feb 2024
    keeps the 29th); other windows shift by date, Feb 29 mapping to Feb 28."""
    start, end = (pd.Timestamp(b) for b in bounds)
    if _month_aligned(bounds):
        s = start + pd.DateOffset(years=years)
        return s, (end.normalize() + pd.DateOffset(years=years) + pd.offsets.MonthEnd(0)) + _EOD
    return start + pd.DateOffset(years=years), end + pd.DateOffset(years=years)


def previous_period(bounds: tuple) -> tuple[pd.Timestamp, pd.Timestamp]:
    """The window just before `bounds`: the previous N calendar months when the bounds are
    whole months (a year before a year, a quarter before a quarter), else the window of
    equal length ending just before `bounds` starts."""
    start, end = (pd.Timestamp(b) for b in bounds)
    months = _month_aligned(bounds)
    if months:
        s = start - pd.DateOffset(months=months)
        return s, start - pd.Timedelta(1, "ns")
    span = end - start
    return start - span - pd.Timedelta(1, "ns"), start - pd.Timedelta(1, "ns")


def between(df: pd.DataFrame, date: str, bounds: tuple) -> pd.DataFrame:
    d = pd.to_datetime(df[date])
    return df[(d >= bounds[0]) & (d <= bounds[1])]


# ------------------------------------------------------------------------------ forecast

@dataclass
class Forecast:
    history: pd.Series
    table: pd.DataFrame          # period, yhat, lo, hi
    method: str
    level: float                 # interval coverage, e.g. 0.8
    backtest_mape: float | None  # mean absolute % error on the held-out tail
    backtest_points: int
    season: int = 1              # periods per seasonal cycle (12 for months)

    def frame(self) -> pd.DataFrame:
        """History and forecast stacked for charting: period, value, kind, yhat, lo, hi."""
        h = self.history.rename("value").reset_index()
        h["kind"] = "actual"
        f = self.table.copy()
        f["kind"] = "forecast"
        return pd.concat([h, f], ignore_index=True)

    @property
    def total(self) -> float:
        return float(self.table["yhat"].sum())


def _ets(y: pd.Series, horizon: int, sp: int, alpha: float):
    from statsmodels.tsa.exponential_smoothing.ets import ETSModel
    seasonal = "add" if sp > 1 and len(y) >= 2 * sp + 2 else None
    trend = "add" if len(y) >= 6 else None
    model = ETSModel(y, error="add", trend=trend, damped_trend=trend is not None, seasonal=seasonal,
                     seasonal_periods=sp if seasonal else None, initialization_method="estimated")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = model.fit(disp=False, maxiter=200)
        pred = fit.get_prediction(start=len(y), end=len(y) + horizon - 1)
        sf = pred.summary_frame(alpha=alpha)
    name = "ETS(A," + ("Ad" if trend else "N") + "," + ("A" if seasonal else "N") + ")"
    return sf["mean"].to_numpy(), sf["pi_lower"].to_numpy(), sf["pi_upper"].to_numpy(), name


def _snaive(y: pd.Series, horizon: int, sp: int, alpha: float):
    vals = y.to_numpy(dtype=float)
    k = sp if sp > 1 and len(vals) >= sp else 1
    base = np.array([vals[-k + (i % k)] for i in range(horizon)])
    resid = vals[k:] - vals[:-k] if len(vals) > k else np.array([0.0])
    from scipy.stats import norm
    z = norm.ppf(1 - alpha / 2)
    sd = np.std(resid) * np.sqrt(np.arange(1, horizon + 1) / k + 1)
    return base, base - z * sd, base + z * sd, "seasonal naive" if k > 1 else "naive"


def forecast(y: pd.Series, horizon: int = 6, *, grain: str | None = None, level: float = 0.8,
             nonnegative: bool | None = None, backtest: int | None = None) -> Forecast:
    """Forecast a regular series `horizon` periods ahead with an ETS model (damped additive
    trend, additive seasonality when there are two full cycles), falling back to a seasonal
    naive forecast for short series. The `level` interval comes from the model.

    The backtest refits on all but the last `backtest` points (default: min(horizon,
    len/4)) and reports MAPE on them, so the UI can say how far to trust the line."""
    y = y.astype(float)
    if grain is None:
        grain = grain_of(y.index)
    sp = _SEASON[grain]
    # Re-key to period starts (end-anchored "ME"/"W-SUN"/"QE" indexes would otherwise be
    # emptied by asfreq: every value silently NaN, review 2026-10-23), then tag the frequency.
    keyed = y.groupby(period_key(pd.Series(y.index, index=y.index), grain).to_numpy()).sum(min_count=1)
    y = keyed.asfreq(FREQ[grain])
    if y.isna().any():
        raise ValueError(f"the series has {int(y.isna().sum())} missing {grain}s; fill them first "
                         "(stkit.analytics.timeseries.series() returns a gap-filled series)")
    alpha = 1 - level
    nonneg = (y >= 0).all() if nonnegative is None else nonnegative
    fit = _ets if len(y) >= 8 else _snaive
    try:
        yhat, lo, hi, method = fit(y, horizon, sp, alpha)
    except Exception:          # numerical failure in the optimiser: fall back
        yhat, lo, hi, method = _snaive(y, horizon, sp, alpha)
    if nonneg:
        yhat, lo, hi = np.maximum(yhat, 0), np.maximum(lo, 0), np.maximum(hi, 0)
    idx = pd.date_range(y.index[-1], periods=horizon + 1, freq=FREQ[grain])[1:]
    table = pd.DataFrame({"period": idx, "yhat": yhat, "lo": lo, "hi": hi})

    k = backtest if backtest is not None else max(0, min(horizon, len(y) // 4))
    mape = None
    if k and len(y) - k >= 8:
        train, test = y.iloc[:-k], y.iloc[-k:]
        try:
            p, *_ = (_ets if len(train) >= 8 else _snaive)(train, k, sp, alpha)
        except Exception:
            p, *_ = _snaive(train, k, sp, alpha)
        nz = test.to_numpy() != 0
        if nz.any():
            mape = float(np.mean(np.abs((test.to_numpy()[nz] - p[nz]) / test.to_numpy()[nz])))
    return Forecast(history=y, table=table, method=method, level=level, backtest_mape=mape, backtest_points=k,
                    season=sp)


# ------------------------------------------------------------------------------ anomalies

def _odd(n: int) -> int:
    return n if n % 2 else n + 1


def anomalies(y: pd.Series, *, period: int | None = None, threshold: float = 3.5, log: bool | None = None) -> pd.DataFrame:
    """Flag points whose residual after removing trend and seasonality is extreme.

    Uses a robust STL decomposition when there are at least two seasonal cycles, else a
    centred rolling median. The score is the robust z-score of the residual,
    0.6745 × (r − median) / MAD; |score| ≥ threshold flags the point (3.5 is the
    Iglewicz–Hoaglin cut-off). Returns period, value, expected, score, is_anomaly,
    direction ("spike"/"drop").

    Calibrated on the subscriptions template's daily trials (2026-10-23): STL's default
    smoothers (seasonal 7, trend ~13 days) absorb noise into the fit, shrink the MAD and
    flagged 119 of 1,369 days; a seasonal smoother of ~4 cycles and a trend of ~8 cycles
    on the log scale flag 19, which include all 4 planted anomaly days and 8 holidays.

    log (default: True for a non-negative series) scores log1p(values), which suits
    multiplicative patterns (weekday effects that grow with the level) and skewed money."""
    y = y.astype(float)
    period = period or (7 if _freq(y.index) == "D" else 12)
    log = bool((y >= 0).all()) if log is None else log
    z = np.log1p(y.clip(lower=0)) if log else y
    if len(z) >= 2 * period + 1:
        from statsmodels.tsa.seasonal import STL
        trend = _odd(8 * period + 1)
        res = STL(z, period=period, robust=True, seasonal=_odd(4 * period + 3),
                  trend=trend if len(z) >= 3 * trend else None).fit()
        fitted = res.trend + res.seasonal
    else:
        fitted = z.rolling(max(3, period), center=True, min_periods=1).median()
    r = z - fitted
    expected = np.expm1(fitted) if log else fitted
    med = np.median(r)
    dev = np.abs(r - med)
    eps = 1e-9 * max(1.0, float(np.median(np.abs(z))))
    dev = np.where(dev < eps, 0.0, dev)                 # float noise from the fit is zero
    mad, meanad = float(np.median(dev)), float(np.mean(dev))
    if mad > eps:
        score = 0.6745 * np.sign(r - med) * dev / mad
    elif meanad > 0:                                    # most residuals equal: the mean-AD form (Iglewicz-Hoaglin)
        score = np.sign(r - med) * dev / (1.2533 * meanad)
    else:
        score = np.zeros(len(r))
    out = pd.DataFrame({"period": y.index, "value": y.to_numpy(), "expected": np.asarray(expected),
                        "score": np.asarray(score)})
    out["is_anomaly"] = out["score"].abs() >= threshold
    out["direction"] = np.where(out["score"] >= 0, "spike", "drop")
    return out
