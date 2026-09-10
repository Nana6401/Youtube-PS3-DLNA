#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
python3 -m pip install --break-system-packages -r requirements.txt
python3 ps3_youtube_dlna.py
