"""Post-hoc calibration of DC output probabilities (ARCHITECTURE.md §6.1 Stage B).

Fit each of {H, D, A} independently with isotonic regression on a held-out time
slice (raw DC prob -> empirical outcome frequency), then renormalise the triple
to sum to 1. Isotonic is non-parametric and monotone, so it fixes systematic
over/under-confidence -- e.g. the model over-stating draws at extreme mismatches
-- without assuming a functional form.

Persistence is plain JSON: we fit with sklearn but store only each class's
isotonic step points (x/y thresholds) and reproduce `transform` with np.interp.
That keeps saved calibrators portable across sklearn versions (the CI install
may differ from the one that fitted them).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from sklearn.isotonic import IsotonicRegression

CLASS_LABELS = ("H", "D", "A")


class IndependentIsotonic:
    def __init__(self) -> None:
        self._x: list[np.ndarray | None] = [None, None, None]
        self._y: list[np.ndarray | None] = [None, None, None]

    def fit(self, probs: np.ndarray, outcomes: np.ndarray) -> "IndependentIsotonic":
        """probs: (n,3) raw DC; outcomes: (n,) in {0,1,2} for H/D/A."""
        for k in range(3):
            iso = IsotonicRegression(out_of_bounds="clip")
            iso.fit(probs[:, k], (outcomes == k).astype(float))
            self._x[k] = np.asarray(iso.X_thresholds_, dtype=float)
            self._y[k] = np.asarray(iso.y_thresholds_, dtype=float)
        return self

    def _map(self, k: int, col: np.ndarray) -> np.ndarray:
        # np.interp clips to the endpoint values outside [x[0], x[-1]] -> matches
        # IsotonicRegression(out_of_bounds="clip").
        return np.interp(col, self._x[k], self._y[k])

    def transform(self, probs: np.ndarray) -> np.ndarray:
        if any(v is None for v in self._x):
            raise RuntimeError("calibrator not fitted/loaded")
        out = np.column_stack([self._map(k, probs[:, k]) for k in range(3)])
        out = np.clip(out, 1e-6, 1.0)
        out /= out.sum(axis=1, keepdims=True)
        return out

    # --- JSON persistence --------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "method": "isotonic",
            "classes": list(CLASS_LABELS),
            "x": [v.tolist() for v in self._x],
            "y": [v.tolist() for v in self._y],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "IndependentIsotonic":
        obj = cls()
        obj._x = [np.asarray(v, dtype=float) for v in d["x"]]
        obj._y = [np.asarray(v, dtype=float) for v in d["y"]]
        return obj

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path) -> "IndependentIsotonic | None":
        p = Path(path)
        if not p.exists():
            return None
        return cls.from_dict(json.loads(p.read_text()))


class TemperatureScaler:
    """Single-parameter temperature calibration (Guo et al. 2017, "On Calibration
    of Modern Neural Networks").

    Rescales the DC probability vector by `softmax(log(p) / T)`. T > 1 SOFTENS
    (pulls toward uniform / less confident), T < 1 SHARPENS, T = 1 is a no-op.

    Why this over per-class isotonic for V1: the raw DC model is already well
    calibrated (low ECE; raw log-loss beats the uniform floor on every held-out
    tournament), so a flexible per-class isotonic map OVERFITS its small holdout
    slice and generalises negatively to a held-out tournament -- and, worse, it
    can drive a class probability toward 0/1, which log-loss punishes savagely
    when an upset lands (this is exactly what broke the WC-2022 backtest). A
    one-degree-of-freedom temperature cannot overfit and cannot collapse a class,
    so at worst (T ~= 1) it leaves a good model untouched, and when there is
    genuine over-confidence it softens it -- which is what helps on upset-heavy
    fields. Fitted by minimising holdout multiclass log-loss over log T.
    """

    def __init__(self, temperature: float = 1.0) -> None:
        self.temperature = float(temperature)

    @staticmethod
    def _apply(probs: np.ndarray, t: float) -> np.ndarray:
        z = np.log(np.clip(probs, 1e-12, 1.0)) / t
        z -= z.max(axis=1, keepdims=True)          # stabilise the exp
        e = np.exp(z)
        return e / e.sum(axis=1, keepdims=True)

    def fit(self, probs: np.ndarray, outcomes: np.ndarray) -> "TemperatureScaler":
        """probs: (n,3) raw DC; outcomes: (n,) in {0,1,2}. Minimise log-loss over T."""
        from scipy.optimize import minimize_scalar

        probs = np.asarray(probs, dtype=float)
        outcomes = np.asarray(outcomes)
        rows = np.arange(len(outcomes))

        def nll(log_t: float) -> float:
            p = self._apply(probs, float(np.exp(log_t)))
            return float(-np.mean(np.log(np.clip(p[rows, outcomes], 1e-12, 1.0))))

        # Bounded 1-D search over log T in [log 0.3, log 10]; T cannot run away.
        res = minimize_scalar(nll, bounds=(float(np.log(0.3)), float(np.log(10.0))),
                              method="bounded")
        self.temperature = float(np.exp(res.x))
        return self

    def transform(self, probs: np.ndarray) -> np.ndarray:
        return self._apply(np.asarray(probs, dtype=float), self.temperature)

    def to_dict(self) -> dict:
        return {"method": "temperature", "temperature": self.temperature}

    @classmethod
    def from_dict(cls, d: dict) -> "TemperatureScaler":
        return cls(float(d["temperature"]))

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path) -> "TemperatureScaler | None":
        p = Path(path)
        if not p.exists():
            return None
        return cls.from_dict(json.loads(p.read_text()))


class TemperatureDrawScaler:
    """Two-parameter calibration: temperature `T` + a draw-class log-bias `b_D`.

    `softmax(log(p)/T + b_D * [0,1,0])`. The temperature does the same global
    sharpen/soften job as TemperatureScaler; the SINGLE extra parameter shifts the
    draw (middle) class up or down BEFORE the softmax, independently of H/A.

    Why it exists: temperature-only with the fitted T~=0.86 (<1) SHARPENS, which
    pushes the draw -- almost never the argmax class -- systematically DOWN, so the
    model's already-modest draw probability gets demoted further and the model
    players essentially never pick a draw (vs a ~25-30% real draw rate). A draw log-
    bias lets H/A stay as sharp as the data wants while restoring the draw class to
    its honest frequency. It is still only ONE degree of freedom beyond temperature,
    so it keeps the anti-overfit property that made us pick temperature over the
    flexible per-class isotonic (which overfit its holdout and broke the WC-2022
    backtest): it cannot collapse a class and at worst (b_D~=0) reduces to plain
    temperature. Both params are fitted jointly by minimising holdout log-loss.
    """

    DRAW = 1  # index of the draw class in (H, D, A)

    def __init__(self, temperature: float = 1.0, draw_bias: float = 0.0) -> None:
        self.temperature = float(temperature)
        self.draw_bias = float(draw_bias)

    @staticmethod
    def _apply(probs: np.ndarray, t: float, b: float) -> np.ndarray:
        z = np.log(np.clip(probs, 1e-12, 1.0)) / t
        z[:, TemperatureDrawScaler.DRAW] += b      # lift/lower the draw logit only
        z -= z.max(axis=1, keepdims=True)          # stabilise the exp
        e = np.exp(z)
        return e / e.sum(axis=1, keepdims=True)

    def fit(self, probs: np.ndarray, outcomes: np.ndarray) -> "TemperatureDrawScaler":
        """probs: (n,3) raw DC; outcomes: (n,) in {0,1,2}. Minimise log-loss over
        (log T, b_D) jointly -- bounded so neither parameter can run away."""
        from scipy.optimize import minimize

        probs = np.asarray(probs, dtype=float)
        outcomes = np.asarray(outcomes)
        rows = np.arange(len(outcomes))

        def nll(theta: np.ndarray) -> float:
            p = self._apply(probs, float(np.exp(theta[0])), float(theta[1]))
            return float(-np.mean(np.log(np.clip(p[rows, outcomes], 1e-12, 1.0))))

        res = minimize(
            nll, x0=np.array([0.0, 0.0]),
            bounds=[(float(np.log(0.3)), float(np.log(10.0))), (-2.0, 2.0)],
            method="L-BFGS-B",
        )
        self.temperature = float(np.exp(res.x[0]))
        self.draw_bias = float(res.x[1])
        return self

    def transform(self, probs: np.ndarray) -> np.ndarray:
        return self._apply(np.asarray(probs, dtype=float),
                           self.temperature, self.draw_bias)

    def to_dict(self) -> dict:
        return {"method": "temperature_draw", "temperature": self.temperature,
                "draw_bias": self.draw_bias}

    @classmethod
    def from_dict(cls, d: dict) -> "TemperatureDrawScaler":
        return cls(float(d["temperature"]), float(d.get("draw_bias", 0.0)))

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path) -> "TemperatureDrawScaler | None":
        p = Path(path)
        if not p.exists():
            return None
        return cls.from_dict(json.loads(p.read_text()))


def make_calibrator(method: str):
    """Construct an unfitted calibrator by name.

    'temperature_draw' (default in production) -- temperature + draw log-bias.
    'temperature'       -- single-parameter temperature (legacy / ablation).
    'isotonic'          -- flexible per-class (kept for the ablation; overfits).
    """
    if method == "temperature_draw":
        return TemperatureDrawScaler()
    if method == "temperature":
        return TemperatureScaler()
    if method == "isotonic":
        return IndependentIsotonic()
    raise ValueError(f"unknown calibration method {method!r} (choose "
                     "'temperature_draw', 'temperature' or 'isotonic')")


def load_calibrator(path):
    """Load whichever calibrator was persisted, dispatching on its 'method' tag.

    Returns None if the file is absent (caller falls back to RAW). Back-compat:
    an isotonic file written before the 'method' tag existed has 'x'/'y' keys and
    no 'method', so it is treated as isotonic.
    """
    p = Path(path)
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    method = d.get("method") or ("isotonic" if "x" in d else None)
    if method == "temperature_draw":
        return TemperatureDrawScaler.from_dict(d)
    if method == "temperature":
        return TemperatureScaler.from_dict(d)
    if method == "isotonic":
        return IndependentIsotonic.from_dict(d)
    raise ValueError(f"unrecognised calibrator JSON at {path}: keys={list(d)}")


def calibrate_hda(
    cal, p_h: float, p_d: float, p_a: float
) -> tuple[float, float, float]:
    """Apply a fitted calibrator to one (H, D, A) triple -> calibrated floats.

    Works with any calibrator exposing `transform` (IndependentIsotonic or
    TemperatureScaler).
    """
    out = cal.transform(np.array([[p_h, p_d, p_a]], dtype=float))[0]
    return float(out[0]), float(out[1]), float(out[2])
