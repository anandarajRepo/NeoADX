"""Vectorised ADX / DI+  / DI- calculation using pandas / numpy."""

import numpy as np
import pandas as pd


def calculate_adx(df: pd.DataFrame, period: int = 16) -> pd.DataFrame:
    """
    Append ADX, DI_plus, DI_minus columns to *df* (in-place) and return it.

    Expects columns: high, low, close.
    """
    high = df["high"]
    low  = df["low"]
    close = df["close"]

    # True Range
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Directional movement
    up_move   = high - high.shift(1)
    down_move = low.shift(1) - low

    plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move,  0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    # Wilder smoothing
    def _wilder_smooth(series: pd.Series, n: int) -> pd.Series:
        result = series.copy().astype(float)
        result.iloc[:n] = np.nan
        result.iloc[n] = series.iloc[1 : n + 1].sum()
        for i in range(n + 1, len(series)):
            result.iloc[i] = result.iloc[i - 1] - result.iloc[i - 1] / n + series.iloc[i]
        return result

    tr_smooth       = _wilder_smooth(tr, period)
    plus_dm_smooth  = _wilder_smooth(pd.Series(plus_dm,  index=df.index), period)
    minus_dm_smooth = _wilder_smooth(pd.Series(minus_dm, index=df.index), period)

    di_plus  = 100 * plus_dm_smooth  / tr_smooth
    di_minus = 100 * minus_dm_smooth / tr_smooth

    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus)
    adx = _wilder_smooth(dx.fillna(0), period)

    df["DI_plus"]  = di_plus.round(4)
    df["DI_minus"] = di_minus.round(4)
    df["ADX"]      = adx.round(4)
    return df


def detect_crossover(di_plus: pd.Series, di_minus: pd.Series) -> pd.Series:
    """
    Return a Series of crossover signals on the latest bar:
      +1  → DI+ crossed above DI-  (bullish)
      -1  → DI- crossed above DI+  (bearish)
       0  → no crossover
    """
    prev_bull = di_plus.shift(1) <= di_minus.shift(1)
    curr_bull = di_plus > di_minus
    prev_bear = di_minus.shift(1) <= di_plus.shift(1)
    curr_bear = di_minus > di_plus

    signal = pd.Series(0, index=di_plus.index)
    signal[prev_bull & curr_bull] =  1
    signal[prev_bear & curr_bear] = -1
    return signal
