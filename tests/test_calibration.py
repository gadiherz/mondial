"""Calibrator unit tests: temperature scaling + persistence/dispatch.

These are math-primitive unit tests (analogous to test_dixon_coles using toy
arrays) -- they exercise the calibration transforms, not the project model.
"""
import json

import numpy as np

from mondial.model.calibration import (
    IndependentIsotonic,
    TemperatureScaler,
    calibrate_hda,
    load_calibrator,
    make_calibrator,
)


def test_temperature_identity_is_noop():
    p = np.array([[0.6, 0.25, 0.15], [0.2, 0.3, 0.5]])
    out = TemperatureScaler(1.0).transform(p)
    assert np.allclose(out, p, atol=1e-9)


def test_temperature_gt1_softens_lt1_sharpens():
    p = np.array([[0.7, 0.2, 0.1]])
    soft = TemperatureScaler(2.0).transform(p)[0]
    sharp = TemperatureScaler(0.5).transform(p)[0]
    # Softening pulls the top class toward uniform; sharpening pushes it up.
    assert soft[0] < 0.7 < sharp[0]
    # Rows always stay a valid distribution.
    for row in (soft, sharp):
        assert np.isclose(row.sum(), 1.0)


def test_temperature_fit_recovers_overconfidence():
    """If labels are noisier than the probs imply (over-confident model), the
    fitted temperature should soften (T > 1)."""
    rng = np.random.default_rng(0)
    # Confident probs, but outcomes only follow them ~half the time -> over-confident.
    base = np.array([0.8, 0.15, 0.05])
    probs = np.tile(base, (600, 1))
    outs = np.where(rng.random(600) < 0.55, 0, rng.integers(1, 3, size=600))
    t = TemperatureScaler().fit(probs, outs).temperature
    assert t > 1.0


def test_temperature_roundtrip_and_dispatch(tmp_path):
    path = tmp_path / "cal.json"
    TemperatureScaler(0.856).save(path)
    assert json.loads(path.read_text())["method"] == "temperature"
    loaded = load_calibrator(path)
    assert isinstance(loaded, TemperatureScaler)
    assert np.isclose(loaded.temperature, 0.856)


def test_isotonic_dispatch_and_backcompat(tmp_path):
    iso = IndependentIsotonic().fit(
        np.array([[0.5, 0.3, 0.2], [0.6, 0.2, 0.2], [0.4, 0.4, 0.2]]),
        np.array([0, 0, 1]),
    )
    path = tmp_path / "iso.json"
    iso.save(path)
    assert isinstance(load_calibrator(path), IndependentIsotonic)
    # A legacy file with no "method" tag (pre-dispatch) still loads as isotonic.
    d = json.loads(path.read_text())
    d.pop("method")
    (tmp_path / "legacy.json").write_text(json.dumps(d))
    assert isinstance(load_calibrator(tmp_path / "legacy.json"), IndependentIsotonic)


def test_load_calibrator_missing_returns_none(tmp_path):
    assert load_calibrator(tmp_path / "nope.json") is None


def test_calibrate_hda_normalises():
    cal = make_calibrator("temperature")
    h, d, a = calibrate_hda(cal, 0.5, 0.3, 0.2)
    assert np.isclose(h + d + a, 1.0)
