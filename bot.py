#!/usr/bin/env python3
import os
import asyncio
import json
from flask import Flask, request
from twilio.twiml.voice_response import VoiceResponse, Gather
from dotenv import load_dotenv

load_dotenv()

CLAUDE_PATH = "/home/ubuntu/.local/bin/claude"

app = Flask(__name__)

# Store Claude session ID per call for conversation continuity
call_sessions = {}


async def ask_claude(message: str, session_id: str = None) -> tuple[str, str]:
    """Send a message to the Claude CLI and return (reply, session_id)."""
    cmd = [CLAUDE_PATH, "-p", message, "--output-format", "json", "--dangerously-skip-permissions"]
    if session_id:
        cmd += ["--resume", session_id]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(stderr.decode().strip())

    data = json.loads(stdout.decode())
    reply = data.get("result", "").strip()
    new_session_id = data.get("session_id", session_id)
    return reply, new_session_id


def twiml_listen(say_text: str = None) -> str:
    """Build a TwiML response that optionally speaks text then listens for speech."""
    response = VoiceResponse()
    gather = Gather(input="speech", action="/gather", method="POST", speechTimeout="auto", language="en-US")
    if say_text:
        gather.say(say_text, voice="alice")
    response.append(gather)
    # If caller says nothing, loop back
    response.redirect("/voice", method="POST")
    return str(response)


@app.route("/voice", methods=["POST"])
def voice():
    """Entry point when someone calls the Twilio number."""
    call_sid = request.form.get("CallSid")
    app.logger.info(f"[{call_sid}] Incoming call")
    return twiml_listen("Hello! I'm Claude. How can I help you?")


@app.route("/gather", methods=["POST"])
def gather():
    """Called by Twilio after capturing the caller's speech."""
    call_sid = request.form.get("CallSid")
    speech = request.form.get("SpeechResult", "").strip()

    if not speech:
        return twiml_listen("Sorry, I didn't catch that. Please try again.")

    app.logger.info(f"[{call_sid}] Caller: {speech}")

    session_id = call_sessions.get(call_sid)
    try:
        reply, new_session_id = asyncio.run(ask_claude(speech, session_id))
        call_sessions[call_sid] = new_session_id
    except Exception as e:
        app.logger.error(f"[{call_sid}] Claude error: {e}")
        return twiml_listen("Sorry, something went wrong. Please try again.")

    app.logger.info(f"[{call_sid}] Claude: {reply[:80]}...")

    # Twilio TTS works best with shorter chunks — truncate at 1000 chars if needed
    if len(reply) > 1000:
        reply = reply[:1000] + ". I have more to say, but let's continue from here."

    return twiml_listen(reply)


@app.route("/status", methods=["POST"])
def status():
    """Called by Twilio when the call ends — clean up the session."""
    call_sid = request.form.get("CallSid")
    call_status = request.form.get("CallStatus")
    if call_status in ("completed", "failed", "busy", "no-answer"):
        call_sessions.pop(call_sid, None)
        app.logger.info(f"[{call_sid}] Call ended ({call_status}), session cleared")
    return "", 204


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Twilio Claude bot listening on port {port}...")
    app.run(host="0.0.0.0", port=port)
