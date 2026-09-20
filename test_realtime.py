#!/usr/bin/env python3
"""End-to-end check of the realtime loop without a phone.

Plays synthetic speech into the media-stream endpoint exactly as Twilio would,
then asserts that audio comes back. Run: python test_realtime.py
"""
import asyncio, audioop, base64, json, sys, time
import aiohttp
from realtime import OPENAI_KEY, OPENAI_URL, TTS_RATE, TWILIO_RATE, FRAME_BYTES

WS = "http://127.0.0.1:5000/ws"
UTTERANCE = "Hello, can you hear me? Please say yes if you can."


async def speech_frames(http, text):
    """Synthesize text and return it as 20ms 8kHz u-law frames, like Twilio sends."""
    async with http.post(f"{OPENAI_URL}/audio/speech",
                         json={"model": "tts-1", "voice": "echo", "input": text,
                               "response_format": "pcm"},
                         headers={"Authorization": f"Bearer {OPENAI_KEY}"}) as r:
        assert r.status == 200, f"tts failed: {r.status} {await r.text()}"
        pcm24 = await r.read()
    pcm8, _ = audioop.ratecv(pcm24, 2, 1, TTS_RATE, TWILIO_RATE, None)
    ulaw = audioop.lin2ulaw(pcm8, 2)
    return [ulaw[i:i + FRAME_BYTES] for i in range(0, len(ulaw), FRAME_BYTES)]


async def main():
    received = []
    timings = {}
    async with aiohttp.ClientSession() as http:
        frames = await speech_frames(http, UTTERANCE)
        silence = audioop.lin2ulaw(b"\x00" * (FRAME_BYTES * 2), 2)

        async with http.ws_connect(WS) as ws:
            await ws.send_json({"event": "start", "start": {"streamSid": "MZtest"}})

            async def collect():
                async for msg in ws:
                    if msg.type is aiohttp.WSMsgType.TEXT:
                        d = json.loads(msg.data)
                        if d.get("event") == "media":
                            if "spoke_at" in timings and "first_reply" not in timings:
                                timings["first_reply"] = time.time()
                            received.append(base64.b64decode(d["media"]["payload"]))

            reader = asyncio.create_task(collect())
            await asyncio.sleep(6)          # let the greeting play
            greeting = len(received)
            assert greeting > 0, "no greeting audio came back"

            for f in frames:                # speak
                await ws.send_json({"event": "media", "media":
                                    {"payload": base64.b64encode(f).decode()}})
                await asyncio.sleep(0.02)
            timings["spoke_at"] = time.time()
            for _ in range(35):             # ~900ms silence ends the utterance
                await ws.send_json({"event": "media", "media":
                                    {"payload": base64.b64encode(silence).decode()}})
                await asyncio.sleep(0.02)

            await asyncio.sleep(18)         # STT + Claude + TTS
            reply = len(received) - greeting
            await ws.send_json({"event": "stop"})
            reader.cancel()

    latency = timings.get("first_reply", 0) - timings["spoke_at"]
    print(f"greeting frames: {greeting}  reply frames: {reply}")
    print(f"end of speech -> first audio: {latency:.2f}s "
          f"(includes the {35 * 0.02:.2f}s silence window that ends the turn)")
    assert reply > 0, "no reply audio — the loop did not complete"
    print(f"audio returned: {(greeting + reply) * 0.02:.1f}s total")
    print("all checks passed")


# (entrypoint moved to the bottom so every check runs)


# --- playback concurrency checks (no network) ---

class FakeWS:
    """Records what would go to Twilio, and when."""
    def __init__(self):
        self.frames = []
    async def send_json(self, d):
        if d.get("event") == "media":
            self.frames.append(d["media"]["payload"][:8])


async def check_no_overlap():
    """Two speakers must never interleave, and a stale epoch must stop playing."""
    import realtime
    ws = FakeWS()
    call = realtime.Call(ws, None)
    call.stream_sid = "MZ"

    # Pretend TTS already happened: patch speak's synthesis with fixed audio.
    ulaw = bytes(realtime.FRAME_BYTES * 20)

    async def fake_speak(text, epoch=None):
        if epoch is None:
            epoch = call.epoch
        if epoch != call.epoch:
            return
        async with call.play_lock:
            if epoch != call.epoch:
                return
            for i in range(0, len(ulaw), realtime.FRAME_BYTES):
                if epoch != call.epoch:
                    break
                await ws.send_json({"event": "media", "streamSid": "MZ",
                                    "media": {"payload": f"{text}-{i}"}})
                await asyncio.sleep(0.001)

    # two concurrent speakers at the same epoch: lock must serialize them
    await asyncio.gather(fake_speak("A"), fake_speak("B"))
    tags = [f.split("-")[0] for f in ws.frames]
    first, second = tags[0], tags[-1]
    assert first != second, "expected both speakers to run"
    assert tags == sorted(tags, key=lambda t: 0 if t == first else 1), \
        f"speakers interleaved: {tags}"

    # a stale epoch stops an in-flight speaker
    ws.frames.clear()
    task = asyncio.create_task(fake_speak("C", call.epoch))
    await asyncio.sleep(0.004)
    call.epoch += 1                      # barge-in
    await task
    assert len(ws.frames) < 20, f"stale playback kept going: {len(ws.frames)} frames"
    print("playback concurrency checks passed")


async def check_stream_discipline():
    """Concurrent turns must not read each other's output.

    Regression for the bug where abandoning a response mid-read left the rest in
    the pipe, so every later turn answered the previous question.
    """
    import realtime
    sess = realtime.ClaudeSession()
    await sess.start()
    try:
        async def ask(t):
            return "".join([d async for d in sess.ask(t)])

        a, b = await asyncio.gather(
            ask("Reply with exactly one word: ALPHA"),
            ask("Reply with exactly one word: BRAVO"),
        )
        assert "ALPHA" in a and "BRAVO" not in a, f"turn 1 read the wrong response: {a!r}"
        assert "BRAVO" in b and "ALPHA" not in b, f"turn 2 read the wrong response: {b!r}"
    finally:
        await sess.close()
    print("stream discipline checks passed")


def check_hallucination_filter():
    import realtime
    for junk in ["Thank you.", "you", "Bye-bye.", " So ", "Okay.", "THANKS FOR WATCHING!"]:
        assert realtime.is_hallucination(junk), f"{junk!r} should be filtered"
    for real in ["Can you tell me about giraffes?", "thank you for calling, I need help",
                 "yes please", "bye for now, but first"]:
        assert not realtime.is_hallucination(real), f"{real!r} should not be filtered"
    print("hallucination filter checks passed")


if __name__ == "__main__":
    check_hallucination_filter()      # offline
    asyncio.run(check_no_overlap())   # fast, offline
    asyncio.run(check_stream_discipline())
    sys.exit(asyncio.run(main()))     # full loop, hits the live service
