import json

import pytest
from media_helpers import sdp
from test_contracts import example

from jrtc_signalling.contracts import (
    ContractError,
    OperationError,
    decode_message,
    validate_message,
)
from jrtc_signalling.ice import IceState, description


def candidate(index=1, *, ice="sg08", mid="0", line=0):
    return {
        "candidate": f"candidate:{index} 1 udp 1 192.0.2.1 {4000 + index} typ host",
        "sdpMid": mid,
        "sdpMLineIndex": line,
        "usernameFragment": ice,
    }


def test_temporary_singular_shim_preserves_array_and_strict_canonical_contract():
    message = example("ice-final")
    message["payload"].pop("completed")
    message["payload"]["candidate"] = message["payload"].pop("candidates")[0]
    with pytest.raises(ContractError):
        validate_message(message)
    decoded = decode_message(json.dumps(message))
    assert decoded["payload"]["candidates"] == [message["payload"]["candidate"]]
    assert decoded["payload"]["completed"] is False
    message["payload"]["candidates"] = []
    with pytest.raises(ContractError):
        decode_message(json.dumps(message))


@pytest.mark.parametrize(
    "bad",
    [
        {"candidate": "not-an-ice-candidate"},
        {"candidate": "candidate:1 1 udp 1 192.0.2.1 70000 typ host"},
        {"candidate": "candidate:1 1 udp 1 192.0.2.1 9 typ host\r\na=ice-ufrag:leak"},
        {"candidate": "candidate:1 1 udp 1 192.0.2.1 9 typ host ufrag sg08 ufrag sg08"},
        {"candidate": "candidate:1 1 udp 1 192.0.2.1 9 typ host ufrag old1"},
        {"sdpMLineIndex": 0.0},
        {"sdpMLineIndex": True},
        {"completed": True},
    ],
)
def test_malformed_candidate_is_rejected_before_dispatch_without_input_repr(bad):
    message = example("ice-final")
    message["payload"]["candidates"] = [candidate(), candidate(2) | bad]
    with pytest.raises(ContractError) as error:
        decode_message(json.dumps(message))
    assert "192.0.2" not in str(error.value)
    assert "leak" not in str(error.value)


@pytest.mark.parametrize(
    "bad",
    [
        {"usernameFragment": "old1"},
        {"sdpMid": "other"},
        {"sdpMLineIndex": 1},
        {
            "candidate": "candidate:1 1 udp 1 192.0.2.1 9 typ host ufrag old1",
            "usernameFragment": None,
        },
    ],
)
def test_whole_batch_is_bound_to_actual_sdp_credentials_and_mid(bad):
    state = IceState(description(sdp()))
    with pytest.raises(OperationError):
        state.prepare([candidate(), candidate(2) | bad])
    assert state.count == 0 and not state.completed


def test_sdp_inheritance_disabled_sections_and_optional_legacy_fragment():
    value = sdp(kinds=("audio", "video"))
    value = value.replace("a=ice-ufrag:sg08\r\n", "").replace(
        "a=ice-pwd:sg08syntheticpassword123456\r\n", ""
    )
    value = value.replace(
        "t=0 0\r\n", "t=0 0\r\na=ice-ufrag:sg08\r\na=ice-pwd:sg08syntheticpassword123456\r\n"
    )
    value = value.replace("m=video 9", "m=video 0")
    state = IceState(description(value))
    c = candidate()
    c.pop("usernameFragment")
    assert state.prepare([c])[0] == [c]
    with pytest.raises(OperationError):
        state.prepare([candidate(mid="1", line=1)])


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.replace("a=ice-ufrag:sg08", "a=ice-ufrag:sg08\r\na=ice-ufrag:old1"),
        lambda s: s.replace("a=ice-ufrag:sg08\r\n", ""),
        lambda s: s.replace("sg08syntheticpassword123456", "short"),
        lambda s: s.replace("a=ice-ufrag:sg08", "a=ice-ufrag:bad value"),
    ],
)
def test_ambiguous_or_missing_sdp_credentials_reject(change):
    with pytest.raises(OperationError):
        description(change(sdp()))


def test_restart_requires_both_credentials_and_does_not_reset_same_generation_budget():
    previous = IceState(description(sdp()), completed=True, count=256, bytes=2048)
    current = IceState.negotiate(description(sdp()), previous)
    assert current.completed and current.count == 256 and current.bytes == 2048
    with pytest.raises(OperationError):
        current.prepare([candidate()])
    new = description(sdp(ice="sg09"))
    new.require_restart(previous.description)
    fresh = IceState.negotiate(new, previous)
    assert not fresh.completed and fresh.count == 0
    for partial in [
        sdp().replace("a=ice-ufrag:sg08", "a=ice-ufrag:sg09"),
        sdp().replace("sg08synthetic", "sg09synthetic"),
    ]:
        with pytest.raises(OperationError):
            description(partial).require_restart(previous.description)


def test_generation_count_and_bytes_are_bounded():
    state = IceState(description(sdp()), count=256)
    with pytest.raises(OperationError, match="OVERLOADED"):
        state.prepare([candidate()])
    state.count, state.bytes = 0, 256 * 1024
    with pytest.raises(OperationError, match="OVERLOADED"):
        state.prepare([candidate()])


def test_embedded_sdp_completion_closes_trickle_window():
    state = IceState.negotiate(description(sdp() + "a=end-of-candidates\r\n"), None)
    assert state.completed
    with pytest.raises(OperationError):
        state.prepare([candidate()])


def test_a_later_negotiation_cannot_resurrect_retired_browser_credentials():
    first = IceState.negotiate(description(sdp()), None)
    second = IceState.negotiate(description(sdp(ice="sg09")), first)
    with pytest.raises(OperationError, match="STALE_NEGOTIATION"):
        IceState.negotiate(first.description, second)
