#!/usr/bin/env python3
"""Checks for the outbound-call plumbing. Run: python test_call.py"""
import sys
from unittest import mock

sys.modules.setdefault("dotenv", mock.MagicMock())
import bot
import call

# format_transcript labels both sides and preserves order
turns = [
    {"role": "assistant", "text": "Hi, I'm an AI assistant calling for Jordan."},
    {"role": "callee", "text": "Sure, what do you need?"},
]
assert bot.format_transcript(turns) == (
    "Claude: Hi, I'm an AI assistant calling for Jordan.\nThem: Sure, what do you need?"
)

# record_turn only records for calls registered as outbound
bot.call_transcripts.clear()
bot.record_turn("CA_inbound", "assistant", "ignored")
assert "CA_inbound" not in bot.call_transcripts, "inbound calls must not be recorded"
bot.call_transcripts["CA_out"] = []
bot.record_turn("CA_out", "callee", "hello")
assert bot.call_transcripts["CA_out"] == [{"role": "callee", "text": "hello"}]

# E.164 validation rejects the formats people actually type
for bad in ["4155551234", "+1 415 555 1234", "(415) 555-1234", "+0155512345", ""]:
    try:
        call.place_call(bad, "goal")
    except ValueError:
        pass
    else:
        raise AssertionError(f"{bad!r} should have been rejected")

# a valid number gets past validation and reaches the HTTP layer
with mock.patch("urllib.request.urlopen", side_effect=AssertionError("reached HTTP")):
    try:
        call.place_call("+14155551234", "book a table")
    except AssertionError as e:
        assert "reached HTTP" in str(e)

# empty goal is rejected before dialing
try:
    call.place_call("+14155551234", "   ")
except ValueError:
    pass
else:
    raise AssertionError("empty goal should have been rejected")

# --- Sinch provider ---
import os
import sinch

# payload matches the documented ttsCallout shape
payload = sinch.build_tts_payload("+14155551234", "Your table is confirmed.", "+18662593587")
assert payload["method"] == "ttsCallout"
assert payload["ttsCallout"]["destination"] == {"type": "number", "endpoint": "+14155551234"}
assert payload["ttsCallout"]["cli"] == "+18662593587"
assert payload["ttsCallout"]["locale"] == "en-US"
assert payload["ttsCallout"]["text"] == "Your table is confirmed."

# same validation as the Twilio path
for bad in ["4155551234", "+1 415 555 1234", ""]:
    try:
        sinch.build_tts_payload(bad, "hi", "+18662593587")
    except ValueError:
        pass
    else:
        raise AssertionError(f"{bad!r} should have been rejected")
try:
    sinch.build_tts_payload("+14155551234", "   ", "+18662593587")
except ValueError:
    pass
else:
    raise AssertionError("empty text should have been rejected")

# basic auth header is base64(key:secret)
assert sinch._auth_header("key", "secret") == "Basic a2V5OnNlY3JldA=="

# missing credentials fail before any network call
with mock.patch.dict(os.environ, {"SINCH_KEY": "", "SINCH_SECRET": "", "SINCH_NUMBER": ""}, clear=False):
    try:
        sinch.tts_call("+14155551234", "hi")
    except RuntimeError as e:
        assert "SINCH_KEY" in str(e) and "SINCH_SECRET" in str(e) and "SINCH_NUMBER" in str(e)
    else:
        raise AssertionError("missing credentials should have been reported")

print("all checks passed")
