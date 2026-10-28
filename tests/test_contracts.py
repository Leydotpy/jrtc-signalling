import copy
import json
from importlib.resources import files
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from jrtc_signalling.contracts import ContractError, decode_message, validate_message

FIXTURES = json.loads((Path(__file__).parent / "fixtures/v1.json").read_text())


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f["name"])
def test_contract_examples(fixture):
    assert (
        decode_message(json.dumps(fixture["message"]), schema=fixture["schema"])
        == fixture["message"]
    )


def example(name):
    return copy.deepcopy(next(f["message"] for f in FIXTURES if f["name"] == name))


@pytest.mark.parametrize("value", [None, [], 1, True, "hello", {}])
def test_non_envelopes_reject(value):
    with pytest.raises(ContractError):
        validate_message(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2),
        ("version", True),
        ("id", ""),
        ("generation", 0),
        ("generation", True),
        ("sequence", -1),
        ("sequence", 2**53),
        ("op", "participant.admit"),
        ("meeting_id", "../other"),
        ("participant_session_id", 42),
        ("unexpected", "private-value"),
    ],
)
def test_invalid_envelopes_reject(field, value):
    message = example("ping")
    message[field] = value
    with pytest.raises(ContractError):
        validate_message(message)


@pytest.mark.parametrize("value", ["true", 1, None, [], {}])
def test_completion_requires_real_boolean(value):
    message = example("ice-final")
    message["payload"]["completed"] = value
    with pytest.raises(ContractError):
        validate_message(message)


def test_final_candidates_are_preserved_and_empty_incomplete_is_invalid():
    message = example("ice-final")
    result = validate_message(message)
    assert len(result["payload"]["candidates"]) == 2
    assert result["payload"]["completed"] is True
    message["payload"]["candidates"] = []
    validate_message(message)  # Explicit completion-only is valid.
    message["payload"]["completed"] = False
    with pytest.raises(ContractError):
        validate_message(message)


def test_candidate_limits_and_target_are_validated_before_dispatch():
    message = example("ice-final")
    candidate = message["payload"]["candidates"][0]
    message["payload"]["candidates"] = [candidate] * 32
    validate_message(message)
    message["payload"]["candidates"].append(candidate)
    with pytest.raises(ContractError):
        validate_message(message)
    message["payload"]["candidates"] = [{"candidate": "private-candidate"}]
    with pytest.raises(ContractError):
        validate_message(message)


def test_ice_byte_limit_includes_multibyte_candidates():
    message = example("ice-final")
    message["payload"]["candidates"] = [{"candidate": "\u00e9" * 2100, "sdpMid": "0"}] * 16
    with pytest.raises(ContractError):
        validate_message(message)


@pytest.mark.parametrize(
    "field,value",
    [
        ("candidate", ""),
        ("sdpMLineIndex", True),
        ("sdpMLineIndex", -1),
        ("usernameFragment", 7),
        ("address", "192.0.2.1"),
    ],
)
def test_invalid_candidates_reject(field, value):
    message = example("ice-final")
    message["payload"]["candidates"][0][field] = value
    with pytest.raises(ContractError):
        validate_message(message)


def test_v1_does_not_silently_accept_singular_candidates():
    message = example("ice-final")
    message["payload"]["candidate"] = message["payload"].pop("candidates")[0]
    with pytest.raises(ContractError):
        validate_message(message)


@pytest.mark.parametrize("value", [1, 9007199254740992, "0", "-1", "01", "1e2"])
def test_janus_ids_reject_numeric_or_noncanonical_input(value):
    message = example("media-ack")
    message["result"]["selected_streams"][0]["feed"] = value
    with pytest.raises(ContractError):
        validate_message(message, schema="response")


def test_browser_ack_retains_large_decimal_ids_and_nullable_selection_fields():
    message = example("media-ack")
    result = validate_message(message, schema="response")["result"]
    assert result["selected_streams"][0] == {
        "feed": "900719925474099312345",
        "mid": "0",
        "sub_mid": None,
        "crossrefid": None,
    }
    assert result["jsep"]["type"] == "answer"


@pytest.mark.parametrize("name,key", [("media-ack", "error"), ("error-ack", "result")])
def test_responses_cannot_succeed_and_fail_together(name, key):
    message = example(name)
    message[key] = example("error-ack")["error"] if key == "error" else {}
    with pytest.raises(ContractError):
        validate_message(message, schema="response")


@pytest.mark.parametrize("schema,name", [("command", "admit"), ("event", "connected")])
def test_broker_messages_cannot_carry_signalling_or_credentials(schema, name):
    for key in ["sdp", "jsep", "candidate", "candidates", "ticket", "token", "metadata"]:
        message = example(name)
        message["data"][key] = "private-value"
        with pytest.raises(ContractError):
            validate_message(message, schema=schema)


def test_domain_messages_require_bound_participant_and_owner():
    message = example("admit")
    for field in ["target_owner", "participant_id", "participant_session_id"]:
        invalid = {**message, field: None}
        with pytest.raises(ContractError):
            validate_message(invalid, schema="command")
    terminate = example("terminate")
    terminate["participant_id"] = "participant-1"
    with pytest.raises(ContractError):
        validate_message(terminate, schema="command")


@pytest.mark.parametrize(
    "expiry",
    [
        "2026-10-28T12:00:00Z",
        "2026-10-28T12:06:00Z",
        "2026-10-28T12:00:30",
        "2026-10-28",
        "2026-10-28T13:00:30+01:00",
        "2026-10-28T12:00Z",
    ],
)
def test_command_timestamps_have_bounded_utc_window(expiry):
    message = example("admit")
    message["expires_at"] = expiry
    with pytest.raises(ContractError):
        validate_message(message, schema="command")


@pytest.mark.parametrize(
    "change",
    [
        {"aud": "janus"},
        {"exp": 1000},
        {"exp": 1121},
        {"nbf": 999},
        {"scopes": ["lobby", "media:publish"]},
        {"scopes": ["lobby", "lobby"]},
        {"scopes": []},
        {"authorization_epoch": True},
        {"generation": 0},
    ],
)
def test_ticket_claim_shape_is_strict(change):
    message = example("lobby-ticket")
    message.update(change)
    with pytest.raises(ContractError):
        validate_message(message, schema="ticket-claims")


def test_schema_validation_does_not_claim_to_verify_ticket_freshness():
    # An expired example can satisfy a schema. SG-04 must verify signature/current time.
    validate_message(example("lobby-ticket"), schema="ticket-claims")


def test_projection_invalidation_is_contiguous():
    message = example("state-change")
    message["projection_version"] += 1
    with pytest.raises(ContractError):
        validate_message(message, schema="state-notification")


@pytest.mark.parametrize(
    "raw",
    [
        '{"ticket":"sensitive","ticket":"replacement"}',
        '{"data": NaN}',
        '{"data": Infinity}',
        b"\xff",
        "\ud800",
        "[" * 2000,
        "x" * (128 * 1024 + 1),
    ],
)
def test_parser_rejects_malformed_or_oversized_json_without_leaks(raw):
    with pytest.raises(ContractError) as caught:
        decode_message(raw)
    assert "sensitive" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_validation_error_does_not_include_sdp_ticket_or_candidates():
    message = example("publish")
    message["payload"]["offer"]["type"] = "sensitive-sdp"
    with pytest.raises(ContractError) as caught:
        validate_message(message)
    assert "sensitive-sdp" not in str(caught.value)


def test_exported_schemas_are_valid_draft_2020_12():
    for resource in files("jrtc_signalling").joinpath("schemas/v1").iterdir():
        Draft202012Validator.check_schema(json.loads(resource.read_text()))
