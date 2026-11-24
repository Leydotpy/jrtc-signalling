import pytest
from media_helpers import publish_payload, sdp

from jrtc_signalling.auth import AuthError
from jrtc_signalling.contracts import OperationError
from jrtc_signalling.sdp import publisher_tracks, sections, subscriber_answer


@pytest.mark.parametrize(
    "mutate",
    [
        lambda text: "v=1" + text[3:],
        lambda text: text + "a=x:" + "x" * 65536,
        lambda text: text.replace("a=mid:0", "a=mid:0\r\na=mid:1"),
        lambda text: text.replace("a=mid:0\r\n", ""),
        lambda text: text.replace("a=mid:0", "a=mid:bad mid"),
        lambda text: text.replace("a=mid:0", "a=mid:" + "x" * 129),
        lambda text: text + "a=recvonly\r\n",
        lambda text: text.replace("audio 9", "audio 99999"),
        lambda text: text.replace("audio 9", "audio ٩"),
        lambda text: text.replace("audio 9", "audio +9"),
        lambda text: text.replace("UDP/TLS/RTP/SAVPF", "UDP/DTLS/SCTP"),
        lambda text: text.replace("a=sendonly", "a=sendonly\x00"),
        lambda text: text.replace("a=sendonly", "a=sendonly\x0b"),
        lambda text: text.replace("a=sendonly", "a=sendonly\r"),
        lambda text: text + "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\na=mid:1\r\n",
        lambda text: sdp(kinds=("audio", "video"), mids=("0", "0")),
    ],
)
def test_ambiguous_or_unbounded_sdp_is_rejected_without_echo(mutate):
    with pytest.raises(OperationError) as error:
        sections(mutate(sdp()))
    assert "SG08" not in str(error.value)


def test_publisher_mids_and_track_kind_are_bound_and_limited():
    permissions = {"audio": True, "video": True, "screen": False}
    payload = publish_payload()
    payload["tracks"][0].pop("mid")
    assert publisher_tracks(sdp(), payload["tracks"], permissions)["0"]["source"] == "microphone"
    with pytest.raises(OperationError):
        publisher_tracks(sdp(kinds=("audio", "audio")), payload["tracks"], permissions)
    with pytest.raises(OperationError):
        publisher_tracks(sdp(kinds=("audio",) * 17), [], permissions)
    with pytest.raises(AuthError):
        publisher_tracks(sdp("recvonly"), payload["tracks"], permissions)


@pytest.mark.parametrize("direction", ["sendrecv", "sendonly"])
def test_subscriber_cannot_publish_through_answer(direction):
    with pytest.raises(AuthError):
        subscriber_answer(sdp(direction), sections(sdp()))


def test_subscriber_answer_cannot_reorder_or_activate_rejected_sections():
    offer = sections(sdp(kinds=("audio", "video")))
    with pytest.raises(OperationError):
        subscriber_answer(sdp("recvonly", kinds=("video", "audio")), offer)
    with pytest.raises(OperationError):
        subscriber_answer(sdp("recvonly"), sections(sdp().replace("audio 9", "audio 0")))
    subscriber_answer(sdp("inactive"), sections(sdp()))
