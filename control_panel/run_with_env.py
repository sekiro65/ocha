#!/usr/bin/env python3
"""Supervisor wrapper: load /config/settings.env into the environment, then exec.

    run_with_env.py [--require FILE] -- command args...

Re-reading the file on every (re)start is what lets the control panel apply
settings with a plain `supervisorctl restart`. With --require, exits 0 quietly
when FILE is missing (e.g. the Telegram session before first login).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from settings_file import parse  # noqa: E402

args = sys.argv[1:]
if args[:1] == ["--require"]:
    required, args = args[1], args[2:]
    if not os.path.exists(required):
        print(f"[run_with_env] {required} missing, not starting", flush=True)
        sys.exit(0)
if args[:1] == ["--"]:
    args = args[1:]

for key, val in parse().items():
    if val != "":
        os.environ[key] = val
# Explo and the Telegram bot use different names for the same values
for explo_name, bot_name in (("TELEGRAM_BOT_API_KEY", "EXPLO_API_KEY"), ("TELEGRAM_TARGET_USER_ID", "MUSIC_BOT_ID")):
    if os.environ.get(bot_name):
        os.environ[explo_name] = os.environ[bot_name]
os.execvp(args[0], args)
