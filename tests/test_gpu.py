import sys
import types

import pytest

from tonematch import gpu


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("TONEMATCH_DEVICE", raising=False)


def _fake_torch(monkeypatch, cuda=False, hip=None, mps=False):
    t = types.ModuleType("torch")
    t.cuda = types.SimpleNamespace(is_available=lambda: cuda)
    t.version = types.SimpleNamespace(hip=hip)
    t.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps))
    monkeypatch.setitem(sys.modules, "torch", t)
    monkeypatch.setattr(gpu.importlib.util, "find_spec", lambda n: object())


def test_detect_returns_known_backend():
    assert gpu.detect_backend() in gpu.BACKENDS


def test_override(monkeypatch):
    monkeypatch.setenv("TONEMATCH_DEVICE", "CPU")
    assert gpu.detect_backend() == "cpu"


def test_override_invalid(monkeypatch):
    monkeypatch.setenv("TONEMATCH_DEVICE", "tpu")
    with pytest.raises(ValueError):
        gpu.detect_backend()


def test_conventions():
    assert gpu.SAMPLE_RATE == 48_000 and gpu.DTYPE == "float32"


def test_cuda(monkeypatch):
    _fake_torch(monkeypatch, cuda=True)
    assert gpu.detect_backend() == "cuda"


def test_rocm(monkeypatch):
    _fake_torch(monkeypatch, cuda=True, hip="6.0")
    assert gpu.detect_backend() == "rocm"


def test_mps(monkeypatch):
    _fake_torch(monkeypatch, mps=True)
    assert gpu.detect_backend() == "mps"


def test_directml(monkeypatch):
    ort = types.ModuleType("onnxruntime")
    ort.get_available_providers = lambda: ["DmlExecutionProvider", "CPUExecutionProvider"]
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setitem(sys.modules, "torch", None)  # import fails

    def spec(name):
        return None if name == "torch" else object()

    monkeypatch.setattr(gpu.importlib.util, "find_spec", spec)
    monkeypatch.setattr(sys, "platform", "win32")
    assert gpu.detect_backend() == "directml"


def test_cpu_when_nothing(monkeypatch):
    monkeypatch.setattr(gpu.importlib.util, "find_spec", lambda n: None)
    assert gpu.detect_backend() == "cpu"
