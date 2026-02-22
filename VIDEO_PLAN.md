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

## Approaches

Three realistic paths to adding video, each with different trade-offs.

---

### Approach 1: Twilio Video

Replace the existing TwiML voice flow with Twilio Video Rooms. Build a minimal browser frontend using the Twilio Video JS SDK. The server mints Access Tokens (JWTs) for rooms. Audio must be handled manually — Twilio Video has no built-in STT, so you pipe audio to Deepgram or similar.

**Stack:** Twilio Video JS SDK (browser) + Twilio Video REST API (room/token) + Deepgram (STT) + ElevenLabs/Google TTS

**Pros:**
- Stays within the existing Twilio relationship (one vendor)
- Well-documented

**Cons:**
- No server-side bot participant SDK — the bot can't join a room as a first-class participant; audio must be routed through the browser
- More frontend work than Daily.co
- No prebuilt UI

---

### Approach 2: Daily.co

Use Daily.co as the WebRTC infrastructure. Daily has a `daily-python` SDK that lets a server process join a room as a full participant — it receives raw PCM audio from other participants and can push audio back. No virtual devices, no UI automation.

**Stack:** Daily.co room (browser) + `daily-python` (server bot participant) + Deepgram (STT) + ElevenLabs/Google TTS

**Pros:**
- Server-side bot SDK — cleanest integration, bot joins the call like a real participant
- Prebuilt browser UI available (embed with one line of JS)
- Raw audio access on the server makes STT straightforward
- Generous free tier

**Cons:**
- New vendor (not Twilio)
- `daily-python` SDK is less mature than Twilio's tooling

---

### Approach 3: Desktop App via Virtual Display (Xvfb)

Run an existing video call app (Zoom, Telegram, etc.) on the server using a virtual display (`Xvfb`), virtual camera (`v4l2loopback`), and virtual audio (PulseAudio). Automate the UI with `xdotool`/`pyautogui` to join calls and route audio through the STT/TTS pipeline.

**Stack:** Xvfb + v4l2loopback + PulseAudio + Zoom or Telegram desktop app + UI automation

**Pros:**
- Works with any existing video platform — users call you on Zoom/Telegram like a normal person
- No custom frontend to build

**Cons:**
- UI automation is fragile — app updates break it
- Against ToS for most platforms (bot impersonating a human participant)
- Significant system-level setup (virtual display, camera, audio devices)
- High operational complexity

---

## Comparison

| | Twilio Video | Daily.co | Xvfb + Desktop App |
|---|---|---|---|
| Server-side bot SDK | No | Yes (`daily-python`) | N/A (UI automation) |
| Prebuilt browser UI | No | Yes | N/A |
| Works with existing apps (Zoom etc.) | No | No | Yes |
| Custom frontend required | Yes | Minimal | No |
| Virtual devices needed | No | No | Yes |
| ToS risk | Low | Low | High |
| Operational complexity | Medium | Low | High |
| Recommended | — | Yes | No |

**Daily.co is the recommended path** — it has the cleanest server-side integration, the least frontend work, and avoids the fragility of the Xvfb approach.
