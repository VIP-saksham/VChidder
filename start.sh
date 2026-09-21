#!/bin/bash
# VChidder — start/restart bot under screen 'vchidder'
cd "$(dirname "$0")"
while screen -ls 2>/dev/null | grep -q '\.vchidder'; do
  screen -S vchidder -X quit >/dev/null 2>&1
  sleep 0.3
done
rm -f log.txt
screen -dmS vchidder bash -c "export PYTHONUNBUFFERED=1; exec $PWD/venv/bin/python $PWD/Vc.py >> $PWD/log.txt 2>&1"
echo "LAUNCHED — logs: tail -f log.txt"
