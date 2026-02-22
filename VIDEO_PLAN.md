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
