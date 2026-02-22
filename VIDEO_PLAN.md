# Video Call Feature Plan

This document outlines what it would take to evolve the current voice-only bot into a video call experience with Claude.

---

## Current Architecture (Voice)

The flow is simple and stateless per turn:

```
Phone call → Twilio STT → /gather → Claude CLI → Twilio TTS → caller hears response
```

Each turn is: speech captured → text in → text out → speech played back. No real-time streaming; each leg is a discrete HTTP round-trip.

---

## What Adding Video Requires

### 1. Web Frontend (New — significant work)

The phone call entry point goes away. You need a browser-based UI that:
- Opens a WebRTC session (camera + mic)
- Connects to a Twilio Video Room
- Renders the remote video (even if it's just a static avatar for Claude)

This is a full mini web app — HTML/JS, likely ~200–400 lines. Twilio provides client SDKs for this but you wire it all together.

### 2. Replace TwiML Voice with Twilio Video Rooms (New — moderate work)

Instead of `client.calls.create(...)`, you'd:
- Create a Twilio Video Room via the REST API
- Issue an Access Token (JWT) scoped to that room for the browser client
- Add a new `/token` endpoint to `bot.py` that mints and returns these tokens

### 3. Real-time Audio Pipeline (New — hard, most complex piece)

The current bot relies on Twilio's built-in STT (`<Gather input="speech">`). Twilio Video doesn't have that — you own the audio stream. You'd need:

- **Audio capture from the WebRTC stream** — Twilio's Video JS SDK gives you a `LocalAudioTrack`; on the server side you'd subscribe to the remote audio track
- **STT** — pipe the audio to e.g. Deepgram, Google STT, or Twilio's own Media Streams + transcription. This is real-time streaming, not a single HTTP call
- **VAD (Voice Activity Detection)** — detect when the user stops speaking so you know when to send to Claude. Without this you're either constantly transcribing noise or adding awkward fixed pauses

### 4. Video Frame Capture Pipeline (New — moderate work)

This is the part that makes it actually "video" vs just video-call-shaped voice:

- Periodically capture frames from the user's video track (e.g., every 2–5 seconds, or on a user pause)
- Encode as JPEG/PNG
- Pass to Claude alongside the transcribed speech
- This is where `claude -p "..." --image path/to/frame.jpg` or equivalent API calls come in

### 5. TTS + Avatar Video (New — moderate to hard)

Claude's response is text. To play it back as video you have two options:

| Option | What it is | Difficulty |
|---|---|---|
| **Audio only** | TTS (e.g. ElevenLabs, Google TTS) played in the browser; Claude has no video presence | Easy |
| **Talking avatar** | Services like HeyGen, D-ID, or Tavus generate a lip-synced video of a face speaking the TTS | Hard + costly |

### 6. Latency Management (New — ongoing tuning)

The current bot has inherent latency (one full round-trip per turn) but it's acceptable for phone calls. Video calls feel more synchronous. You'd need:
- Streaming TTS so audio starts playing before the full response is generated
- Possibly streaming Claude responses (if using the API directly instead of the CLI)
- Visual "thinking" indicator in the UI while Claude processes

---

## Implementation Stages

Each stage builds on the previous and is independently testable.

---

### Stage 1: Web Frontend + Twilio Video Room

**What you build:**
- A minimal HTML/JS page that opens the user's camera and mic via the Twilio Video JS SDK and joins a Video Room
- A `/token` endpoint in `bot.py` that mints a Twilio Access Token (JWT) scoped to a room
- A `/room` endpoint (or static page) that serves the frontend

**Key files changed:** `bot.py` (new endpoints), `static/index.html` (new)

**Outcome:** You can open a browser, click "Start", and see your own camera feed in a Twilio Video Room. No Claude yet — just a working WebRTC session. Proves the plumbing works before any AI integration.

---

### Stage 2: Real-time Audio → STT

**What you build:**
- In the browser: capture the local audio track and stream it to the server (via WebSocket or Twilio Media Streams)
- On the server: pipe the audio to a streaming STT service (Deepgram recommended — clean Python SDK, low latency)
- VAD: use `webrtcvad` (Python) or Deepgram's built-in endpointing to detect end-of-utterance and emit a complete transcript

**Key files changed:** `bot.py` (WebSocket handler, STT integration), `static/index.html` (audio streaming)

**Outcome:** You can speak into the browser and see your words transcribed in real time in the server logs. No Claude yet — just end-to-end speech-to-text. This is the hardest stage; once it works the rest is straightforward.

---

### Stage 3: Transcript → Claude → TTS Playback

**What you build:**
- Wire the completed transcript (from Stage 2) into Claude (via the Claude API with streaming, replacing the CLI)
- Feed Claude's text response into a TTS service (ElevenLabs or Google TTS) and stream the audio back to the browser
- The browser plays the audio through a standard `<audio>` element or Web Audio API

**Key files changed:** `bot.py` (Claude API call, TTS integration), `static/index.html` (audio playback)

**Outcome:** A fully working voice conversation through the browser — you speak, Claude responds in a synthesized voice. Functionally equivalent to the current phone bot, but browser-based with higher quality STT/TTS. This is a shippable v1 of the video bot (audio-only mode).

---

### Stage 4: Video Frame Capture → Claude Vision

**What you build:**
- In the browser: periodically capture a frame from the local video track (e.g. every 3 seconds or on speech end) using a canvas, encode as JPEG, and send to the server alongside the audio/transcript
- On the server: attach the frame to the Claude API call as an image input

**Key files changed:** `bot.py` (image handling in Claude request), `static/index.html` (frame capture + upload)

**Outcome:** Claude can now see you. If you hold up an object or share your screen, Claude will reference it in its response. The conversation becomes genuinely multimodal — speech + vision, not just speech.

---

### Stage 5: Talking Avatar (Optional)

**What you build:**
- Integrate a talking avatar API (HeyGen, D-ID, or Tavus) that takes Claude's TTS audio and generates a lip-synced video of a face
- Stream or display the avatar video in the browser alongside (or instead of) the user's camera feed

**Key files changed:** `bot.py` (avatar API integration), `static/index.html` (video element for avatar)

**Outcome:** Claude has a face. The experience feels like a proper video call — the user sees a speaking avatar on one side, their own camera on the other. Higher cost and latency, but significantly more immersive.

---

### Stage 6: Latency & Polish

**What you build:**
- Streaming Claude responses (already supported if using the API directly) so TTS starts before the full reply is ready
- A "thinking" visual indicator in the UI while Claude is processing
- Graceful handling of interruptions (user speaks while Claude is responding)
- Session cleanup, error handling, reconnection logic

**Outcome:** The experience feels fluid rather than turn-based. Conversations feel natural rather than like a call-and-response system with awkward pauses.

---

## Effort Summary

| Component | Effort | Notes |
|---|---|---|
| Web frontend (WebRTC) | ~1–2 days | Twilio Video JS SDK helps a lot |
| Twilio Video Room + token endpoint | ~0.5 days | Well-documented API |
| STT pipeline (real-time) | ~1–2 days | Deepgram has good streaming support |
| VAD | ~0.5–1 day | Libraries exist (e.g. `webrtcvad`) |
| Video frame capture → Claude | ~0.5–1 day | Straightforward once audio works |
| TTS playback (audio only) | ~0.5 days | Simple if skipping avatar |
| Talking avatar | ~2–3 days | New third-party integration |
| Latency tuning | ~1–2 days | Iterative |

**Total realistic estimate without avatar: ~4–6 days of focused work**
**With talking avatar: ~7–10 days**

The hardest single piece is the real-time audio pipeline (STT + VAD) — that's where the current architecture's simplicity completely breaks down.
