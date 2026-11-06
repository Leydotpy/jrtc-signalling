"""Real test-only TURN and peer adapters; none are shipped with the gateway."""

import asyncio
import secrets
import shutil
import socket
import subprocess

from jrtc_signalling.ice import description


class TurnServer:
    def __init__(self, directory):
        self.directory, self.process = directory, None
        self.username, self.password = "sg09", secrets.token_hex(24)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]

    async def start(self):
        assert shutil.which("turnserver"), "Install coturn to run the opted-in relay tests"
        config = self.directory / "turn.conf"
        config.write_text(
            "listening-ip=127.0.0.1\nrelay-ip=127.0.0.1\n"
            f"listening-port={self.port}\nrealm=sg09.test\n"
            f"user={self.username}:{self.password}\nlt-cred-mech\nfingerprint\n"
            "no-tls\nno-dtls\nno-cli\nno-multicast-peers\n"
            "allow-loopback-peers\nrelay-threads=1\nno-software-attribute\n"
            f"pidfile={self.directory / 'turn.pid'}\n"
            f"userdb={self.directory / 'turn.sqlite'}\nlog-file=stdout\n"
        )
        config.chmod(0o600)
        self.process = await asyncio.to_thread(
            subprocess.Popen,
            ["turnserver", "-c", str(config)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        async with asyncio.timeout(10):
            while True:
                assert self.process.poll() is None, "Isolated coturn exited"
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", self.port)
                    writer.close()
                    await writer.wait_closed()
                    return
                except OSError:
                    await asyncio.sleep(0.05)

    async def close(self):
        if self.process is not None:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait, timeout=5)


def split_trickle(sdp):
    """Remove all embedded candidates/completion; send them only via the gateway."""
    media = description(sdp).media
    lines, candidates, index = [], [], -1
    for line in sdp.splitlines():
        if line.startswith("m="):
            index += 1
        if line.startswith("a=candidate:"):
            candidates.append(
                {
                    "candidate": line[2:],
                    "sdpMid": media[index].mid,
                    "sdpMLineIndex": index,
                    "usernameFragment": media[index].ufrag,
                }
            )
        elif line != "a=end-of-candidates":
            lines.append(line)
    assert candidates
    return "\r\n".join(lines) + "\r\n", candidates


async def trickle(browser, role, result, candidates):
    for start in range(0, len(candidates), 16):
        batch = candidates[start : start + 16]
        response = await browser.request(
            "media.trickle",
            {
                "handle_type": role,
                "negotiation_id": result["negotiation_id"],
                "candidates": batch,
                "completed": start + 16 >= len(candidates),
            },
        )
        assert response["ok"], response.get("error")


def relay_only(pc):
    # aiortc exposes no RTCConfiguration.iceTransportPolicy. This test adapter
    # sets aioice's real RELAY policy before gathering, then asserts SDP and the
    # nominated pair below. Host candidates cannot be used as fallback.
    from aioice import TransportPolicy

    for transceiver in pc.getTransceivers():
        connection = transceiver.sender.transport.transport._connection
        connection._transport_policy = TransportPolicy.RELAY


def rotate_local_ice(pc):
    # aiortc has no public restartIce(). Keep DTLS/SRTP and the socket alive and
    # rotate the real aioice agent's credentials. Janus must authenticate a new
    # ICE check before this test counts its restart as connected.
    connection = pc.getTransceivers()[0].sender.transport.transport._connection
    connection._local_username = secrets.token_hex(4)
    connection._local_password = secrets.token_hex(16)
    return connection


def apply_remote_ice(pc, sdp):
    remote = next(s for s in description(sdp).media if s.active)
    connection = pc.getTransceivers()[0].sender.transport.transport._connection
    connection.remote_username, connection.remote_password = remote.ufrag, remote.password


def observe_restart_checks(connection, monkeypatch):
    seen = asyncio.Event()
    original = connection.request_received

    def received(message, *args, **kwargs):
        if (
            message.attributes.get("USERNAME")
            == f"{connection.local_username}:{connection.remote_username}"
        ):
            # request_received itself verifies MESSAGE-INTEGRITY; do not count
            # arbitrary packets, only the successful response from this agent.
            protocol = args[1]
            send = protocol.send_stun

            def response(reply, addr):
                from aioice.stun import Class

                if reply.message_class == Class.RESPONSE:
                    seen.set()
                return send(reply, addr)

            protocol.send_stun = response
            try:
                return original(message, *args, **kwargs)
            finally:
                protocol.send_stun = send
        return original(message, *args, **kwargs)

    monkeypatch.setattr(connection, "request_received", received)
    return seen
