#!/usr/bin/env python3
"""Runs Explo playlists on the *_SCHEDULE / *_FLAGS pairs the Explo dashboard
writes (upstream does this with crond in docker/start.sh). One run at a time,
so playlists queue instead of hammering the downloader."""
import logging
import os
import queue
import shlex
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from settings_file import parse  # noqa: E402

EXPLO_BIN = os.environ.get("EXPLO_BIN", "/app/explo/explo")
EXPLO_ENV = os.environ.get("EXPLO_ENV", "/config/explo/.env")
LOG = os.path.join(os.environ.get("LOG_DIR", "/config/logs"), "scheduler.log")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(LOG), logging.StreamHandler(sys.stdout)])
log = logging.getLogger("scheduler")


def _field(spec, value, lo, hi):
    for part in spec.split(","):
        rng, _, step = part.partition("/")
        step = int(step or 1)
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = map(int, rng.split("-"))
        else:
            a = b = int(rng)
            if step > 1:
                b = hi
        if a <= value <= b and (value - a) % step == 0:
            return True
    return False


def cron_matches(expr, t):
    """Standard 5-field cron (minute hour day month weekday, Sunday=0 or 7)."""
    f = expr.split()
    if len(f) != 5:
        return False
    wd = (t.tm_wday + 1) % 7
    dom_ok, dow_ok = _field(f[2], t.tm_mday, 1, 31), (_field(f[4], wd, 0, 7) or (wd == 0 and _field(f[4], 7, 0, 7)))
    # cron rule: if both day fields are restricted, either may match
    day_ok = (dom_ok or dow_ok) if f[2] != "*" and f[4] != "*" else (dom_ok and dow_ok)
    return _field(f[0], t.tm_min, 0, 59) and _field(f[1], t.tm_hour, 0, 23) and _field(f[3], t.tm_mon, 1, 12) and day_ok


def jobs():
    env = parse(EXPLO_ENV)
    for key, sched in env.items():
        if key.endswith("_SCHEDULE") and sched:
            flags = env.get(key[:-len("_SCHEDULE")] + "_FLAGS", "")
            if flags:
                yield key[:-len("_SCHEDULE")], sched, flags


def worker(q):
    while True:
        name, flags = q.get()
        log.info("run %s: explo %s", name, flags)
        run_env = {**os.environ, **{k: v for k, v in parse().items() if v}}
        with open(os.path.join(os.path.dirname(LOG), "explo-runs.log"), "a") as out:
            rc = subprocess.call([EXPLO_BIN, "--config", EXPLO_ENV, *shlex.split(flags)],
                                 cwd=os.path.dirname(EXPLO_ENV), env=run_env, stdout=out, stderr=out)
        log.info("done %s (exit %s)", name, rc)


def main():
    q = queue.Queue()
    threading.Thread(target=worker, args=(q,), daemon=True).start()
    log.info("scheduler started (%s)", EXPLO_ENV)
    last = None
    while True:
        t = time.localtime()
        minute = (t.tm_year, t.tm_yday, t.tm_hour, t.tm_min)
        if minute != last:
            last = minute
            for name, sched, flags in jobs():
                if cron_matches(sched, t):
                    q.put((name, flags))
        time.sleep(5)


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        t = time.strptime("2026-10-27 04:00", "%Y-%m-%d %H:%M")  # Tuesday
        assert cron_matches("0 4 * * *", t)
        assert cron_matches("*/15 4 * * 2", t)
        assert not cron_matches("15 00 * * 2", t)
        assert cron_matches("0 4 27 * *", t) and not cron_matches("0 4 1 * *", t)
        assert cron_matches("0 4 1 * 2", t)  # dom OR dow when both set
        print("scheduler ok")
    else:
        main()
