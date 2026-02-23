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

#### Avatar Service Comparison

These services render a talking avatar from text/audio — they are not video call platforms themselves. You still need Daily.co (or similar) to deliver the avatar video stream to the user. All three support custom avatars.

| | HeyGen | D-ID | Tavus |
|---|---|---|---|
| Real-time streaming | Yes | Yes | Yes |
| Latency | ~1–2s | ~2–3s | ~2s |
| Avatar quality | Excellent | Good | Excellent |
| Custom avatar: photo | No | Yes | No |
| Custom avatar: video | Yes (Instant/Studio) | Yes | Yes (Replica) |
| Voice cloning | Yes | Yes | Yes (auto from video) |
| Bring your own LLM | Yes | Yes | Limited |
| Full conversation loop managed | No | No (yes w/ Agents add-on) | Yes |
| Flexibility | High | High | Low |
| Cost | Mid | Mid | High |
| Recommended | Yes | — | — |

**HeyGen is the recommended avatar service** — best quality, lowest latency, real-time streaming API, flexible custom avatars, and lets you keep Claude as the brain. For a quick all-in-one solution with less control, Tavus CVI manages the full conversation loop internally.

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

---

## Implementation Plan: Daily.co

### Step 1: Account & Room Setup

1. Create a Daily.co account at daily.co
2. In the dashboard, create a new app and note your **API key**
3. Add `DAILY_API_KEY` to your `.env` file
4. Install dependencies:
   ```bash
   pip install daily-python deepgram-sdk anthropic elevenlabs python-dotenv
   ```

**Outcome:** You have a Daily API key and the required packages installed.

---

### Step 2: Room Creation Endpoint

Add a `/room` endpoint to `bot.py` that creates a Daily room on demand and returns the room URL to the browser.

```python
import requests

@app.route("/room", methods=["POST"])
def create_room():
    resp = requests.post(
        "https://api.daily.co/v1/rooms",
        headers={"Authorization": f"Bearer {os.getenv('DAILY_API_KEY')}"},
        json={"properties": {"enable_chat": False, "max_participants": 2}}
    )
    room = resp.json()
    return jsonify({"url": room["url"], "name": room["name"]})
```

**Outcome:** Calling `POST /room` creates a fresh Daily room and returns its URL.

---

### Step 3: Browser Frontend

Add a minimal `static/index.html` that embeds the Daily Prebuilt UI — no custom WebRTC code needed.

```html
<!DOCTYPE html>
<html>
<body>
  <button id="start">Call Claude</button>
  <div id="call" style="display:none; width:100%; height:600px;"></div>
  <script src="https://unpkg.com/@daily-co/daily-js"></script>
  <script>
    document.getElementById("start").onclick = async () => {
      const { url } = await fetch("/room", { method: "POST" }).then(r => r.json());
      document.getElementById("start").style.display = "none";
      document.getElementById("call").style.display = "block";
      const call = window.DailyIframe.createFrame(document.getElementById("call"));
      await call.join({ url });
    };
  </script>
</body>
</html>
```

Serve it from Flask:
```python
from flask import send_from_directory

@app.route("/")
def index():
    return send_from_directory("static", "index.html")
```

**Outcome:** Opening the page in a browser shows a "Call Claude" button. Clicking it opens a Daily video room with your camera and mic. Claude isn't there yet, but the room works.

---

### Step 4: Bot Joins the Room (daily-python)

When a room is created, the server also joins it as a bot participant using `daily-python`. The bot receives the user's raw audio frames.

```python
from daily import Daily, CallClient

def start_bot(room_url):
    Daily.init()
    client = CallClient()
    client.join(room_url, client_settings={
        "inputs": {
            "camera": False,       # bot has no camera (yet)
            "microphone": False    # bot will push audio manually
        }
    })
    client.set_audio_renderer(my_audio_callback, audio_source="remote")
```

The `my_audio_callback` function receives raw PCM audio frames from the user.

**Outcome:** When a room is created, the bot silently joins it server-side and starts receiving audio. The user sees a second (bot) participant in the room.

---

### Step 5: STT with Deepgram

Pipe the raw PCM frames from the bot's audio callback into Deepgram's streaming STT. Use Deepgram's built-in endpointing (VAD) so you get a final transcript when the user finishes speaking — no separate VAD library needed.

```python
from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions

dg = DeepgramClient(os.getenv("DEEPGRAM_API_KEY"))
dg_connection = dg.listen.live.v("1")
dg_connection.on(LiveTranscriptionEvents.Transcript, on_transcript)
dg_connection.start(LiveOptions(model="nova-2", endpointing=500))

def my_audio_callback(audio_frames, *args):
    dg_connection.send(audio_frames)

def on_transcript(self, result, **kwargs):
    transcript = result.channel.alternatives[0].transcript
    if result.is_final and transcript:
        handle_turn(transcript)
```

**Outcome:** When the user speaks, `handle_turn` is called with the transcribed text. End-to-end voice → text is working.

---

### Step 6: Claude API

Replace the Claude CLI with the Anthropic Python SDK for streaming responses. Maintain a conversation history list to preserve context across turns.

```python
import anthropic

client = anthropic.Anthropic()
history = []

def handle_turn(transcript):
    history.append({"role": "user", "content": transcript})
    response_text = ""
    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=history
    ) as stream:
        for text in stream.text_stream:
            response_text += text
            # optionally: pipe each chunk to TTS as it arrives
    history.append({"role": "assistant", "content": response_text})
    speak(response_text)
```

**Outcome:** Claude responds to the user's speech. Responses are contextual across the conversation.

---

### Step 7: TTS → Bot Speaks

Convert Claude's text response to audio and push it back into the Daily room through the bot participant so the user hears it.

```python
import elevenlabs

def speak(text):
    audio = elevenlabs.generate(text=text, voice="Rachel", model="eleven_monolingual_v1")
    # Push PCM audio back into the Daily room via the bot client
    # daily-python accepts raw PCM via client.send_audio()
    client.send_audio(audio)
```

**Outcome:** The full loop works — user speaks, Claude listens (via Deepgram), thinks, and responds in a synthesized voice through the call. This is a fully functional voice-over-video bot.

---

### Step 8: Video Frame Capture → Claude Vision (Optional)

Extend `handle_turn` to also accept a video frame captured by the browser at the moment the user finishes speaking. Send the frame to Claude as an image alongside the transcript.

In the browser, capture a frame when Deepgram fires its endpointing event (proxied via WebSocket):
```js
const canvas = document.createElement("canvas");
canvas.drawImage(videoElement, 0, 0);
const frame = canvas.toDataURL("image/jpeg", 0.7);
// send frame to server alongside transcript
```

On the server, include the image in the Claude API call:
```python
history.append({"role": "user", "content": [
    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": frame_b64}},
    {"type": "text", "text": transcript}
]})
```

**Outcome:** Claude can see the user. It will reference visible objects, expressions, or anything shown on camera in its responses.

---

### Step 9: Polish

- Add a "thinking" indicator in the browser while Claude processes (show/hide a spinner via WebSocket message from the server)
- Stream TTS as Claude responds chunk-by-chunk to reduce latency (ElevenLabs supports streaming)
- Clean up the Daily room when the user leaves (`client.leave()` on the `participant-left` event)
- Add `DEEPGRAM_API_KEY` and `ELEVENLABS_API_KEY` to `.env` and `setup.sh`

**Outcome:** A polished, low-latency video call experience with Claude.
