"""
Tests for agent/config.py.

Track C (tests). Phase C1.

These are deliberately cheap: no network, no event loop, no server.
Their job is to turn "the config file looks fine" into a pass/fail, and
to pin down the two things most likely to be got wrong later --
the URL composition and the validation of crypto_mode.

Run with:
    pytest tests/agent/test_config.py -v
"""

from __future__ import annotations

import json

import pytest

from agent.config import (
    DEFAULT_CSMS_URL,
    DEFAULT_ID_TOKEN,
    SUBPROTOCOL_PQC,
    SUBPROTOCOL_STANDARD,
    AgentConfig,
)
from crypto.provider import VALID_MODES


# -- defaults ----------------------------------------------------------


def test_defaults_are_sane():
    """A config built with no arguments is usable as-is."""
    cfg = AgentConfig()

    assert cfg.station_id == "CP001"
    assert cfg.csms_url == DEFAULT_CSMS_URL
    assert cfg.id_token == DEFAULT_ID_TOKEN
    assert cfg.crypto_mode == "classical"
    assert cfg.subprotocol == SUBPROTOCOL_STANDARD
    assert cfg.log_level == "INFO"


def test_run_id_is_generated_when_not_supplied():
    """
    Downstream code should never have to handle run_id being None.

    The harness sets it explicitly so a whole fleet shares one value;
    a lone agent gets its own.
    """
    cfg = AgentConfig()
    assert cfg.run_id
    assert len(cfg.run_id) == 12

    explicit = AgentConfig(run_id="fixed-run-01")
    assert explicit.run_id == "fixed-run-01"


def test_two_configs_get_different_run_ids():
    assert AgentConfig().run_id != AgentConfig().run_id


# -- URL composition ---------------------------------------------------
#
# This is the highest-value test in the file. csms/server.py takes the
# LAST path segment as the station identity, so a malformed URL does not
# crash -- it connects as the wrong station, or as no station, and the
# failure surfaces much later as a missing row in the fleet view.


def test_ws_url_appends_station_id():
    cfg = AgentConfig(station_id="CP042", csms_url="ws://localhost:9000")
    assert cfg.ws_url == "ws://localhost:9000/CP042"


def test_ws_url_tolerates_trailing_slash():
    """A trailing slash must not produce a double slash, which would
    make the last path segment empty on some parsers."""
    cfg = AgentConfig(station_id="CP042", csms_url="ws://localhost:9000/")
    assert cfg.ws_url == "ws://localhost:9000/CP042"


def test_station_id_with_slash_is_rejected():
    """
    "CP/001" would arrive at the server as station "001", silently.

    Rejecting it at construction is the whole reason __post_init__
    exists.
    """
    with pytest.raises(ValueError, match="must not contain"):
        AgentConfig(station_id="CP/001")


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_station_id_is_rejected(bad):
    with pytest.raises(ValueError, match="non-empty"):
        AgentConfig(station_id=bad)


# -- crypto mode -------------------------------------------------------


@pytest.mark.parametrize("mode", VALID_MODES)
def test_every_contract_1_mode_is_accepted(mode):
    """
    Whatever Contract 1 declares legal, this config accepts.

    Parametrised over the imported tuple rather than a hardcoded list,
    so if Track B ever adds a mode this test covers it automatically
    instead of silently continuing to test three.
    """
    assert AgentConfig(crypto_mode=mode).crypto_mode == mode


def test_unknown_crypto_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown crypto_mode"):
        AgentConfig(crypto_mode="quantum-ish")


# -- subprotocol -------------------------------------------------------


@pytest.mark.parametrize("proto", [SUBPROTOCOL_STANDARD, SUBPROTOCOL_PQC])
def test_both_server_subprotocols_are_accepted(proto):
    """csms/server.py advertises exactly these two."""
    assert AgentConfig(subprotocol=proto).subprotocol == proto


def test_unknown_subprotocol_is_rejected():
    with pytest.raises(ValueError, match="unknown subprotocol"):
        AgentConfig(subprotocol="ocpp1.6")


# -- numeric validation ------------------------------------------------


def test_negative_power_is_rejected():
    with pytest.raises(ValueError, match="max_power_w"):
        AgentConfig(max_power_w=-1.0)


def test_zero_meter_interval_is_rejected():
    """Zero would spin the metering loop without ever sleeping."""
    with pytest.raises(ValueError, match="meter_every_s"):
        AgentConfig(meter_every_s=0.0)


def test_max_delay_below_base_delay_is_rejected():
    with pytest.raises(ValueError, match="reconnect_max_delay_s"):
        AgentConfig(reconnect_base_delay_s=10.0, reconnect_max_delay_s=1.0)


@pytest.mark.parametrize("jitter", [-0.1, 1.1])
def test_out_of_range_jitter_is_rejected(jitter):
    with pytest.raises(ValueError, match="reconnect_jitter"):
        AgentConfig(reconnect_jitter=jitter)


def test_jitter_zero_is_allowed_but_meaningful():
    """
    Zero jitter is legal — it is how you would deliberately demonstrate
    a thundering herd — so it must not be rejected. E2 runs must not
    use it.
    """
    assert AgentConfig(reconnect_jitter=0.0).reconnect_jitter == 0.0


# -- log level ---------------------------------------------------------


def test_log_level_is_normalised_to_upper_case():
    assert AgentConfig(log_level="debug").log_level == "DEBUG"


def test_unknown_log_level_is_rejected():
    with pytest.raises(ValueError, match="unknown log_level"):
        AgentConfig(log_level="chatty")


# -- command line ------------------------------------------------------


def test_from_args_overrides_defaults():
    cfg = AgentConfig.from_args([
        "--station-id", "CP007",
        "--csms-url", "ws://10.0.0.5:9000",
        "--token", "TAG-BLOCKED",
        "--crypto-mode", "pqc",
        "--log-level", "DEBUG",
    ])

    assert cfg.station_id == "CP007"
    assert cfg.ws_url == "ws://10.0.0.5:9000/CP007"
    assert cfg.id_token == "TAG-BLOCKED"
    assert cfg.crypto_mode == "pqc"
    assert cfg.log_level == "DEBUG"


def test_from_args_with_no_arguments_matches_defaults():
    """The command-line path and the in-code path must agree."""
    from_cli = AgentConfig.from_args([])
    in_code = AgentConfig()

    # run_id differs by design, so compare everything else.
    a = from_cli.describe()
    b = in_code.describe()
    a.pop("run_id")
    b.pop("run_id")
    assert a == b


def test_supported_algorithms_parses_comma_separated_list():
    cfg = AgentConfig.from_args(["--supported-algorithms", "alg-a, alg-b ,alg-c"])
    assert cfg.supported_algorithms == ["alg-a", "alg-b", "alg-c"]


def test_supported_algorithms_defaults_to_empty():
    """
    Empty, not invented.

    Putting placeholder algorithm names here before Track B publishes
    real ones would put fiction into the Contract 2 record and from
    there into the results table.
    """
    assert AgentConfig.from_args([]).supported_algorithms == []


# -- serialisation -----------------------------------------------------


def test_describe_includes_derived_url():
    """
    describe() feeds the one start-up log line, so it must carry
    everything needed to reconstruct how a run was configured.
    """
    cfg = AgentConfig(station_id="CP003")
    described = cfg.describe()

    assert described["ws_url"] == cfg.ws_url
    assert described["station_id"] == "CP003"
    assert "crypto_mode" in described
    assert "run_id" in described


def test_describe_is_json_serialisable():
    """It ends up in a log line, so it must survive json.dumps."""
    json.dumps(AgentConfig().describe())


def test_describe_compact_mentions_the_measurement_relevant_fields():
    line = AgentConfig(station_id="CP009", crypto_mode="hybrid").describe_compact()
    assert "CP009" in line
    assert "hybrid" in line


def test_json_round_trip(tmp_path):
    """
    to_json() then from_json_file() must return an equal config.

    This is what lets an experiment's parameters be committed to the
    repo and replayed exactly months later.
    """
    original = AgentConfig(
        station_id="CP123",
        crypto_mode="hybrid",
        supported_algorithms=["alg-a"],
        run_id="run-abc",
    )
    path = tmp_path / "cfg.json"
    path.write_text(original.to_json(), encoding="utf-8")

    restored = AgentConfig.from_json_file(path)
    assert restored == original


def test_from_json_file_rejects_unknown_keys(tmp_path):
    """
    A silently ignored key is a setting you believe you changed and did
    not — the worst kind of configuration bug in an experiment.
    """
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"station_id": "CP001", "hartbeat": 20}),
                    encoding="utf-8")

    with pytest.raises(ValueError, match="unknown configuration keys"):
        AgentConfig.from_json_file(path)