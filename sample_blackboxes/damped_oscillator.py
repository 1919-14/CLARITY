"""Sample Black Box: Driven Damped Harmonic Resonator

Simulates the steady-state mechanical oscillation amplitude (mm)
as a function of external driving frequency omega (rad/s).
Resonant peak occurs sharply around omega = 5.0 rad/s.
"""

import math


def simulate(frequency: float) -> float:
    """Predicts oscillation amplitude (mm) from driving frequency (rad/s).

    Recommended Search Range: 0.0 to 10.0
    Entrypoint: simulate
    """
    omega = float(frequency)
    omega_0 = 5.0      # Natural resonance frequency
    gamma = 0.65       # Damping factor
    f_0 = 42.0         # Driving force magnitude

    denominator = math.sqrt((omega_0**2 - omega**2)**2 + (gamma * omega)**2)
    amplitude = (f_0 / max(denominator, 0.001)) + 1.5
    
    return round(float(amplitude), 4)


def evaluate(frequency: float) -> float:
    """Alternative named entrypoint for the same simulation."""
    return simulate(frequency)
