"""Joint weekday + linear trend + persistent level-shift contrast.

Unlike extrapolation from a short reference alone, observations on both sides
estimate the common slope. The step coefficient retains slope uncertainty via
the residualized design information. Scores are diagnostics, not p-values.
No scenario labels, IDs, cause assumptions or tuned case thresholds enter here.
"""
from __future__ import annotations

import math
from collections import defaultdict


def fit_shift(y: dict, base_dates: list, post_dates: list, sigma_floor: float):
    days = [d for d in base_dates + post_dates if d in y]
    if not days or not post_dates or (post_dates[0] - base_dates[-1]).days != 1:
        return None
    # A missing transition is not evidence of a continuous shift.
    if len(days) != len(base_dates) + len(post_dates):
        return None
    groups = defaultdict(list)
    t0 = base_dates[0]
    post = set(post_dates)
    for d in days:
        groups[d.weekday()].append(((d - t0).days, float(d in post), y[d]))
    means = {w: tuple(sum(r[j] for r in rows) / len(rows) for j in range(3)) for w, rows in groups.items()}
    centered = [(t - means[w][0], z - means[w][1], v - means[w][2]) for w, rows in groups.items() for t, z, v in rows]
    tt = sum(t*t for t, z, v in centered)
    tz = sum(t*z for t, z, v in centered)
    zz = sum(z*z for t, z, v in centered)
    ty = sum(t*v for t, z, v in centered)
    zy = sum(z*v for t, z, v in centered)
    information = zz - tz*tz/tt if tt else 0
    df = len(days) - len(groups) - 2
    if information <= 1e-9 or df <= 0:
        return None
    shift = (zy - tz*ty/tt) / information
    slope = (ty - tz*shift) / tt
    intercepts = {w: v - slope*t - shift*z for w, (t, z, v) in means.items()}
    expected_log = {d: intercepts[d.weekday()] + slope*(d-t0).days for d in days}
    residuals = [y[d] - expected_log[d] - shift*(d in post) for d in days]
    rms = math.sqrt(sum(r*r for r in residuals)/df)
    # Positive serial dependence reduces effective evidence. Never shrink the
    # pre-reference robust/sampling noise floor by fitting the post observations.
    energy = sum(r*r for r in residuals)
    rho = max(0.0, min(.9, sum(a*b for a,b in zip(residuals, residuals[1:]))/energy)) if energy else 0.0
    sigma = max(sigma_floor, rms) * math.sqrt((1+rho)/(1-rho))
    score = abs(shift)*math.sqrt(information)/sigma
    return dict(shift=shift, slope=slope, intercepts=intercepts, sigma=sigma,
                score=score, expected_log=expected_log)
