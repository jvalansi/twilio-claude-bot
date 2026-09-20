#!/usr/bin/env python3
"""Place an outbound call.

Twilio (default) — Claude conducts a two-way conversation:

    ./call.py --to +14155551234 --goal "Book a table for 2 on Friday at 7pm under Jordan"
    ./call.py --to +14155551234 --goal "..." --wait

Sinch — a scripted one-way message, since its API has no mid-call speech-to-text:

    ./call.py --provider sinch --to +14155551234 --say "Your package arrived."

Without --wait Twilio calls return the SID immediately and the outcome is reported
to Discord when the call ends. With --wait it blocks and prints the summary.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import sinch

BOT_URL = "http://127.0.0.1:5002"
CALLS_DIR = Path(__file__).parent / "calls"
E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def place_call(to: str, goal: str) -> str:
    """POST to the bot's /call endpoint and return the call SID."""
    if not E164.match(to):
        raise ValueError(f"'{to}' is not an E.164 number (e.g. +14155551234)")
    if not goal.strip():
        raise ValueError("goal must not be empty")

    req = urllib.request.Request(
        f"{BOT_URL}/call",
        data=json.dumps({"to": to, "context": goal}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["call_sid"]


def wait_for_result(call_sid: str, timeout_s: int = 900) -> dict:
    """Poll for the transcript file the bot writes when the call ends."""
    path = CALLS_DIR / f"{call_sid}.json"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(5)
    raise TimeoutError(f"no result for {call_sid} after {timeout_s}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", required=True, help="E.164 number to call, e.g. +14155551234")
    ap.add_argument("--provider", choices=("twilio", "sinch"), default="twilio",
                    help="twilio: two-way conversation. sinch: scripted message only")
    ap.add_argument("--goal", help="Twilio only — what Claude should accomplish on the call")
    ap.add_argument("--say", help="Sinch only — the message to speak")
    ap.add_argument("--wait", action="store_true", help="Block until the call ends and print the outcome")
    ap.add_argument("--timeout", type=int, default=900, help="Seconds to wait with --wait (default 900)")
    args = ap.parse_args()

    if args.provider == "sinch":
        if not args.say:
            ap.error("--provider sinch requires --say (Sinch cannot hold a conversation)")
        if args.goal:
            ap.error("--goal is Twilio-only; use --say with Sinch")
        try:
            call_id = sinch.tts_call(args.to, args.say)
        except (ValueError, RuntimeError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"calling {args.to} via Sinch — {call_id}")
        return 0

    if not args.goal:
        ap.error("--goal is required for Twilio calls")
    if args.say:
        ap.error("--say is Sinch-only; use --goal with Twilio")

    try:
        call_sid = place_call(args.to, args.goal)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except urllib.error.URLError as e:
        print(f"error: cannot reach bot at {BOT_URL} ({e})", file=sys.stderr)
        return 1

    print(f"calling {args.to} — {call_sid}")
    if not args.wait:
        return 0

    try:
        result = wait_for_result(call_sid, args.timeout)
    except TimeoutError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"\nstatus: {result['status']}")
    print(result["summary"] or "(no summary)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
