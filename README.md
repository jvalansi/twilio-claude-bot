# twilio-claude-bot

A voice bot that lets you call a phone number and have a spoken conversation with Claude. Built with Twilio (call handling + STT/TTS) and the Claude CLI.

---

## How It Works

```
Caller → Twilio Phone Number → Webhook (Flask server) → Claude CLI → Twilio TTS → Caller
```

1. Caller dials your Twilio number
2. Twilio hits a webhook on your server with a `CallSid`
3. Server responds with TwiML instructing Twilio to listen for speech (`<Gather>`)
4. Twilio transcribes the caller's speech and posts it back to your server
5. Server sends the transcript to Claude, gets a response
6. Server responds with TwiML instructing Twilio to speak Claude's reply (`<Say>`)
7. Repeat from step 4 — the conversation continues in a loop

Conversation continuity is maintained per call using the `CallSid` as the session key, mapped to a Claude session ID (same pattern as the Slack and Discord bots).

---

## Architecture

### Components

| Component | Role |
|---|---|
| **Twilio** | Phone number, speech-to-text, text-to-speech |
| **Flask** | Lightweight webhook server to handle Twilio callbacks |
| **Claude CLI** | Processes transcribed speech and returns a response |
| **ngrok** (dev) | Exposes local server to the internet for Twilio webhooks |

### Key Design Decisions

- **Twilio's built-in STT/TTS**: Avoids integrating ElevenLabs or Whisper — simpler, fewer moving parts, good enough quality for a first version
- **`<Gather>` loop**: After every Claude response, Twilio re-listens for input. The call naturally ends when the caller hangs up or says nothing
- **Session continuity via `CallSid`**: Each call gets its own Claude session, so Claude has full context of the conversation. Sessions are cleaned up when the call ends
- **Synchronous Flask**: Unlike the async Discord/Slack bots, Flask is synchronous here — acceptable since Twilio's webhook timeout is 15 seconds and Claude typically responds in 2-5s

### Latency

Callers will notice a 3-6 second pause while Claude responds. This is mitigated by:
- Playing a brief filler message ("Let me think about that...") while waiting (optional enhancement)
- Keeping system prompts short to reduce token overhead

---

## Project Structure

```
twilio-claude-bot/
├── README.md
├── bot.py              # Flask webhook server (main entry point)
├── requirements.txt    # Python dependencies
├── setup.sh            # Install as a systemd service (production)
└── .env                # TWILIO_AUTH_TOKEN (not committed)
```

---

## Prerequisites

- Python 3.9+
- A [Twilio account](https://www.twilio.com) with:
  - A phone number (~$1/month)
  - Your Account SID and Auth Token
- Claude CLI installed at `/home/ubuntu/.local/bin/claude`
- `ngrok` (for local development) or a server with a public IP (for production)

---

## Setup

### 1. Create conda environment

```bash
conda create -n twilio-claude-bot python=3.11 -y
/home/ubuntu/miniconda3/envs/twilio-claude-bot/bin/pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in your Twilio credentials
```

Required env vars:

| Variable | Description |
|---|---|
| `TWILIO_ACCOUNT_SID` | From Twilio console |
| `TWILIO_AUTH_TOKEN` | From Twilio console |

### 3. Set up ngrok

Install ngrok and add your auth token (get it from [dashboard.ngrok.com](https://dashboard.ngrok.com/get-started/your-authtoken)):

```bash
# Install
curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | sudo tee /etc/apt/sources.list.d/ngrok.list
sudo apt-get update && sudo apt-get install -y ngrok

# Authenticate
ngrok config add-authtoken <your-auth-token>
```

### 4. Run locally

```bash
# Start the Flask server
/home/ubuntu/miniconda3/envs/twilio-claude-bot/bin/python bot.py

# In another terminal, expose it via ngrok
ngrok http 5000
```

### 5. Configure Twilio webhook

In the [Twilio console](https://console.twilio.com), go to your phone number settings and set:

- **Voice webhook (HTTP POST):** `https://<your-ngrok-url>/voice`

### 6. Call your number

Dial your Twilio number and start talking to Claude.

---

## Production Deployment

Use `setup.sh` to install as a systemd service (same pattern as the Slack bot):

```bash
chmod +x setup.sh
sudo ./setup.sh
```

In production, replace ngrok with a real public URL (e.g., the server's IP/domain) and update the Twilio webhook accordingly.

---

## Dependencies

```
flask
twilio
python-dotenv
```

---

## Roadmap to Profitability

**Current state:** Personal demo — one phone number, no billing.

| Milestone | Description | Expected Monthly ROI |
|---|---|---|
| **Stripe metered billing** | Charge per call or per minute | — |
| **Customer onboarding** | Sign-up page, provision phone number, connect to Claude | $50–100/mo |
| **Voice quality upgrade** | ElevenLabs TTS + Whisper STT for natural feel | higher conversion |
| **Outbound campaigns** | Let customers trigger outbound calls from Claude | enterprise tier |
| **White-label** | Businesses embed voice AI on their own number | $100–500/mo/client |

**Next step (Notion task):** Add per-call billing and customer onboarding flow — ROI hypothesis $50/mo from 5–10 customers at $0.10/call.

---

## Limitations & Future Improvements

- **Latency**: 3-6s pause per turn is noticeable. Could add a filler phrase ("One moment...") while Claude thinks
- **TTS quality**: Twilio's built-in voices are functional but robotic. Upgrade to ElevenLabs or OpenAI TTS for a more natural feel
- **STT accuracy**: Twilio's speech recognition is decent but not state-of-the-art. Upgrading to Whisper would improve accuracy
- **Outbound calls**: Currently inbound only. Twilio supports outbound calls if you want Claude to initiate
- **Session persistence**: Sessions are in-memory only — a server restart clears all active call sessions (acceptable since calls are short-lived)
- **Video calls**: See [VIDEO_PLAN.md](VIDEO_PLAN.md) for a detailed plan on evolving this into a full video call experience with real-time audio, video frame capture, and an optional talking avatar
