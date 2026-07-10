"""`alle test`: the service.test() probe + optional
--speed path and its rendering. The real transfers (throughput.run) are stubbed so
tests stay hermetic and offline; here we check orchestration: which channels get
probed vs. speed-tested, filtering, the not-running path, latency reuse, and output."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from alle import cli, service
from conftest import wg_config

WG = wg_config("1.2.3.4")


@pytest.fixture
def two_channels():
    store = service.Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "United States", "", dict(WG))
    store.add_channel("nordvpn", "Japan", "", dict(WG))


@pytest.fixture
def stub_running(monkeypatch):
    class Runner:
        def is_running(self):
            return True

    monkeypatch.setattr(service.singbox, "Runner", Runner)


@pytest.fixture
def stub_throughput(monkeypatch):
    calls = []

    def fake_run(port, timeout=60, progress=None, measure_latency=True):
        if progress:  # exercise the phase callback the CLI spinner relies on
            phases = (
                ("latency", "download", "upload")
                if measure_latency
                else ("download", "upload")
            )
            for phase in phases:
                progress(phase)
        calls.append({"port": port, "measure_latency": measure_latency})
        return {
            "latency_ms": 42.0 if measure_latency else None,
            "download_bps": 150e6,
            "upload_bps": 12e6,
        }

    monkeypatch.setattr(service.throughput, "run", fake_run)
    return calls


@pytest.fixture
def stub_probe(monkeypatch):
    def fake_probe(self, channels=None):
        channels = self.store.channels() if channels is None else channels
        out = {}
        for ch in channels:
            result = {
                "ok": True,
                "at": 1,
                "latency_ms": 12.3,
                "ip": "1.2.3.4",
                "error": None,
            }
            self.store.set_probe(ch.provider, ch.id, result)
            out[f"{ch.provider}/{ch.id}"] = result
        return out

    monkeypatch.setattr(service.Engine, "probe_all", fake_probe)


@pytest.fixture
def stub_probe_one_failed(monkeypatch):
    def fake_probe(self, channels=None):
        channels = self.store.channels() if channels is None else channels
        out = {}
        for ch in channels:
            ok = ch.id == "japan_1"
            result = {
                "ok": ok,
                "at": 1,
                "latency_ms": 12.3 if ok else None,
                "ip": "1.2.3.4" if ok else None,
                "error": None if ok else "timeout",
            }
            self.store.set_probe(ch.provider, ch.id, result)
            out[f"{ch.provider}/{ch.id}"] = result
        return out

    monkeypatch.setattr(service.Engine, "probe_all", fake_probe)


def test_test_default_probes_without_speed(two_channels, stub_probe, stub_throughput):
    data = service.test()
    assert data["speed"] is False
    assert data["healthy_count"] == 2
    assert [r["name"] for r in data["channels"]] == ["japan_1", "united_states_1"]
    assert data["channels"][0]["latency_ms"] == 12.3
    assert stub_throughput == []


def test_test_speed_runs_only_healthy_channels(
    two_channels, stub_probe_one_failed, stub_throughput
):
    data = service.test(speed=True)
    assert data["speed"] is True
    assert data["healthy_count"] == 1
    assert len(stub_throughput) == 1
    assert stub_throughput[0]["measure_latency"] is False  # latency not re-measured
    tested = {r["name"]: r["speed_result"] for r in data["channels"]}
    assert tested["japan_1"]["tested"] is True
    assert (
        tested["japan_1"]["latency_ms"] == 12.3
    )  # probe latency reused, not re-measured
    assert tested["united_states_1"]["skip_reason"] == "unhealthy"


def test_test_speed_filters_to_one_channel(
    two_channels, stub_running, stub_probe, stub_throughput
):
    data = service.test(speed=True, channel="japan_1")
    assert [r["name"] for r in data["channels"]] == ["japan_1"]
    assert len(stub_throughput) == 1  # only the one channel's proxy was driven


def test_test_unknown_channel_errors(two_channels):
    with pytest.raises(service.ServiceError) as e:
        service.test(channel="nope")
    assert "no channel named 'nope'" in str(e.value)


def test_test_speed_when_stopped_skips_transfers(two_channels, monkeypatch):
    class Runner:
        def is_running(self):
            return False

    monkeypatch.setattr(service.singbox, "Runner", Runner)
    monkeypatch.setattr(
        service.throughput,
        "run",
        lambda *a, **k: pytest.fail("must not test while stopped"),
    )
    data = service.test(speed=True)
    assert data["running"] is False
    assert all(
        r["speed_result"]["skip_reason"] == "unhealthy" for r in data["channels"]
    )


def test_cli_streams_a_row_per_channel(
    two_channels, stub_running, stub_probe, stub_throughput, capsys
):
    cli.main(["test", "--speed"])
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].split()[:2] == ["LABEL", "ID"]  # shared channel-table lead
    assert set(lines[1]) <= {"-", " "} and "-" in lines[1]  # dash separator second
    # rows carry the provider-qualified id (globally unique)
    assert "nordvpn/japan_1" in out and "nordvpn/united_states_1" in out
    assert out.count("150.0 Mbps") == 2


def test_cli_test_default_is_connectivity_only(
    two_channels, stub_running, stub_probe, stub_throughput, capsys
):
    cli.main(["test"])
    out = capsys.readouterr().out
    assert "IP" in out and "1.2.3.4" in out and "12.3ms" in out
    assert "PORT" in out.splitlines()[0]  # PORT column present
    assert re.search(r":\d{2,5}\b", out)  # a :<port> value is rendered
    assert "DOWNLOAD" not in out
    assert "ERROR" not in out  # no separate error column; reason folds into STATE
    assert stub_throughput == []


def test_cli_stopped_message(two_channels, monkeypatch, capsys):
    class Runner:
        def is_running(self):
            return False

    monkeypatch.setattr(service.singbox, "Runner", Runner)
    cli.main(["test", "--speed"])
    out = capsys.readouterr().out
    assert "Stopped" in out  # failure reason folds into STATE, like `alle status`


def test_cli_json(two_channels, stub_running, stub_probe, stub_throughput, capsys):
    cli.main(["test", "--speed", "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["running"] is True and len(data["channels"]) == 2
    assert data["speed"] is True
    assert data["channels"][0]["speed_result"]["tested"] is True


def test_test_speed_streams_on_row_and_on_begin(
    two_channels, stub_running, stub_probe, stub_throughput
):
    """The streaming callbacks reveal each channel as it finishes: on_begin once
    up front (enough to size the table), on_row once per fully-done channel."""
    begins = []
    rows = []
    data = service.test(
        speed=True,
        on_begin=begins.append,
        on_row=rows.append,
    )
    # on_begin fires exactly once, before any result, with channel identity.
    assert len(begins) == 1
    began = begins[0]
    assert [c["name"] for c in began] == ["japan_1", "united_states_1"]
    assert len({c["port_number"] for c in began}) == 2  # distinct ports
    assert all(c["country"] and c["city"] and c["port"] for c in began)

    # on_row fires once per healthy channel, each with its speed result filled in.
    assert [r["name"] for r in rows] == ["japan_1", "united_states_1"]
    assert all(r["speed_result"]["tested"] for r in rows)

    # The final aggregate is unchanged from the non-streaming path.
    assert data["speed"] is True and data["healthy_count"] == 2
    assert [r["name"] for r in data["channels"]] == ["japan_1", "united_states_1"]


def test_run_streaming_test_prints_live_table(
    two_channels, stub_running, stub_probe, stub_throughput, capsys
):
    """The TTY streaming path: header first, then a row per channel, then a
    summary line. The progress line lives on stderr, so stdout is just the table.
    """
    cli._run_streaming_test(SimpleNamespace(channel=None))
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[0].split()[:2] == ["LABEL", "ID"]  # header leads the channel table
    assert set(lines[1]) <= {"-", " "} and "-" in lines[1]  # dash separator
    assert captured.out.count("150.0 Mbps") == 2  # both channels' download column
    assert (
        "nordvpn/japan_1" in captured.out and "nordvpn/united_states_1" in captured.out
    )
    assert lines[-1] == "2 healthy · 0 failed"  # final summary
    # the animated progress indicator lives on stderr, never polluting the table
    assert "⠋" not in captured.out
