#!/bin/bash
# Keeps the watcher alive: restarts it if it ever crashes or exits after restart.flag.
# Exit code 2 means config.json is missing or invalid, so it stops instead of looping.
# Start it with `python3 watcher.py --start` (opens it in its own Terminal window) or run
# ./run.sh in any terminal you leave open.
cd "$(dirname "$0")" || exit 1
while true; do
  caffeinate -i python3 watcher.py
  code=$?
  if [ "$code" -eq 2 ]; then
    echo "watcher.py exited with a config error; fix config.json and rerun." >&2
    exit 2
  fi
  echo "$(date '+%m-%d %H:%M:%S') watcher exited ($code), restarting in 5s" >> watcher.log
  sleep 5
done
