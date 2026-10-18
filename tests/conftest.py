"""Shared test settings.

StompEngine defaults added in T110-T112 (stomp-engine, 2026-10-17): the stream-slip guard (pilot
loopback), device-template capture and whole writes for delay/reverb changes. Tests written before
them use fake bridges that model none of these, so they run with the three turned off; the tests
of the new behaviour (tests/test_stomp_robust.py) turn them back on explicitly.
"""
import pytest


@pytest.fixture(autouse=True)
def _stomp_pre_t112_defaults(monkeypatch, request):
    if request.node.get_closest_marker("stomp_robust"):
        return
    monkeypatch.setenv("TONEMATCH_STOMP_SLIP_GUARD", "off")
    monkeypatch.setenv("TONEMATCH_STOMP_TEMPLATES", "off")
    monkeypatch.setenv("TONEMATCH_STOMP_AMBIENT_INPLACE", "1")


@pytest.fixture(autouse=True)
def _host_budget_off(monkeypatch, request):
    """T133: the host scaler is off in tests written before it (no real machine load, no registry);
    tests/test_host_budget.py and the ``host_budget`` marker turn it on explicitly."""
    if not request.node.get_closest_marker("host_budget"):
        monkeypatch.setenv("TONEMATCH_HOST_BUDGET", "off")


def pytest_configure(config):
    config.addinivalue_line("markers", "host_budget: run with the host scaler on")
    config.addinivalue_line("markers", "stomp_robust: run with the T110-T112 StompEngine defaults")
