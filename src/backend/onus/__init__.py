"""Onus: claim-to-evidence maps for research papers."""

import os as _os

# PyMuPDF's table finder runs an ONNX layout model; onnxruntime would otherwise try to send usage telemetry
# to Microsoft. Onus is private by default, so this is switched off before anything can load onnxruntime.
_os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
