#!/usr/bin/env python3
"""Sinch Voice API — outbound text-to-speech calls.

Sinch has no equivalent of Twilio's <Gather input="speech">: its runMenu voice
input is numerical only and transcription is delivered after the call, so a
turn-by-turn conversation is not possible on this API. What works today is a
scripted call: dial a number, speak a message, hang up.

Credentials (env, loaded from .env):
    SINCH_KEY, SINCH_SECRET   application key/secret from the Sinch dashboard
    SINCH_NUMBER              verified CLI to call from, E.164
"""
import base64
import json
import os
import re
import urllib.error
import urllib.request

CALLOUT_URL = "https://calling.api.sinch.com/calling/v1/callouts"
E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def _auth_header(key: str, secret: str) -> str:
    token = base64.b64encode(f"{key}:{secret}".encode()).decode()
    return f"Basic {token}"


def build_tts_payload(to: str, text: str, cli: str, locale: str = "en-US") -> dict:
    """Build the ttsCallout body for POST /calling/v1/callouts."""
    if not E164.match(to):
        raise ValueError(f"'{to}' is not an E.164 number (e.g. +14155551234)")
    if not text.strip():
        raise ValueError("text must not be empty")
    return {
        "method": "ttsCallout",
        "ttsCallout": {
            "cli": cli,
            "destination": {"type": "number", "endpoint": to},
            "locale": locale,
            "text": text,
        },
    }


def tts_call(to: str, text: str, locale: str = "en-US") -> str:
    """Place a scripted text-to-speech call. Returns the Sinch call ID."""
    key = os.environ.get("SINCH_KEY")
    secret = os.environ.get("SINCH_SECRET")
    cli = os.environ.get("SINCH_NUMBER")
    missing = [n for n, v in (("SINCH_KEY", key), ("SINCH_SECRET", secret), ("SINCH_NUMBER", cli)) if not v]
    if missing:
        raise RuntimeError(f"missing env: {', '.join(missing)}")

    payload = build_tts_payload(to, text, cli, locale)
    req = urllib.request.Request(
        CALLOUT_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": _auth_header(key, secret)},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp).get("callId", "")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Sinch rejected the call ({e.code}): {e.read().decode()[:200]}") from e
