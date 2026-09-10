# Youtube-PS3-DLNA-V1.0

First public release of Youtube-PS3-DLNA.

## Included

- PS3 XMB DLNA media server
- YouTube video search from the desktop app and directly from the PS3
- Channel search and newest-upload browsing with pagination
- Local Favorites and Subscriptions shared between the PS3 and PC app
- Search History
- Optional personalized Home recommendations
- 360p / 480p PS3-friendly transcoding
- YouTube source chunk relay for improved long-video reliability
- PS3-oriented MPEG-2 + AC-3 MPEG-PS playback
- DLNA time-seek handling for rewind / fast-forward / timeline seeking
- Responsive dark desktop UI
- Windows and Linux launchers
- Python 3.14 target

## Requirements

- Python 3.14
- FFmpeg
- YouTube Data API v3 API key
- PS3 and PC on the same local network

## Launch

Windows: run `run_windows.bat`.

Linux: run `run_linux.sh`.

The Linux launcher uses `python3` and installs Python requirements with `--break-system-packages` as requested by the project.

## Notes

The YouTube API key and local user data are stored outside the program directory in `~/.ps3_youtube_dlna/` and are not included in the release.
