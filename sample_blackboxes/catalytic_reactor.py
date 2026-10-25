"""Sample Black Box: Catalytic Chemical Reactor

Simulates the product yield (%) of an exothermic chemical reaction
as a function of reactor core temperature (Celsius).
Peak optimal conversion occurs around 25.0 C with steep drop-offs
at extremes due to catalyst deactivation.
"""

import math


def simulate(temperature: float) -> float:
    """Predicts reaction yield (%) from reactor temperature (°C).

    Recommended Search Range: 0.0 to 50.0
    Entrypoint: simulate
    """
    t = float(temperature)
    # Bell-shaped thermal activation curve with secondary resonant flutter
    peak = 88.5 * math.exp(-0.5 * ((t - 25.0) / 7.5) ** 2)
    secondary_kinetics = 2.4 * math.sin(0.4 * t)
    baseline = 6.2
    
    yield_pct = peak + secondary_kinetics + baseline
    return round(float(yield_pct), 4)


def predict_yield(temperature: float) -> float:
    """Alternative named entrypoint for the same simulation."""
    return simulate(temperature)
