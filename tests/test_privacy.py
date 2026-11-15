"""No third-party telemetry from Onus's dependencies."""

import os

import onus  # noqa: F401


def test_onnxruntime_telemetry_disabled() -> None:
    assert os.environ.get("ORT_DISABLE_TELEMETRY") == "1"
