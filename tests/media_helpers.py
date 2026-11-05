"""In-process ASGI browser and wire-level mock Janus, never a production adapter."""

import asyncio
import json
from uuid import uuid4

from starlette.websockets import WebSocketDisconnect

from jrtc_signalling.sdp import sections
from jrtc_signalling.websocket_auth import PROTOCOL


def sdp(direction="sendonly", kinds=("audio",), mids=None):
    mids = mids or [str(i) for i in range(len(kinds))]
    lines = [
        "v=0",
        "o=- 1000 1 IN IP4 127.0.0.1",
        "s=SG08",
        "t=0 0",
        "a=group:BUNDLE " + " ".join(mids),
    ]
    for mid, kind in zip(mids, kinds, strict=True):
        pt, codec = ("111", "opus/48000/2") if kind == "audio" else ("96", "VP8/90000")
        lines.extend(
            [
                f"m={kind} 9 UDP/TLS/RTP/SAVPF {pt}",
                "c=IN IP4 0.0.0.0",
                f"a=mid:{mid}",
                f"a={direction}",
                "a=rtcp-mux",
                f"a=rtpmap:{pt} {codec}",
                "a=ice-ufrag:sg08",
                "a=ice-pwd:sg08syntheticpassword123456",
                "a=fingerprint:sha-256 " + ":".join(["AB"] * 32),
                "a=setup:actpass",
                "a=ssrc:12345 cname:sg08",
                "a=msid:sg08stream sg08track",
            ]
        )
    return "\r\n".join(lines) + "\r\n"


def publish_payload(*, kind="audio", source="microphone", negotiation=None):
    return {
        "offer": {"type": "offer", "sdp": sdp(kinds=(kind,))},
        "tracks": [{"kind": kind, "source": source, "mid": "0"}],
        "negotiation_id": negotiation or str(uuid4()),
    }


class Browser:
    def __init__(self, app, claims, sign):
        self.app, self.claims, self.sign = app, claims, sign
        self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue()
        self.task, self.sequence, self.notifications = None, 0, []
        self.stall_sends = False
        self.send_blocked = asyncio.Event()

    async def output(self, event):
        if self.stall_sends and event["type"] == "websocket.send":
            self.send_blocked.set()
            await asyncio.Event().wait()
        await self.outgoing.put(event)

    async def open(self):
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "scheme": "wss",
            "path": "/ws",
            "raw_path": b"/ws",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"origin", b"https://meet.example")],
            "subprotocols": [PROTOCOL],
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 1234),
        }
        self.task = asyncio.create_task(self.app(scope, self.incoming.get, self.output))
        await self.incoming.put({"type": "websocket.connect"})
        assert (await asyncio.wait_for(self.outgoing.get(), 5))["type"] == "websocket.accept"
        return await self.request("session.hello", {"ticket": self.sign(self.claims)})

    def message(self, op, payload=None):
        self.sequence += 1
        return {
            "version": 1,
            "id": str(uuid4()),
            "op": op,
            "meeting_id": self.claims["meeting_id"],
            "participant_session_id": self.claims["participant_session_id"],
            "generation": self.claims["generation"],
            "sequence": self.sequence,
            "payload": payload or {},
        }

    async def send(self, message):
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps(message)})

    async def receive(self):
        event = await asyncio.wait_for(self.outgoing.get(), 8)
        if event["type"] == "websocket.close":
            raise WebSocketDisconnect(event["code"], event.get("reason", ""))
        return json.loads(event["text"])

    async def reply(self, request_id):
        while True:
            result = await self.receive()
            if result.get("id") == request_id:
                return result
            assert "event_id" in result, result
            self.notifications.append(result)

    async def request(self, op, payload=None):
        message = self.message(op, payload)
        await self.send(message)
        return await self.reply(message["id"])

    async def close(self):
        if self.task is not None:
            if not self.task.done():
                await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(self.task, 10)


class MockJanus:
    def __init__(self):
        self.requests, self.events, self.handles, self.feeds = [], [], {}, {}
        self.rooms, self.jobs = set(), set()
        self.next_id, self.connection, self.block = 2**53 + 100, None, None
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.webrtc_up = True
        self.mutate_response = lambda result: result

    async def handler(self, connection):
        self.connection = connection
        try:
            async for raw in connection:
                request = json.loads(raw)
                self.requests.append(request)
                task = asyncio.create_task(self.respond(request))
                self.jobs.add(task)
                task.add_done_callback(self.jobs.discard)
        finally:
            jobs = tuple(self.jobs)
            for task in jobs:
                task.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)

    async def send(self, data):
        self.events.append(data)
        await self.connection.send(json.dumps(data))

    async def respond(self, request):
        base = {"transaction": request["transaction"], "session_id": 777}
        command = request["janus"]
        if command == "create":
            await self.send({**base, "janus": "success", "data": {"id": 777}})
            return
        if command == "attach":
            self.next_id += 1
            self.handles[self.next_id] = {}
            await self.send({**base, "janus": "success", "data": {"id": self.next_id}})
            return
        if command in {"keepalive", "trickle"}:
            await self.send({**base, "janus": "ack"})
            return
        if command in {"detach", "destroy", "hangup"}:
            if command == "detach":
                self.handles.pop(request["handle_id"], None)
            await self.send({**base, "janus": "success"})
            return
        assert command == "message"
        body = request["body"]
        method, handle = body["request"], self.handles[request["handle_id"]]
        base["sender"] = request["handle_id"]
        jsep = None
        if method in {"create", "destroy", "exists", "list"}:
            if method == "create":
                self.rooms.add(body["room"])
                data = {"videoroom": "created", "room": body["room"], "permanent": False}
            elif method == "destroy":
                self.rooms.discard(body["room"])
                data = {"videoroom": "destroyed", "room": body["room"]}
            elif method == "exists":
                data = {
                    "videoroom": "success",
                    "room": body["room"],
                    "exists": body["room"] in self.rooms,
                }
            else:
                data = {"videoroom": "success", "list": []}
            await self.send(
                {
                    **base,
                    "janus": "success",
                    "plugindata": {"plugin": "janus.plugin.videoroom", "data": data},
                }
            )
            return
        await self.send({**base, "janus": "ack"})
        if self.block == method:
            self.entered.set()
            await self.release.wait()
            if request["handle_id"] not in self.handles:
                return
        if method in {"join", "joinandconfigure"} and body["ptype"] == "publisher":
            handle["room"], handle["feed"] = body["room"], request["handle_id"] + 1000
            data = {
                "videoroom": "joined",
                "room": body["room"],
                "id": handle["feed"],
                "publishers": [],
            }
        elif method == "join":
            handle["room"], handle["targets"] = body["room"], body["streams"]
            data = {"videoroom": "attached", "room": body["room"], "streams": self.layout(handle)}
            jsep = self.offer(handle)
        elif method in {"publish", "configure"}:
            data = {"videoroom": "event", "room": handle["room"], "configured": "ok"}
        elif method == "unpublish":
            self.feeds.pop(handle["feed"], None)
            data = {"videoroom": "event", "room": handle["room"], "unpublished": "ok"}
        elif method == "update":
            removed = {(s["feed"], s["mid"]) for s in body.get("unsubscribe", [])}
            handle["targets"] = [
                t for t in handle["targets"] if (t["feed"], t["mid"]) not in removed
            ] + body.get("subscribe", [])
            data = {"videoroom": "updated", "room": handle["room"], "streams": self.layout(handle)}
            if handle["targets"]:
                jsep = self.offer(handle)
        elif method == "start":
            data = {"videoroom": "event", "room": handle["room"], "started": "ok"}
        else:
            raise AssertionError(method)
        if "jsep" in request and method != "start":
            offer = request["jsep"]["sdp"]
            self.feeds[handle["feed"]] = {s.mid: s.kind for s in sections(offer)}
            jsep = {
                "type": "answer",
                "sdp": offer.replace("a=sendonly", "a=recvonly")
                .replace("a=sendrecv", "a=recvonly")
                .replace("a=setup:actpass", "a=setup:active"),
            }
        result = {
            **base,
            "janus": "event",
            "plugindata": {"plugin": "janus.plugin.videoroom", "data": data},
        }
        if jsep:
            result["jsep"] = jsep
        await self.send(self.mutate_response(result))
        if self.webrtc_up and "jsep" in request and method != "start":
            await self.send(
                {"janus": "webrtcup", "session_id": 777, "sender": request["handle_id"]}
            )

    def layout(self, handle):
        return [
            {
                "mid": str(i),
                "feed_id": t["feed"],
                "feed_mid": t["mid"],
                "type": self.feeds[t["feed"]][t["mid"]],
                "active": True,
                "ready": True,
            }
            for i, t in enumerate(handle["targets"])
        ]

    def offer(self, handle):
        return {
            "type": "offer",
            "sdp": sdp(kinds=[self.feeds[t["feed"]][t["mid"]] for t in handle["targets"]]),
        }
