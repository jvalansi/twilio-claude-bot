#!/usr/bin/env python3
import os
import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, request, jsonify
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()

CLAUDE_PATH = "/home/ubuntu/.local/bin/claude"
CC_CONNECT_PATH = os.environ.get("CC_CONNECT_PATH", "/usr/lib/node_modules/cc-connect/bin/cc-connect")
CALLS_DIR = Path(__file__).parent / "calls"

account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
client = Client(account_sid, auth_token)

app = Flask(__name__)

# Store Claude session ID per call for conversation continuity
call_sessions = {}

# Store Claude session ID per SMS sender phone number
sms_sessions = {}

# Store call context (purpose/instructions) for outbound calls
call_contexts = {}

# Store transcript turns per outbound call: [{"role": ..., "text": ...}]
call_transcripts = {}

# Store the number dialed per outbound call, for the completion report
call_targets = {}


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


def record_turn(call_sid: str, role: str, text: str) -> None:
    """Append a turn to an outbound call's transcript (no-op for inbound calls)."""
    if call_sid in call_transcripts:
        call_transcripts[call_sid].append({"role": role, "text": text})


def format_transcript(turns: list) -> str:
    """Render transcript turns as readable dialogue."""
    labels = {"assistant": "Claude", "callee": "Them"}
    return "\n".join(f"{labels.get(t['role'], t['role'])}: {t['text']}" for t in turns)


def notify(message: str) -> None:
    """Send a message to the active cc-connect session (Discord)."""
    try:
        subprocess.run(
            [CC_CONNECT_PATH, "send", "--stdin"],
            input=message.encode(),
            timeout=30,
            check=True,
            capture_output=True,
        )
    except Exception as e:
        app.logger.error(f"notify failed: {e}")


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


@app.route("/call", methods=["POST"])
def initiate_call():
    """Initiate an outbound call with optional context for Claude.

    JSON body:
        to      (str, required): E.164 phone number to call, e.g. "+14086182819"
        context (str, optional): Instructions for Claude — what to say/do on the call.
    """
    data = request.get_json() or {}
    to = data.get("to")
    context = data.get("context", "")

    if not to:
        return jsonify({"error": "Missing 'to' phone number"}), 400

    from_number = os.environ.get("TWILIO_PHONE_NUMBER")
    if not from_number:
        return jsonify({"error": "TWILIO_PHONE_NUMBER not configured"}), 500

    base_url = os.environ.get("BASE_URL", request.host_url.rstrip("/"))

    call = client.calls.create(
        to=to,
        from_=from_number,
        url=f"{base_url}/voice",
        status_callback=f"{base_url}/status",
        status_callback_method="POST",
    )

    if context:
        call_contexts[call.sid] = context
        call_transcripts[call.sid] = []
        call_targets[call.sid] = to

    app.logger.info(f"[{call.sid}] Outbound call initiated to {to}")
    return jsonify({"call_sid": call.sid, "status": call.status})


@app.route("/voice", methods=["POST"])
def voice():
    """Entry point when a call connects (inbound or outbound)."""
    call_sid = request.form.get("CallSid")
    context = call_contexts.get(call_sid)

    if context:
        # Outbound call: prime Claude with context and get opening line
        app.logger.info(f"[{call_sid}] Outbound call connected, context: {context[:80]}")
        prompt = (
            f"You are making a phone call on behalf of the user. Your goal: {context}. "
            "Open by stating that you are an AI assistant calling on behalf of the user, "
            "then state your purpose in one or two sentences. "
            "Be polite and concise — you are speaking aloud on a phone call."
        )
        try:
            reply, session_id = asyncio.run(ask_claude(prompt))
            call_sessions[call_sid] = session_id
        except Exception as e:
            app.logger.error(f"[{call_sid}] Claude error: {e}")
            reply = ("Hello, this is an AI assistant calling on behalf of my user. "
                     "Could I speak with someone who can help me?")
        record_turn(call_sid, "assistant", reply)
        return twiml_listen(reply)

    # Inbound call: default greeting
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
    record_turn(call_sid, "callee", speech)

    session_id = call_sessions.get(call_sid)
    try:
        reply, new_session_id = asyncio.run(ask_claude(speech, session_id))
        call_sessions[call_sid] = new_session_id
    except Exception as e:
        app.logger.error(f"[{call_sid}] Claude error: {e}")
        return twiml_listen("Sorry, something went wrong. Please try again.")

    app.logger.info(f"[{call_sid}] Claude: {reply[:80]}...")
    record_turn(call_sid, "assistant", reply)

    # Twilio TTS works best with shorter chunks — truncate at 1000 chars if needed
    if len(reply) > 1000:
        reply = reply[:1000] + ". I have more to say, but let's continue from here."

    return twiml_listen(reply)


def report_outbound_call(call_sid: str, call_status: str) -> None:
    """Persist the transcript of a finished outbound call and report it to Discord."""
    turns = call_transcripts.pop(call_sid, [])
    goal = call_contexts.get(call_sid, "")
    to = call_targets.pop(call_sid, "unknown")
    session_id = call_sessions.get(call_sid)

    summary = ""
    if turns and session_id:
        try:
            summary, _ = asyncio.run(ask_claude(
                "The call just ended. In 2-3 short lines, report the outcome to the user: "
                "was the goal achieved, what was agreed (dates, times, names, amounts), "
                "and what follow-up is needed. No preamble.",
                session_id,
            ))
        except Exception as e:
            app.logger.error(f"[{call_sid}] summary failed: {e}")

    CALLS_DIR.mkdir(exist_ok=True)
    record = {
        "call_sid": call_sid,
        "to": to,
        "goal": goal,
        "status": call_status,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "turns": turns,
    }
    path = CALLS_DIR / f"{call_sid}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False))

    header = f"Call to {to} ended ({call_status})\nGoal: {goal}"
    if not turns:
        notify(f"{header}\n\nNobody spoke — no transcript.")
        return
    body = summary or "(summary unavailable)"
    notify(f"{header}\n\n{body}\n\nTranscript: {path}")


@app.route("/status", methods=["POST"])
def status():
    """Called by Twilio when the call ends — report outbound calls, clean up the session."""
    call_sid = request.form.get("CallSid")
    call_status = request.form.get("CallStatus")
    if call_status in ("completed", "failed", "busy", "no-answer"):
        if call_sid in call_transcripts:
            report_outbound_call(call_sid, call_status)
        call_sessions.pop(call_sid, None)
        call_contexts.pop(call_sid, None)
        app.logger.info(f"[{call_sid}] Call ended ({call_status}), session cleared")
    return "", 204


@app.route("/opt-in")
def opt_in():
    phone = os.environ.get("TWILIO_PHONE_NUMBER", "our number")
    return f"""<html><body>
<h1>SMS Opt-In</h1>
<p>To chat with the AI assistant, text <strong>START</strong> to <strong>{phone}</strong>.</p>
<p>By texting START you consent to receive AI-generated SMS replies. Message frequency varies (one reply per message sent).
Message and data rates may apply. Reply <strong>STOP</strong> to unsubscribe at any time.
Reply <strong>HELP</strong> for help.</p>
<p><a href="/privacy">Privacy Policy</a> | <a href="/terms">Terms of Service</a></p>
</body></html>"""


@app.route("/privacy")
def privacy():
    return """<html><body>
<h1>Privacy Policy</h1>
<p><strong>Service:</strong> Personal AI assistant SMS chatbot.</p>
<p><strong>Data collected:</strong> Phone number and message content, used solely to generate a reply.
No messages are stored permanently after a reply is sent. No data is sold or shared with third parties.</p>
<p><strong>Opt-out:</strong> Reply STOP at any time to stop receiving messages.</p>
<p><strong>Contact:</strong> Reply HELP for assistance.</p>
<p><a href="/terms">Terms of Service</a></p>
</body></html>"""


@app.route("/terms")
def terms():
    return """<html><body>
<h1>Terms of Service</h1>
<p><strong>Service:</strong> Personal AI assistant SMS chatbot for private use.</p>
<p><strong>Consent:</strong> By texting this number you consent to receive AI-generated SMS replies.</p>
<p><strong>Message frequency:</strong> One reply per message you send.</p>
<p><strong>Rates:</strong> Message and data rates may apply.</p>
<p><strong>Opt-out:</strong> Reply STOP to unsubscribe at any time. Reply HELP for help.</p>
<p><a href="/privacy">Privacy Policy</a></p>
</body></html>"""


@app.route("/sms", methods=["POST"])
def sms():
    """Handle incoming SMS messages."""
    from_number = request.form.get("From", "")
    body = request.form.get("Body", "").strip()

    app.logger.info(f"[SMS {from_number}] User: {body}")

    session_id = sms_sessions.get(from_number)
    try:
        reply, new_session_id = asyncio.run(ask_claude(body, session_id))
        sms_sessions[from_number] = new_session_id
    except Exception as e:
        app.logger.error(f"[SMS {from_number}] Claude error: {e}")
        reply = "Sorry, something went wrong. Please try again."

    app.logger.info(f"[SMS {from_number}] Claude: {reply[:80]}...")

    # SMS has a practical limit; truncate if needed
    if len(reply) > 1600:
        reply = reply[:1597] + "..."

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Twilio Claude bot listening on port {port}...")
    app.run(host="0.0.0.0", port=port)
