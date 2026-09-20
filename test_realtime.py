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


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
