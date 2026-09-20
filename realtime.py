#!/usr/bin/env python3
"""Low-latency voice loop: Twilio Media Streams <-> Whisper <-> Claude CLI <-> OpenAI TTS.

Replaces bot.py's <Gather> turn-taking (3-6s per turn, no interruption) with a
bidirectional audio stream. Claude runs as one long-lived CLI process per call,
so it stays on the subscription instead of the metered API.

    Twilio  --8kHz u-law-->  VAD  -->  Whisper  -->  Claude (stream-json)
            <--8kHz u-law--  u-law <-- resample <--  OpenAI TTS (24kHz pcm)

Barge-in: speech detected during playback clears Twilio's buffer and cancels
the in-flight TTS and Claude turn.
"""
import asyncio
import audioop
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import quoteattr
import base64
import io
import json
import os
import re
import time
import wave

import aiohttp
from aiohttp import web

CLAUDE_PATH = "/home/ubuntu/.local/bin/claude"
CC_CONNECT_PATH = os.environ.get("CC_CONNECT_PATH", "/usr/lib/node_modules/cc-connect/bin/cc-connect")
CALLS_DIR = Path(__file__).parent / "calls"
TWILIO_API = "https://api.twilio.com/2010-04-01/Accounts"
RECORD_CALLS = os.environ.get("RECORD_CALLS", "1") == "1"
OPENAI_URL = "https://api.openai.com/v1"
PORT = int(os.environ.get("REALTIME_PORT", 5000))
FLASK_PORT = int(os.environ.get("FLASK_PORT", 5002))

# Twilio streams 8kHz mono u-law in 20ms frames (160 bytes); OpenAI TTS returns 24kHz PCM16.
TWILIO_RATE, TTS_RATE, FRAME_BYTES = 8000, 24000, 160

# VAD: RMS over 20ms frames of 16-bit PCM. Tuned for phone audio, where line
# noise sits well under 500 and speech runs 1500+.
SPEECH_RMS = 700
ECHO_GUARD_S = 0.4          # ignore input for this long after we stop speaking
BARGE_IN = os.environ.get("BARGE_IN", "0") == "1"
SPEECH_FRAMES = 3           # ~60ms above threshold starts an utterance
SILENCE_FRAMES = 25         # ~500ms below threshold ends one
MAX_UTTERANCE_FRAMES = 1500  # ~30s hard cap

SYSTEM_PROMPT = (
    "You are on a live phone call. Your replies are spoken aloud, so keep them "
    "to one or two sentences and never use markdown, lists, or code. "
    "Speak naturally and conversationally. If you need a moment, say so briefly."
)

SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

# Whisper emits these verbatim on silence or line noise. Treating them as speech
# makes the bot answer things the caller never said, and supersede itself doing it.
HALLUCINATIONS = {
    "you", "thank you", "thanks", "bye", "bye-bye", "goodbye", "so", "uh", "um",
    "thank you for watching", "thanks for watching", "i'll be going",
    "please subscribe", "the end", "okay", "ok", "mm", "hmm", "yeah",
}


def is_hallucination(text: str) -> bool:
    """True when the transcript is one of Whisper's stock silence outputs."""
    cleaned = re.sub(r"[^a-z' -]", "", text.strip().lower()).strip()
    return cleaned in HALLUCINATIONS

OUTBOUND_OPENING = (
    "You are placing this call on behalf of the user. Your goal: {goal}. "
    "Open by saying you are an AI assistant calling on behalf of the user, then "
    "state your purpose in one sentence. Speak only the words you want said aloud."
)

SUMMARY_PROMPT = (
    "The call just ended. In 2-3 short lines, report the outcome to the user: was "
    "the goal achieved, what was agreed (dates, times, names, amounts), and what "
    "follow-up is needed. This is a written report, not speech. No preamble."
)


def openai_key() -> str:
    """Read the OpenAI key from the cc-connect config (already configured for speech)."""
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    with open("/home/ubuntu/.cc-connect/config.toml") as f:
        for line in f:
            if line.strip().startswith("api_key"):
                return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("no OpenAI API key found")


OPENAI_KEY = openai_key()


def pcm_to_wav(pcm: bytes, rate: int = TWILIO_RATE) -> bytes:
    """Wrap raw PCM16 mono in a WAV container for the transcription endpoint."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class ClaudeSession:
    """One long-lived `claude -p` process per call, fed via stream-json."""

    def __init__(self):
        self.proc = None
        # Serializes turns. Abandoning a read mid-response leaves the rest of it
        # in the pipe, and the next turn reads those leftovers as its own answer.
        self.lock = asyncio.Lock()

    async def start(self):
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        self.proc = await asyncio.create_subprocess_exec(
            CLAUDE_PATH, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages", "--verbose",
            "--dangerously-skip-permissions",
            "--append-system-prompt", SYSTEM_PROMPT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )

    async def ask(self, text: str):
        """Send a turn; yield text deltas until this response is fully drained.

        Callers must consume this to exhaustion — stopping early desynchronizes
        every later turn.
        """
        async with self.lock:
            msg = {"type": "user",
                   "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self.proc.stdin.drain()

            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    return
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "stream_event":
                    inner = ev.get("event", {})
                    if inner.get("type") == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield delta["text"]
                elif ev.get("type") == "result":
                    return

    async def close(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            self.proc.terminate()
            await self.proc.wait()


class Call:
    """State for one media stream."""

    def __init__(self, ws, session: aiohttp.ClientSession):
        self.ws = ws
        self.http = session
        self.stream_sid = None
        self.call_sid = None
        self.goal = None
        self.reporter = ("", "")   # (cc-connect project, session key)
        self.claude = ClaudeSession()
        self.frames = []            # PCM16 frames of the utterance in progress
        self.speech_run = 0
        self.silence_run = 0
        self.in_speech = False
        self.speaking = False       # we are playing audio back
        self.quiet_since = 0.0      # when playback last stopped
        self.epoch = 0              # bumped per turn; stale playback aborts itself
        self.play_lock = asyncio.Lock()   # only one speaker on the socket at a time
        self.turn = None            # asyncio.Task for the active reply
        self.transcript = []

    # ---- inbound audio ----

    async def on_media(self, payload: str):
        # Twilio streams our own audio back on the same leg on some carriers, and
        # a speakerphone echoes it acoustically. With no echo cancellation, the
        # only reliable fix is not to listen while talking.
        if not BARGE_IN and (self.speaking or time.time() - self.quiet_since < ECHO_GUARD_S):
            return

        pcm = audioop.ulaw2lin(base64.b64decode(payload), 2)
        rms = audioop.rms(pcm, 2)

        if rms > SPEECH_RMS:
            self.speech_run += 1
            self.silence_run = 0
        else:
            self.silence_run += 1
            self.speech_run = 0

        if not self.in_speech:
            if self.speech_run >= SPEECH_FRAMES:
                self.in_speech = True
                self.frames = []
                if self.speaking:
                    await self.barge_in()
            return

        self.frames.append(pcm)
        if self.silence_run >= SILENCE_FRAMES or len(self.frames) >= MAX_UTTERANCE_FRAMES:
            self.in_speech = False
            utterance = b"".join(self.frames)
            self.frames = []
            if len(utterance) > TWILIO_RATE:  # ignore blips under ~0.5s
                self.epoch += 1     # stops playback; the old turn still drains
                self.turn = asyncio.create_task(self.respond(utterance))

    async def barge_in(self):
        """Caller started talking over us: stop playback and abandon the turn."""
        self.epoch += 1             # every in-flight speak() sees a stale epoch and stops
        self.speaking = False
        await self.ws.send_json({"event": "clear", "streamSid": self.stream_sid})

    # ---- the reply path ----

    async def respond(self, utterance: bytes):
        text = await self.transcribe(utterance)
        if not text.strip():
            return
        if is_hallucination(text):
            print(f"[skip] whisper silence artifact: {text!r}", flush=True)
            return
        self.transcript.append({"role": "callee", "text": text})
        print(f"[them] {text}", flush=True)
        await self.say_turn(text)

    async def collect(self, prompt: str) -> str:
        """Run a turn and return its text without speaking it."""
        out = []
        async for delta in self.claude.ask(prompt):
            out.append(delta)
        return "".join(out).strip()

    async def say_turn(self, prompt: str):
        """Run a turn and speak it sentence by sentence, recording both sides.

        The response is always read to the end; a superseded turn simply stops
        producing audio rather than abandoning the stream mid-response.
        """
        epoch = self.epoch
        buffer, spoken = "", []
        async for delta in self.claude.ask(prompt):
            buffer += delta
            if epoch != self.epoch:
                continue            # superseded: keep draining, stop speaking
            parts = SENTENCE_END.split(buffer)
            while len(parts) > 1:
                sentence, parts = parts[0], parts[1:]
                if sentence.strip():
                    spoken.append(sentence.strip())
                    await self.speak(sentence.strip(), epoch)
                buffer = " ".join(parts)
        if buffer.strip() and epoch == self.epoch:
            spoken.append(buffer.strip())
            await self.speak(buffer.strip(), epoch)
        if spoken:
            reply = " ".join(spoken)
            self.transcript.append({"role": "assistant", "text": reply})
            print(f"[claude] {reply}", flush=True)

    async def report(self):
        """Persist the transcript and push a summary to Discord."""
        if not self.transcript:
            return
        summary = ""
        try:
            summary = await self.collect(SUMMARY_PROMPT)
        except Exception as e:
            print(f"summary failed: {e}", flush=True)

        CALLS_DIR.mkdir(exist_ok=True)
        path = CALLS_DIR / f"{self.call_sid or self.stream_sid}.json"
        path.write_text(json.dumps({
            "call_sid": self.call_sid,
            "stream_sid": self.stream_sid,
            "goal": self.goal,
            "mode": "realtime",
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
            "turns": self.transcript,
        }, indent=2, ensure_ascii=False))

        header = f"Call ended ({'outbound' if self.goal else 'inbound'})"
        if self.goal:
            header += f"\nGoal: {self.goal}"
        body = summary or "(summary unavailable)"
        project, session = self.reporter
        project = project or os.environ.get("CC_DEFAULT_PROJECT", "")
        session = session or os.environ.get("CC_DEFAULT_SESSION", "")
        cmd = [CC_CONNECT_PATH, "send", "--stdin"]
        if project:
            cmd += ["-p", project]
        if session:
            cmd += ["-s", session]
        try:
            subprocess.run(cmd,
                           input=f"{header}\n\n{body}\n\nTranscript: {path}".encode(),
                           timeout=30, check=True, capture_output=True)
        except Exception as e:
            print(f"notify failed: {e}", flush=True)

    async def transcribe(self, pcm: bytes) -> str:
        form = aiohttp.FormData()
        form.add_field("file", pcm_to_wav(pcm), filename="audio.wav", content_type="audio/wav")
        form.add_field("model", "whisper-1")
        form.add_field("language", "en")
        async with self.http.post(
            f"{OPENAI_URL}/audio/transcriptions",
            data=form,
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
        ) as r:
            if r.status != 200:
                print(f"stt error {r.status}: {(await r.text())[:200]}", flush=True)
                return ""
            return (await r.json()).get("text", "")

    async def speak(self, text: str, epoch: int = None):
        """Synthesize one sentence and stream it back as u-law frames.

        Aborts if the epoch moved on (barge-in or a newer turn), and holds a
        lock so two sentences can never interleave on the socket.
        """
        if epoch is None:
            epoch = self.epoch
        if epoch != self.epoch:
            return
        async with self.http.post(
            f"{OPENAI_URL}/audio/speech",
            json={"model": "tts-1", "voice": "alloy", "input": text, "response_format": "pcm"},
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
        ) as r:
            if r.status != 200:
                print(f"tts error {r.status}: {(await r.text())[:200]}", flush=True)
                return
            pcm24 = await r.read()

        pcm8, _ = audioop.ratecv(pcm24, 2, 1, TTS_RATE, TWILIO_RATE, None)
        ulaw = audioop.lin2ulaw(pcm8, 2)

        async with self.play_lock:
            if epoch != self.epoch:
                return
            self.speaking = True
            # Pace at real time so a barge-in can cut in mid-sentence.
            for i in range(0, len(ulaw), FRAME_BYTES):
                if epoch != self.epoch:
                    break
                await self.ws.send_json({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(ulaw[i:i + FRAME_BYTES]).decode()},
                })
                await asyncio.sleep(0.02)
            self.speaking = False
            self.quiet_since = time.time()


async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async with aiohttp.ClientSession() as http:
        call = Call(ws, http)
        started = time.time()
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            event = data.get("event")
            if event == "start":
                start = data["start"]
                call.stream_sid = start["streamSid"]
                call.call_sid = start.get("callSid")
                params = start.get("customParameters") or {}
                call.goal = params.get("goal")
                call.reporter = (params.get("project", ""), params.get("session", ""))
                await call.claude.start()
                print(f"[call] stream {call.stream_sid} started"
                      f"{' (outbound)' if call.goal else ''}", flush=True)
                # As a task, not awaited: the loop must keep reading media so
                # VAD can interrupt the opening, and so frames don't pile up.
                if call.goal:
                    call.turn = asyncio.create_task(
                        call.say_turn(OUTBOUND_OPENING.format(goal=call.goal)))
                else:
                    call.turn = asyncio.create_task(
                        call.speak("Hi, it's Claude. What's up?"))
            elif event == "media":
                await call.on_media(data["media"]["payload"])
            elif event == "stop":
                break
        call.epoch += 1             # silence anything still speaking
        await call.report()
        await call.claude.close()
        print(f"[call] ended after {time.time() - started:.0f}s, "
              f"{len(call.transcript)} turns", flush=True)
    return ws


async def proxy(request):
    """Everything this service doesn't own belongs to bot.py (outbound calls, SMS, pages).

    One ngrok tunnel means one public port, so this process fronts both.
    """
    body = await request.read()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length")}
    url = f"http://127.0.0.1:{FLASK_PORT}{request.rel_url}"
    async with aiohttp.ClientSession() as s:
        async with s.request(request.method, url, data=body, headers=headers,
                             allow_redirects=False) as r:
            data = await r.read()
            skip = ("content-length", "transfer-encoding", "content-encoding")
            out = {k: v for k, v in r.headers.items() if k.lower() not in skip}
            return web.Response(status=r.status, body=data, headers=out)


async def start_recording(call_sid: str):
    """Record the call so it can be listened to afterwards, not just read."""
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    user = os.environ.get("TWILIO_API_KEY") or sid
    pw = os.environ.get("TWILIO_API_SECRET") or os.environ.get("TWILIO_AUTH_TOKEN")
    if not (sid and user and pw and call_sid):
        return
    auth = aiohttp.BasicAuth(user, pw)
    async with aiohttp.ClientSession(auth=auth) as http:
        async with http.post(f"{TWILIO_API}/{sid}/Calls/{call_sid}/Recordings.json") as r:
            if r.status not in (200, 201):
                print(f"recording failed {r.status}: {(await r.text())[:150]}", flush=True)


async def live(request):
    """TwiML that hands the call to the media stream.

    An outbound call passes ?goal=... ; it reaches the socket as a Stream
    <Parameter>, which is how Twilio carries per-call data into the stream.
    """
    if RECORD_CALLS:
        form = await request.post()
        call_sid = form.get("CallSid")
        if call_sid:
            asyncio.create_task(start_recording(call_sid))

    host = request.headers.get("X-Forwarded-Host") or request.host
    params = "".join(
        f"<Parameter name={quoteattr(k)} value={quoteattr(v)}/>"
        for k in ("goal", "project", "session")
        for v in [request.query.get(k, "")] if v
    )
    param = params
    return web.Response(
        text=f'<?xml version="1.0" encoding="UTF-8"?>'
             f'<Response><Connect><Stream url="wss://{host}/ws">{param}</Stream></Connect></Response>',
        content_type="text/xml",
    )


def main():
    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    app.router.add_route("*", "/live", live)
    app.router.add_route("*", "/{tail:.*}", proxy)
    print(f"realtime voice loop on :{PORT}", flush=True)
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    main()
