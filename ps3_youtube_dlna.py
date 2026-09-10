#!/usr/bin/env python3
"""
PS3 YouTube DLNA Bridge
Python 3.14, Windows + Linux

The PC appears as a DLNA Media Server in the PS3 XMB > Video menu.
Searches use the official YouTube Data API v3, from either the desktop UI or an XMB folder-keyboard. When the PS3 opens a result,
yt-dlp resolves a playable YouTube media URL and FFmpeg transcodes it on-the-fly
to a PS3-safe MPEG Program Stream carrying MPEG-2 video + AC-3 audio.
"""

from __future__ import annotations

import base64
import html
import hashlib
import http.server
import json
import os
import queue
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuid_mod
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from pathlib import Path
from tkinter import messagebox, ttk, filedialog
from typing import Optional

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

APP_NAME = "PS3 YouTube DLNA Bridge"
APP_VERSION = "1.0"
CONFIG_DIR = Path.home() / ".ps3_youtube_dlna"
CONFIG_FILE = CONFIG_DIR / "config.json"
LIBRARY_FILE = CONFIG_DIR / "library.json"
THUMB_CACHE_DIR = CONFIG_DIR / "thumbnails"
MAX_HISTORY_SEARCHES = 12
PS3_SEARCH_MAX_CHARS = 60
PS3_SEARCH_CACHE_SECONDS = 300
HOME_REFRESH_SECONDS = 30 * 60
CHANNEL_PAGE_CACHE_SECONDS = 10 * 60
CHANNEL_VISIT_DEBOUNCE_SECONDS = 5 * 60
MAX_WATCH_HISTORY = 120
WATCH_LEARN_MIN_SECONDS = 12.0
WATCH_LEARN_MIN_BYTES = 4 * 1024 * 1024
DEFAULT_PORT = 8099
YOUTUBE_PROXY_CHUNK_BYTES = 8 * 1024 * 1024
YOUTUBE_PROXY_RETRIES = 5
YOUTUBE_PROXY_TTL_SECONDS = 4 * 60 * 60
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

DLNA_FLAGS = "01700000000000000000000000000000"
SERVER_HEADER = f"Windows/10.0 DLNADOC/1.50 UPnP/1.0 PS3-YouTube-DLNA/{APP_VERSION}"

# The PS3 is unusually conservative when deciding which ContentDirectory items
# to display.  The first resource is intentionally generic (like older DLNA
# servers) so the XMB classifies it as ordinary MPEG video instead of filtering
# it out because of an over-specific DLNA profile.  HTTP responses still include
# useful DLNA contentFeatures separately.
PROTOCOL_INFO = (
    "http-get:*:video/mpeg:"
    "DLNA.ORG_OP=10;DLNA.ORG_CI=1;DLNA.ORG_FLAGS=" + DLNA_FLAGS
)
PROTOCOL_INFO_DETAILED = PROTOCOL_INFO
CONTENT_FEATURES = "DLNA.ORG_OP=10;DLNA.ORG_CI=1;DLNA.ORG_FLAGS=" + DLNA_FLAGS

DEVICE_XML = """<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"
      xmlns:dlna="urn:schemas-dlna-org:device-1-0"
      xmlns:av="urn:schemas-sony-com:av">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>{friendly_name}</friendlyName>
    <manufacturer>PS3 YouTube DLNA Bridge</manufacturer>
    <manufacturerURL>https://openai.com/</manufacturerURL>
    <modelDescription>PS3-compatible YouTube DLNA media server</modelDescription>
    <modelName>Windows Media Connect compatible (PS3 YouTube Bridge)</modelName>
    <modelNumber>{version}</modelNumber>
    <modelURL>https://openai.com/</modelURL>
    <serialNumber>1</serialNumber>
    <UDN>uuid:{udn}</UDN>
    <dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC>
    <dlna:X_DLNACAP></dlna:X_DLNACAP>
    <av:aggregationFlags>10</av:aggregationFlags>
    <presentationURL>http://{ip}:{port}/</presentationURL>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
        <SCPDURL>/ContentDirectory/scpd.xml</SCPDURL>
        <controlURL>/ContentDirectory/control</controlURL>
        <eventSubURL>/ContentDirectory/event</eventSubURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/ConnectionManager/scpd.xml</SCPDURL>
        <controlURL>/ConnectionManager/control</controlURL>
        <eventSubURL>/ConnectionManager/event</eventSubURL>
      </service>
      <service>
        <serviceType>urn:microsoft.com:service:X_MS_MediaReceiverRegistrar:1</serviceType>
        <serviceId>urn:microsoft.com:serviceId:X_MS_MediaReceiverRegistrar</serviceId>
        <SCPDURL>/X_MS_MediaReceiverRegistrar/scpd.xml</SCPDURL>
        <controlURL>/X_MS_MediaReceiverRegistrar/control</controlURL>
        <eventSubURL>/X_MS_MediaReceiverRegistrar/event</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>
"""

CONTENT_DIRECTORY_SCPD = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetSearchCapabilities</name><argumentList>
      <argument><name>SearchCaps</name><direction>out</direction><relatedStateVariable>SearchCapabilities</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetSortCapabilities</name><argumentList>
      <argument><name>SortCaps</name><direction>out</direction><relatedStateVariable>SortCapabilities</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetSystemUpdateID</name><argumentList>
      <argument><name>Id</name><direction>out</direction><relatedStateVariable>SystemUpdateID</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>Browse</name><argumentList>
      <argument><name>ObjectID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>
      <argument><name>BrowseFlag</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>
      <argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>
      <argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>
      <argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
      <argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>
      <argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
      <argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
      <argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
      <argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>
    </argumentList></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="yes"><name>TransferIDs</name><dataType>string</dataType><defaultValue></defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ObjectID</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Result</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_BrowseFlag</name><dataType>string</dataType><allowedValueList><allowedValue>BrowseMetadata</allowedValue><allowedValue>BrowseDirectChildren</allowedValue></allowedValueList></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Filter</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Index</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Count</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_UpdateID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_SortCriteria</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SearchCapabilities</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SortCapabilities</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SystemUpdateID</name><dataType>ui4</dataType><defaultValue>0</defaultValue></stateVariable>
    <stateVariable sendEvents="yes"><name>ContainerUpdateIDs</name><dataType>string</dataType><defaultValue></defaultValue></stateVariable>
  </serviceStateTable>
</scpd>
"""

CONNECTION_MANAGER_SCPD = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetProtocolInfo</name><argumentList>
      <argument><name>Source</name><direction>out</direction><relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>
      <argument><name>Sink</name><direction>out</direction><relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetCurrentConnectionIDs</name><argumentList>
      <argument><name>ConnectionIDs</name><direction>out</direction><relatedStateVariable>CurrentConnectionIDs</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetCurrentConnectionInfo</name><argumentList>
      <argument><name>ConnectionID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>
      <argument><name>RcsID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_RcsID</relatedStateVariable></argument>
      <argument><name>AVTransportID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_AVTransportID</relatedStateVariable></argument>
      <argument><name>ProtocolInfo</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ProtocolInfo</relatedStateVariable></argument>
      <argument><name>PeerConnectionManager</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionManager</relatedStateVariable></argument>
      <argument><name>PeerConnectionID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>
      <argument><name>Direction</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Direction</relatedStateVariable></argument>
      <argument><name>Status</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionStatus</relatedStateVariable></argument>
    </argumentList></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="yes"><name>SourceProtocolInfo</name><dataType>string</dataType><defaultValue>{protocol_info}</defaultValue></stateVariable>
    <stateVariable sendEvents="yes"><name>SinkProtocolInfo</name><dataType>string</dataType><defaultValue></defaultValue></stateVariable>
    <stateVariable sendEvents="yes"><name>CurrentConnectionIDs</name><dataType>string</dataType><defaultValue>0</defaultValue></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionStatus</name><dataType>string</dataType><allowedValueList><allowedValue>OK</allowedValue><allowedValue>ContentFormatMismatch</allowedValue><allowedValue>InsufficientBandwidth</allowedValue><allowedValue>UnreliableChannel</allowedValue><allowedValue>Unknown</allowedValue></allowedValueList></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionManager</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Direction</name><dataType>string</dataType><allowedValueList><allowedValue>Input</allowedValue><allowedValue>Output</allowedValue></allowedValueList></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionID</name><dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_AVTransportID</name><dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_RcsID</name><dataType>i4</dataType></stateVariable>
  </serviceStateTable>
</scpd>
""".format(protocol_info=html.escape(PROTOCOL_INFO))

MEDIA_RECEIVER_SCPD = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>IsAuthorized</name><argumentList>
      <argument><name>DeviceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_DeviceID</relatedStateVariable></argument>
      <argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>IsValidated</name><argumentList>
      <argument><name>DeviceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_DeviceID</relatedStateVariable></argument>
      <argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>RegisterDevice</name><argumentList>
      <argument><name>RegistrationReqMsg</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_RegistrationReqMsg</relatedStateVariable></argument>
      <argument><name>RegistrationRespMsg</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_RegistrationRespMsg</relatedStateVariable></argument>
    </argumentList></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_DeviceID</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_RegistrationReqMsg</name><dataType>bin.base64</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_RegistrationRespMsg</name><dataType>bin.base64</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Result</name><dataType>int</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>AuthorizationDeniedUpdateID</name><dataType>ui4</dataType><defaultValue>0</defaultValue></stateVariable>
    <stateVariable sendEvents="yes"><name>AuthorizationGrantedUpdateID</name><dataType>ui4</dataType><defaultValue>0</defaultValue></stateVariable>
    <stateVariable sendEvents="yes"><name>ValidationRevokedUpdateID</name><dataType>ui4</dataType><defaultValue>0</defaultValue></stateVariable>
    <stateVariable sendEvents="yes"><name>ValidationSucceededUpdateID</name><dataType>ui4</dataType><defaultValue>0</defaultValue></stateVariable>
  </serviceStateTable>
</scpd>
"""


@dataclass
class Settings:
    api_key: str = ""
    friendly_name: str = "PS3 YouTube"
    port: int = DEFAULT_PORT
    advertised_ip: str = ""
    ffmpeg_path: str = ""
    max_results: int = 50
    region_code: str = "FR"
    relevance_language: str = "en"
    max_height: int = 480
    recommendations_enabled: bool = True
    udn: str = ""

    @classmethod
    def load(cls) -> "Settings":
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_FILE.exists():
            s = cls()
            s.udn = str(uuid_mod.uuid4())
            return s
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            allowed = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            s = cls(**allowed)
            if not s.udn:
                s.udn = str(uuid_mod.uuid4())
            s.max_results = max(1, min(50, int(s.max_results or 50)))
            # Migrate the old v0.7 defaults (20 results / 720p) to the new
            # library-oriented defaults requested for v0.8.
            if data.get("max_results") == 20:
                s.max_results = 50
            if int(s.max_height or 480) not in (360, 480):
                s.max_height = 480
            return s
        except Exception:
            s = cls()
            s.udn = str(uuid_mod.uuid4())
            return s

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


@dataclass
class VideoResult:
    video_id: str
    title: str
    channel: str
    channel_id: str = ""
    thumbnail: str = ""
    duration_seconds: int = 0
    definition: str = "sd"

    @classmethod
    def from_dict(cls, data: dict) -> "VideoResult":
        allowed = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**allowed)


@dataclass
class ChannelResult:
    channel_id: str
    title: str
    thumbnail: str = ""
    uploads_playlist: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "ChannelResult":
        allowed = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**allowed)


class BridgeState:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.results: list[VideoResult] = []
        self.results_lock = threading.RLock()
        self.current_query = ""
        self.update_id = 1
        self.running = threading.Event()
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.active_streams: dict[str, subprocess.Popen] = {}
        self.active_streams_lock = threading.RLock()
        self.subscribers: dict[str, dict[str, object]] = {}
        self.subscribers_lock = threading.RLock()
        self.refresh_callback = None
        self.library_lock = threading.RLock()
        self.favorites: dict[str, VideoResult] = {}
        self.history: list[dict] = []
        self.channel_subscriptions: dict[str, ChannelResult] = {}
        self.channel_visits: dict[str, dict] = {}
        self.channel_search_cache: dict[str, tuple[float, list[ChannelResult]]] = {}
        self.channel_page_cache: dict[str, tuple[float, list[VideoResult], str, ChannelResult]] = {}
        self.channel_page_tokens: dict[str, tuple[str, str]] = {}
        self.channel_query_tokens: dict[str, str] = {}
        self.channel_cache_lock = threading.RLock()
        # Searches launched from the PS3 are cached briefly so the console can
        # re-Browse the ENTER folder without spending YouTube API quota again.
        self.ps3_search_cache: dict[str, tuple[float, list[VideoResult]]] = {}
        self.ps3_search_lock = threading.RLock()
        # Short aliases used by the PS3-side keyboard.  Older PS3 XMB builds can
        # behave strangely with very long ContentDirectory ObjectIDs.  Keep the
        # typed query in memory and expose only a tiny deterministic token to the
        # console for the ENTER/results container.
        self.ps3_query_tokens: dict[str, str] = {}
        self.ps3_query_tokens_lock = threading.RLock()
        # Personalized Home is learned only from activity inside this bridge.
        # It never touches the user's YouTube account/watch history.
        self.watch_history: list[dict] = []
        self.home_results: list[VideoResult] = []
        self.popular_results: list[VideoResult] = []
        self.home_generated_at: float = 0.0
        self.home_refresh_lock = threading.Lock()
        # Short-lived localhost source proxies used by FFmpeg.  YouTube can
        # throttle very large/open-ended media HTTP requests, which becomes
        # especially visible on long videos.  The proxy turns FFmpeg's reads
        # into bounded Range requests and retries individual chunks.
        self.youtube_sources: dict[str, dict[str, object]] = {}
        self.youtube_sources_lock = threading.RLock()
        self._load_library()

    def log(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{stamp}] {msg}")

    def register_youtube_source(self, fmt: dict, info: dict) -> str:
        headers = dict(info.get("http_headers") or {})
        headers.update(fmt.get("http_headers") or {})
        keep_headers = {}
        for name in ("User-Agent", "Referer", "Origin", "Cookie", "Accept-Language"):
            value = headers.get(name)
            if value:
                keep_headers[name] = str(value)
        keep_headers.setdefault("User-Agent", "Mozilla/5.0")
        token = uuid_mod.uuid4().hex
        now = time.time()
        with self.youtube_sources_lock:
            cutoff = now - YOUTUBE_PROXY_TTL_SECONDS
            stale = [k for k, v in self.youtube_sources.items() if float(v.get("created_at", 0) or 0) < cutoff]
            for key in stale:
                self.youtube_sources.pop(key, None)
            self.youtube_sources[token] = {
                "url": str(fmt.get("url") or ""),
                "headers": keep_headers,
                "created_at": now,
            }
        return token

    def get_youtube_source(self, token: str) -> Optional[dict[str, object]]:
        with self.youtube_sources_lock:
            source = self.youtube_sources.get(token)
            if not source:
                return None
            if time.time() - float(source.get("created_at", 0) or 0) > YOUTUBE_PROXY_TTL_SECONDS:
                self.youtube_sources.pop(token, None)
                return None
            return dict(source)

    def remove_youtube_sources(self, tokens: list[str]) -> None:
        with self.youtube_sources_lock:
            for token in tokens:
                self.youtube_sources.pop(token, None)

    def _load_library(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if not LIBRARY_FILE.exists():
            return
        try:
            data = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
            favs = {}
            for raw in data.get("favorites", []):
                try:
                    item = VideoResult.from_dict(raw)
                    if item.video_id:
                        favs[item.video_id] = item
                except Exception:
                    continue
            history = []
            for entry in data.get("history", [])[:MAX_HISTORY_SEARCHES]:
                if not isinstance(entry, dict):
                    continue
                results = []
                for raw in entry.get("results", []):
                    try:
                        item = VideoResult.from_dict(raw)
                        if item.video_id:
                            results.append(item)
                    except Exception:
                        continue
                query = sanitize_title(str(entry.get("query", "")))
                if query and results:
                    history.append({
                        "id": str(entry.get("id") or uuid_mod.uuid4().hex[:10]),
                        "query": query,
                        "created_at": float(entry.get("created_at", time.time())),
                        "results": results[:50],
                    })
            self.favorites = favs
            self.history = history[:MAX_HISTORY_SEARCHES]

            subscriptions: dict[str, ChannelResult] = {}
            for raw in data.get("channel_subscriptions", []):
                try:
                    channel = ChannelResult.from_dict(raw)
                    if channel.channel_id:
                        subscriptions[channel.channel_id] = channel
                except Exception:
                    continue
            self.channel_subscriptions = subscriptions

            visits: dict[str, dict] = {}
            for channel_id, raw in (data.get("channel_visits", {}) or {}).items():
                if not isinstance(raw, dict) or not channel_id:
                    continue
                visits[str(channel_id)] = {
                    "title": sanitize_title(str(raw.get("title", ""))),
                    "count": max(0, int(raw.get("count", 0) or 0)),
                    "last_visit": float(raw.get("last_visit", 0) or 0),
                }
            self.channel_visits = visits

            watches = []
            for raw in data.get("watch_history", [])[:MAX_WATCH_HISTORY]:
                if not isinstance(raw, dict):
                    continue
                try:
                    item = VideoResult.from_dict(raw.get("video", {}))
                except Exception:
                    continue
                if not item.video_id:
                    continue
                watches.append({
                    "video": item,
                    "watched_at": float(raw.get("watched_at", 0) or 0),
                    "seconds": float(raw.get("seconds", 0) or 0),
                })
            self.watch_history = watches

            def _load_video_list(name: str) -> list[VideoResult]:
                loaded = []
                for raw in data.get(name, []):
                    try:
                        item = VideoResult.from_dict(raw)
                        if item.video_id:
                            loaded.append(item)
                    except Exception:
                        continue
                return loaded[:50]

            self.home_results = _load_video_list("home_results")
            self.popular_results = _load_video_list("popular_results")
            self.home_generated_at = float(data.get("home_generated_at", 0) or 0)

            if self.history:
                latest = self.history[0]
                self.current_query = latest["query"]
                self.results = list(latest["results"])
        except Exception as e:
            self.log(f"Could not load library history/favorites: {e}")

    def _save_library(self) -> None:
        with self.library_lock:
            payload = {
                "favorites": [asdict(x) for x in self.favorites.values()],
                "history": [
                    {
                        "id": entry["id"],
                        "query": entry["query"],
                        "created_at": entry.get("created_at", 0),
                        "results": [asdict(x) for x in entry.get("results", [])[:50]],
                    }
                    for entry in self.history[:MAX_HISTORY_SEARCHES]
                ],
                "channel_subscriptions": [asdict(x) for x in self.channel_subscriptions.values()],
                "channel_visits": self.channel_visits,
                "watch_history": [
                    {
                        "video": asdict(entry["video"]),
                        "watched_at": entry.get("watched_at", 0),
                        "seconds": entry.get("seconds", 0),
                    }
                    for entry in self.watch_history[:MAX_WATCH_HISTORY]
                ],
                "home_results": [asdict(x) for x in self.home_results[:50]],
                "popular_results": [asdict(x) for x in self.popular_results[:50]],
                "home_generated_at": self.home_generated_at,
            }
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            LIBRARY_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _bump_library(self, message: str) -> None:
        with self.results_lock:
            self.update_id = (self.update_id + 1) & 0xFFFFFFFF
            if self.update_id == 0:
                self.update_id = 1
            update_id = self.update_id
        self.log(f"{message} (update #{update_id}).")
        self.notify_content_changed()
        callback = self.refresh_callback
        if callback is not None:
            try:
                callback()
            except Exception as e:
                self.log(f"DLNA refresh announcement warning: {e}")

    def set_results(self, results: list[VideoResult], query: str = "", record_history: bool = True) -> None:
        clean_query = sanitize_title(query)
        with self.results_lock:
            self.results = list(results[:50])
            self.current_query = clean_query
        if record_history and clean_query and results:
            with self.library_lock:
                self.history = [e for e in self.history if e.get("query", "").casefold() != clean_query.casefold()]
                self.history.insert(0, {
                    "id": uuid_mod.uuid4().hex[:10],
                    "query": clean_query,
                    "created_at": time.time(),
                    "results": list(results[:50]),
                })
                self.history = self.history[:MAX_HISTORY_SEARCHES]
                if self.settings.recommendations_enabled:
                    self.home_generated_at = 0.0
                self._save_library()
        self._bump_library(f"Published {len(results[:50])} YouTube result(s) to the DLNA library")

    def republish(self) -> None:
        self._bump_library("Republishing current DLNA library")

    def snapshot_results(self) -> list[VideoResult]:
        with self.results_lock:
            return list(self.results)

    def snapshot_query(self) -> str:
        with self.results_lock:
            return self.current_query

    def snapshot_favorites(self) -> list[VideoResult]:
        with self.library_lock:
            return list(self.favorites.values())

    def snapshot_history(self) -> list[dict]:
        with self.library_lock:
            return [
                {
                    "id": e["id"],
                    "query": e["query"],
                    "created_at": e.get("created_at", 0),
                    "results": list(e.get("results", [])),
                }
                for e in self.history
            ]

    def add_favorite(self, item: VideoResult) -> bool:
        return self.add_favorites([item]) > 0

    def add_favorites(self, items: list[VideoResult]) -> int:
        added = 0
        with self.library_lock:
            for item in items:
                if item.video_id not in self.favorites:
                    added += 1
                self.favorites[item.video_id] = item
            if items:
                if self.settings.recommendations_enabled:
                    self.home_generated_at = 0.0
                self._save_library()
        if added:
            self._bump_library(f"Added {added} favorite(s)")
        return added

    def remove_favorite(self, video_id: str) -> bool:
        return self.remove_favorites([video_id]) > 0

    def remove_favorites(self, video_ids: list[str]) -> int:
        removed = 0
        with self.library_lock:
            for video_id in video_ids:
                if self.favorites.pop(video_id, None) is not None:
                    removed += 1
            if removed:
                if self.settings.recommendations_enabled:
                    self.home_generated_at = 0.0
                self._save_library()
        if removed:
            self._bump_library(f"Removed {removed} favorite(s)")
        return removed

    def snapshot_channel_subscriptions(self) -> list[ChannelResult]:
        with self.library_lock:
            return list(self.channel_subscriptions.values())

    def add_channel_subscription(self, channel: ChannelResult) -> bool:
        if not channel.channel_id:
            return False
        added = False
        with self.library_lock:
            if channel.channel_id not in self.channel_subscriptions:
                added = True
            self.channel_subscriptions[channel.channel_id] = channel
            if self.settings.recommendations_enabled:
                self.home_generated_at = 0.0
            self._save_library()
        if added:
            self._bump_library(f"Subscribed to channel {channel.title!r}")
        return added

    def remove_channel_subscription(self, channel_id: str) -> bool:
        removed = False
        with self.library_lock:
            if self.channel_subscriptions.pop(channel_id, None) is not None:
                removed = True
                if self.settings.recommendations_enabled:
                    self.home_generated_at = 0.0
                self._save_library()
        if removed:
            self._bump_library("Removed channel subscription")
        return removed

    def is_channel_subscribed(self, channel_id: str) -> bool:
        with self.library_lock:
            return channel_id in self.channel_subscriptions

    def find_channel(self, channel_id: str) -> Optional[ChannelResult]:
        if not channel_id:
            return None
        with self.library_lock:
            saved = self.channel_subscriptions.get(channel_id)
            if saved is not None:
                return ChannelResult.from_dict(asdict(saved))
        with self.channel_cache_lock:
            for _key, (_stamp, _video_list, _next, channel) in self.channel_page_cache.items():
                if channel.channel_id == channel_id:
                    return ChannelResult.from_dict(asdict(channel))
            for _key, (_stamp, channels) in self.channel_search_cache.items():
                for channel in channels:
                    if channel.channel_id == channel_id:
                        return ChannelResult.from_dict(asdict(channel))
        # Current video/history metadata can still identify the channel even if
        # we have not fetched its uploads playlist yet.
        for item in self.snapshot_results() + self.snapshot_favorites():
            if item.channel_id == channel_id:
                return ChannelResult(channel_id=channel_id, title=item.channel)
        return None

    def record_channel_visit(self, channel: ChannelResult) -> None:
        if not self.settings.recommendations_enabled or not channel.channel_id:
            return
        now = time.time()
        changed = False
        with self.library_lock:
            entry = self.channel_visits.get(channel.channel_id)
            if entry and now - float(entry.get("last_visit", 0) or 0) < CHANNEL_VISIT_DEBOUNCE_SECONDS:
                return
            if entry is None:
                entry = {"title": channel.title, "count": 0, "last_visit": 0.0}
                self.channel_visits[channel.channel_id] = entry
            entry["title"] = channel.title or str(entry.get("title", ""))
            entry["count"] = int(entry.get("count", 0) or 0) + 1
            entry["last_visit"] = now
            self.home_generated_at = 0.0
            self._save_library()
            changed = True
        if changed:
            self.log(f"Recommendation learning: visited channel {channel.title!r}.")

    def register_channel_query(self, query: str) -> str:
        clean = sanitize_title(query).strip()[:PS3_SEARCH_MAX_CHARS]
        token = hashlib.sha1(("channel:" + clean.casefold()).encode("utf-8")).hexdigest()[:10]
        with self.channel_cache_lock:
            self.channel_query_tokens[token] = clean
        return token

    def resolve_channel_query(self, token: str) -> str:
        with self.channel_cache_lock:
            return self.channel_query_tokens.get(token, "")

    def run_channel_search(self, query: str) -> list[ChannelResult]:
        clean = sanitize_title(query).strip()[:PS3_SEARCH_MAX_CHARS]
        if not clean:
            return []
        now = time.monotonic()
        key = clean.casefold()
        with self.channel_cache_lock:
            cached = self.channel_search_cache.get(key)
            if cached and now - cached[0] <= PS3_SEARCH_CACHE_SECONDS:
                return list(cached[1])
        self.log(f"PS3 requested YouTube channel search: {clean!r}")
        channels = youtube_channel_search(self.settings, clean)
        with self.channel_cache_lock:
            self.channel_search_cache[key] = (time.monotonic(), list(channels))
        return channels

    def register_channel_page(self, channel_id: str, page_token: str = "") -> str:
        raw = f"{channel_id}|{page_token}"
        token = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        with self.channel_cache_lock:
            self.channel_page_tokens[token] = (channel_id, page_token)
        return token

    def resolve_channel_page(self, token: str) -> tuple[str, str]:
        with self.channel_cache_lock:
            return self.channel_page_tokens.get(token, ("", ""))

    def get_channel_page(self, channel_id: str, page_token: str = "") -> tuple[list[VideoResult], str, ChannelResult]:
        cache_key = f"{channel_id}|{page_token}"
        now = time.monotonic()
        with self.channel_cache_lock:
            cached = self.channel_page_cache.get(cache_key)
            if cached and now - cached[0] <= CHANNEL_PAGE_CACHE_SECONDS:
                return list(cached[1]), cached[2], ChannelResult.from_dict(asdict(cached[3]))
        known = self.find_channel(channel_id)
        videos, next_token, channel = youtube_channel_videos(
            self.settings,
            channel_id,
            page_token=page_token,
            max_results=self.settings.max_results,
            known_channel=known,
        )
        with self.channel_cache_lock:
            self.channel_page_cache[cache_key] = (time.monotonic(), list(videos), next_token, channel)
        return videos, next_token, channel

    def clear_history(self) -> None:
        with self.library_lock:
            self.history.clear()
            self._save_library()
        self._bump_library("Cleared search history")

    def clear_recommendation_learning(self) -> None:
        with self.library_lock:
            self.watch_history.clear()
            self.channel_visits.clear()
            self.home_results.clear()
            self.popular_results.clear()
            self.home_generated_at = 0.0
            self._save_library()
        self._bump_library("Reset recommendation learning")

    def record_watch(self, item: VideoResult, seconds: float) -> None:
        if not self.settings.recommendations_enabled or not item.video_id:
            return
        now = time.time()
        with self.library_lock:
            # A PS3 can reconnect to the same stream while starting playback.
            # Merge very recent duplicates instead of teaching the engine twice.
            if self.watch_history and self.watch_history[0]["video"].video_id == item.video_id and now - float(self.watch_history[0].get("watched_at", 0)) < 300:
                self.watch_history[0]["watched_at"] = now
                self.watch_history[0]["seconds"] = max(float(self.watch_history[0].get("seconds", 0)), float(seconds))
            else:
                self.watch_history.insert(0, {"video": item, "watched_at": now, "seconds": float(seconds)})
                self.watch_history = self.watch_history[:MAX_WATCH_HISTORY]
            # Mark Home stale; it will refresh lazily next time the PS3 opens it.
            self.home_generated_at = 0.0
            self._save_library()
        self.log(f"Recommendation learning: watched {item.title!r} ({seconds:.0f}s+).")

    def snapshot_home(self) -> list[VideoResult]:
        with self.library_lock:
            return list(self.home_results)

    def snapshot_popular(self) -> list[VideoResult]:
        with self.library_lock:
            return list(self.popular_results)

    def home_is_stale(self) -> bool:
        with self.library_lock:
            return (not self.home_results) or (time.time() - self.home_generated_at >= HOME_REFRESH_SECONDS)

    def _recommendation_seed_query(self) -> str:
        stop = {
            'the','a','an','and','or','of','to','in','on','for','with','from','by','at','is','it','this','that','my','your',
            'video','official','full','episode','part','live','new','best','song','music','game','gaming','hd','4k','1080p','720p','480p'
        }
        scores: dict[str, float] = {}

        def add_phrase(value: str, weight: float) -> None:
            value = sanitize_title(value).lower()
            words = [w for w in re.findall(r"[a-z0-9][a-z0-9'_-]{1,24}", value) if w not in stop and len(w) >= 3]
            for word in words[:8]:
                scores[word] = scores.get(word, 0.0) + weight

        with self.library_lock:
            for rank, entry in enumerate(self.watch_history[:30]):
                item = entry['video']
                weight = max(1.0, 5.0 - rank * 0.12)
                add_phrase(item.title, weight)
                add_phrase(item.channel, weight * 0.8)
            for item in list(self.favorites.values())[:30]:
                add_phrase(item.title, 6.0)
                add_phrase(item.channel, 3.0)
            for channel in list(self.channel_subscriptions.values())[:30]:
                add_phrase(channel.title, 5.0)
            for channel_id, visit in self.channel_visits.items():
                count = max(0, int(visit.get('count', 0) or 0))
                if count:
                    add_phrase(str(visit.get('title', '')), min(6.0, 1.5 + count * 0.8))
            for rank, entry in enumerate(self.history[:8]):
                add_phrase(str(entry.get('query', '')), max(1.0, 3.0 - rank * 0.25))

        terms = [term for term, _score in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:6]]
        return '|'.join(terms)

    def _recommendation_channel_candidates(self) -> list[ChannelResult]:
        scores: dict[str, float] = {}
        channels: dict[str, ChannelResult] = {}
        with self.library_lock:
            for channel in self.channel_subscriptions.values():
                channels[channel.channel_id] = ChannelResult.from_dict(asdict(channel))
                scores[channel.channel_id] = scores.get(channel.channel_id, 0.0) + 8.0
            for channel_id, visit in self.channel_visits.items():
                if not channel_id:
                    continue
                scores[channel_id] = scores.get(channel_id, 0.0) + min(10.0, float(visit.get('count', 0) or 0) * 2.0)
                channels.setdefault(channel_id, ChannelResult(channel_id, sanitize_title(str(visit.get('title', '')))))
            for rank, entry in enumerate(self.watch_history[:40]):
                item = entry['video']
                if not item.channel_id:
                    continue
                scores[item.channel_id] = scores.get(item.channel_id, 0.0) + max(1.0, 5.0 - rank * 0.1)
                channels.setdefault(item.channel_id, ChannelResult(item.channel_id, item.channel))
        ordered = sorted(scores, key=lambda cid: (-scores[cid], channels.get(cid, ChannelResult(cid, '')).title.casefold()))
        return [channels[cid] for cid in ordered[:3] if channels.get(cid) and channels[cid].title]

    def refresh_home(self, force: bool = False) -> list[VideoResult]:
        if not self.settings.recommendations_enabled:
            return []
        if not force and not self.home_is_stale():
            return self.snapshot_home()
        if not self.home_refresh_lock.acquire(blocking=False):
            return self.snapshot_home()
        try:
            if not force and not self.home_is_stale():
                return self.snapshot_home()
            seed = self._recommendation_seed_query()
            personalized: list[VideoResult] = []
            if seed:
                self.log(f"Refreshing Home recommendations from interests: {seed.replace('|', ', ')}")
                personalized = youtube_search(self.settings, seed, max_results=min(40, self.settings.max_results))
            else:
                self.log("Refreshing Home: not enough learned interests yet; using channels/popular videos.")

            channel_recs: list[VideoResult] = []
            for channel in self._recommendation_channel_candidates():
                try:
                    videos, _next, resolved = self.get_channel_page(channel.channel_id, "")
                    # A few newest uploads from each channel are enough to make
                    # Home feel familiar without turning it into a subscriptions feed.
                    channel_recs.extend(videos[:6])
                    self.log(f"Home channel boost: {resolved.title!r} ({min(6, len(videos))} video(s)).")
                except Exception as e:
                    self.log(f"Home channel boost skipped for {channel.title!r}: {e}")

            popular = youtube_most_popular(self.settings, max_results=12)
            sources = {
                'personal': personalized,
                'channel': channel_recs,
                'popular': popular,
            }
            indexes = {name: 0 for name in sources}
            pattern = ['personal', 'channel', 'personal', 'popular', 'personal', 'channel']
            seen: set[str] = set()
            mixed: list[VideoResult] = []
            target = min(50, self.settings.max_results)

            def take_from(name: str) -> bool:
                src = sources[name]
                idx = indexes[name]
                while idx < len(src):
                    item = src[idx]
                    idx += 1
                    indexes[name] = idx
                    if item.video_id in seen:
                        continue
                    seen.add(item.video_id)
                    mixed.append(item)
                    return True
                indexes[name] = idx
                return False

            while len(mixed) < target:
                progressed = False
                for name in pattern:
                    if len(mixed) >= target:
                        break
                    if take_from(name):
                        progressed = True
                if not progressed:
                    break

            with self.library_lock:
                self.home_results = mixed
                self.popular_results = popular
                self.home_generated_at = time.time()
                self._save_library()
            self._bump_library(f"Refreshed Home with {len(mixed)} recommendation(s)")
            return list(mixed)
        finally:
            self.home_refresh_lock.release()


    def find_video(self, video_id: str) -> Optional[VideoResult]:
        with self.results_lock:
            for item in self.results:
                if item.video_id == video_id:
                    return item
        with self.library_lock:
            if video_id in self.favorites:
                return self.favorites[video_id]
            for entry in self.history:
                for item in entry.get("results", []):
                    if item.video_id == video_id:
                        return item
            for item in self.home_results:
                if item.video_id == video_id:
                    return item
            for item in self.popular_results:
                if item.video_id == video_id:
                    return item
        with self.channel_cache_lock:
            for _key, (_stamp, videos, _next, _channel) in self.channel_page_cache.items():
                for item in videos:
                    if item.video_id == video_id:
                        return item
        return None

    def history_entry(self, entry_id: str) -> Optional[dict]:
        with self.library_lock:
            for entry in self.history:
                if entry.get("id") == entry_id:
                    return {**entry, "results": list(entry.get("results", []))}
        return None

    def register_ps3_query(self, query: str) -> str:
        clean_query = sanitize_title(query).strip()[:PS3_SEARCH_MAX_CHARS]
        token = hashlib.sha1(clean_query.casefold().encode("utf-8")).hexdigest()[:10]
        with self.ps3_query_tokens_lock:
            self.ps3_query_tokens[token] = clean_query
        return token

    def resolve_ps3_query(self, token: str) -> str:
        with self.ps3_query_tokens_lock:
            return self.ps3_query_tokens.get(token, "")

    def run_ps3_search(self, query: str) -> list[VideoResult]:
        clean_query = sanitize_title(query).strip()[:PS3_SEARCH_MAX_CHARS]
        if not clean_query:
            return []
        now = time.monotonic()
        with self.ps3_search_lock:
            cached = self.ps3_search_cache.get(clean_query.casefold())
            if cached and now - cached[0] <= PS3_SEARCH_CACHE_SECONDS:
                results = list(cached[1])
                self.log(f"PS3 search cache hit: {clean_query!r} ({len(results)} result(s)).")
                # Still publish it as Current Search, but do not duplicate history.
                self.set_results(results, clean_query, record_history=False)
                return results

        self.log(f"PS3 requested YouTube search: {clean_query!r}")
        results = youtube_search(self.settings, clean_query)
        with self.ps3_search_lock:
            self.ps3_search_cache[clean_query.casefold()] = (time.monotonic(), list(results))
        self.set_results(results, clean_query, record_history=True)
        return results

    def library_title(self) -> str:
        with self.results_lock:
            if self.current_query:
                return sanitize_title(f'Current Search: {self.current_query} ({len(self.results)})')
            return "Current Search"

    @staticmethod
    def _parse_timeout(value: str) -> int:
        if not value:
            return 1800
        if value.lower() == "second-infinite":
            return 86400
        m = re.match(r"(?i)^second-(\d+)$", value.strip())
        if not m:
            return 1800
        return max(60, min(86400, int(m.group(1))))

    def add_subscriber(self, callback: str, timeout_header: str) -> tuple[str, int]:
        timeout = self._parse_timeout(timeout_header)
        sid = f"uuid:{uuid_mod.uuid4()}"
        with self.subscribers_lock:
            self.subscribers[sid] = {
                "callback": callback,
                "expires": time.monotonic() + timeout,
                "seq": 0,
            }
        self.log(f"ContentDirectory subscriber added: {sid} -> {callback}")
        return sid, timeout

    def renew_subscriber(self, sid: str, timeout_header: str) -> Optional[int]:
        timeout = self._parse_timeout(timeout_header)
        with self.subscribers_lock:
            sub = self.subscribers.get(sid)
            if sub is None:
                return None
            sub["expires"] = time.monotonic() + timeout
        return timeout

    def remove_subscriber(self, sid: str) -> None:
        with self.subscribers_lock:
            self.subscribers.pop(sid, None)

    def _subscriber_snapshot(self) -> list[tuple[str, str, int]]:
        now = time.monotonic()
        out = []
        with self.subscribers_lock:
            expired = []
            for sid, sub in self.subscribers.items():
                if float(sub.get("expires", 0)) <= now:
                    expired.append(sid)
                    continue
                out.append((sid, str(sub.get("callback", "")), int(sub.get("seq", 0))))
            for sid in expired:
                self.subscribers.pop(sid, None)
        return out

    def notify_content_changed(self, only_sid: Optional[str] = None) -> None:
        if not self.running.is_set():
            return
        update_id = self.update_id
        subscribers = self._subscriber_snapshot()
        for sid, callback, seq in subscribers:
            if only_sid and sid != only_sid:
                continue
            if not callback:
                continue
            threading.Thread(
                target=self._send_event_notify,
                args=(sid, callback, seq, update_id),
                daemon=True,
                name="DLNAEventNotify",
            ).start()

    def _send_event_notify(self, sid: str, callback: str, seq: int, update_id: int) -> None:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            f'<e:property><SystemUpdateID>{update_id}</SystemUpdateID></e:property>'
            f'<e:property><ContainerUpdateIDs>0,{update_id},home,{update_id},popular,{update_id},yt,{update_id},fav,{update_id},channelsubs,{update_id},history,{update_id},ps3search,{update_id},chsearch,{update_id}</ContainerUpdateIDs></e:property>'
            '</e:propertyset>'
        ).encode("utf-8")
        req = urllib.request.Request(callback, data=body, method="NOTIFY", headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "NT": "upnp:event",
            "NTS": "upnp:propchange",
            "SID": sid,
            "SEQ": str(seq & 0xFFFFFFFF),
            "Connection": "close",
        })
        try:
            with urllib.request.urlopen(req, timeout=2) as response:
                response.read(1)
            with self.subscribers_lock:
                sub = self.subscribers.get(sid)
                if sub is not None:
                    sub["seq"] = (seq + 1) & 0xFFFFFFFF
            self.log(f"Sent PS3 library-change event #{update_id}.")
        except Exception as e:
            self.log(f"DLNA event notify warning ({sid}): {e}")


def detect_local_ip() -> str:
    # UDP connect chooses the interface without sending application data.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def find_ffmpeg(configured: str = "") -> Optional[str]:
    candidates = []
    if configured:
        candidates.append(configured)
    program_dir = Path(sys.argv[0]).resolve().parent
    candidates += [
        str(program_dir / "ffmpeg.exe"),
        str(program_dir / "ffmpeg"),
    ]
    path_hit = shutil.which("ffmpeg")
    if path_hit:
        candidates.append(path_hit)
    for c in candidates:
        if c and Path(c).is_file():
            return str(Path(c).resolve())
    return None


def xml_escape(s: str) -> str:
    return html.escape(s, quote=True)


def sanitize_title(s: str) -> str:
    # XMB behaves better without control chars.
    s = re.sub(r"[\x00-\x1f\x7f]", " ", s)
    return re.sub(r"\s+", " ", s).strip()[:180]


def parse_iso8601_duration(value: str) -> int:
    """Parse the subset of ISO-8601 durations returned by YouTube (PnDTnHnMnS)."""
    m = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        value or "",
    )
    if not m:
        return 0
    parts = {k: int(v or 0) for k, v in m.groupdict().items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


def dlna_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.000"


def parse_npt_seconds(value: str) -> float:
    """Parse the start value from a DLNA TimeSeekRange npt expression."""
    if not value:
        return 0.0
    text = value.strip()
    if text.lower().startswith("npt="):
        text = text[4:]
    start = text.split("-", 1)[0].strip()
    if not start:
        return 0.0
    try:
        if ":" not in start:
            return max(0.0, float(start))
        parts = start.split(":")
        if len(parts) == 3:
            return max(0.0, int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2]))
        if len(parts) == 2:
            return max(0.0, int(parts[0]) * 60 + float(parts[1]))
    except (TypeError, ValueError):
        return 0.0
    return 0.0


def npt_response(start: float, duration: int) -> str:
    start = max(0.0, float(start))
    if duration > 0:
        end = max(start, float(duration))
        return f"npt={start:.3f}-{end:.3f}/{float(duration):.3f}"
    return f"npt={start:.3f}-"


def target_resolution(result: VideoResult, max_height: int) -> tuple[int, int]:
    # The transcoder keeps aspect ratio and caps height. For ContentDirectory
    # metadata the PS3 mainly needs a plausible, supported resolution so it
    # doesn't have to probe the HTTP stream just to classify the item.
    h = max_height if result.definition == "hd" else min(max_height, 480)
    if h >= 720:
        return (1280, 720)
    if h >= 480:
        return (854, 480)
    return (640, 360)


def enrich_youtube_details(settings: Settings, results: list[VideoResult]) -> None:
    """Fill duration/definition with one cheap videos.list API call."""
    ids = [r.video_id for r in results if r.video_id]
    if not ids:
        return
    params = {
        "part": "contentDetails",
        "id": ",".join(ids[:50]),
        "key": settings.api_key.strip(),
    }
    url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.load(response)
    except Exception:
        # Search results remain usable if this optional metadata lookup fails.
        return
    by_id = {r.video_id: r for r in results}
    for item in payload.get("items", []):
        result = by_id.get(item.get("id", ""))
        if result is None:
            continue
        details = item.get("contentDetails", {})
        result.duration_seconds = parse_iso8601_duration(details.get("duration", ""))
        result.definition = details.get("definition", "sd") if details.get("definition") in ("hd", "sd") else "sd"


def youtube_search(settings: Settings, query: str, max_results: Optional[int] = None) -> list[VideoResult]:
    if not settings.api_key.strip():
        raise RuntimeError("Enter a YouTube Data API v3 key first.")
    params = {
        "part": "snippet",
        "type": "video",
        "q": query,
        "maxResults": str(max(1, min(50, int(settings.max_results if max_results is None else max_results)))),
        "key": settings.api_key.strip(),
    }
    if settings.region_code.strip():
        params["regionCode"] = settings.region_code.strip().upper()
    if settings.relevance_language.strip():
        params["relevanceLanguage"] = settings.relevance_language.strip()

    url = "https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body).get("error", {}).get("message", body)
        except Exception:
            detail = body
        raise RuntimeError(f"YouTube API error {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not contact YouTube API: {e.reason}") from e

    out: list[VideoResult] = []
    for item in payload.get("items", []):
        vid = item.get("id", {}).get("videoId")
        snip = item.get("snippet", {})
        if not vid:
            continue
        thumbs = snip.get("thumbnails", {})
        thumb = (thumbs.get("default") or thumbs.get("medium") or {}).get("url", "")
        out.append(VideoResult(
            video_id=vid,
            title=sanitize_title(html.unescape(snip.get("title", "Untitled"))),
            channel=sanitize_title(html.unescape(snip.get("channelTitle", ""))),
            channel_id=str(snip.get("channelId", "")),
            thumbnail=thumb,
        ))
    enrich_youtube_details(settings, out)
    return out


def youtube_most_popular(settings: Settings, max_results: int = 12) -> list[VideoResult]:
    if not settings.api_key.strip():
        raise RuntimeError("Enter a YouTube Data API v3 key first.")
    params = {
        "part": "snippet,contentDetails",
        "chart": "mostPopular",
        "maxResults": str(max(1, min(50, int(max_results)))),
        "key": settings.api_key.strip(),
    }
    if settings.region_code.strip():
        params["regionCode"] = settings.region_code.strip().upper()
    url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body).get("error", {}).get("message", body)
        except Exception:
            detail = body
        raise RuntimeError(f"YouTube API error {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not contact YouTube API: {e.reason}") from e

    out: list[VideoResult] = []
    for item in payload.get("items", []):
        vid = item.get("id", "")
        snip = item.get("snippet", {})
        if not vid:
            continue
        thumbs = snip.get("thumbnails", {})
        thumb = (thumbs.get("default") or thumbs.get("medium") or {}).get("url", "")
        details = item.get("contentDetails", {})
        out.append(VideoResult(
            video_id=vid,
            title=sanitize_title(html.unescape(snip.get("title", "Untitled"))),
            channel=sanitize_title(html.unescape(snip.get("channelTitle", ""))),
            channel_id=str(snip.get("channelId", "")),
            thumbnail=thumb,
            duration_seconds=parse_iso8601_duration(details.get("duration", "")),
            definition=details.get("definition", "sd") if details.get("definition") in ("hd", "sd") else "sd",
        ))
    return out


def youtube_channel_search(settings: Settings, query: str, max_results: Optional[int] = None) -> list[ChannelResult]:
    """Search public YouTube channels with the official Data API."""
    if not settings.api_key.strip():
        raise RuntimeError("Enter a YouTube Data API v3 key first.")
    params = {
        "part": "snippet",
        "type": "channel",
        "q": query,
        "maxResults": str(max(1, min(50, int(settings.max_results if max_results is None else max_results)))),
        "key": settings.api_key.strip(),
    }
    if settings.region_code.strip():
        params["regionCode"] = settings.region_code.strip().upper()
    if settings.relevance_language.strip():
        params["relevanceLanguage"] = settings.relevance_language.strip()
    url = "https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body).get("error", {}).get("message", body)
        except Exception:
            detail = body
        raise RuntimeError(f"YouTube API error {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not contact YouTube API: {e.reason}") from e

    out: list[ChannelResult] = []
    for item in payload.get("items", []):
        channel_id = item.get("id", {}).get("channelId", "")
        snip = item.get("snippet", {})
        if not channel_id:
            continue
        thumbs = snip.get("thumbnails", {})
        thumb = (thumbs.get("default") or thumbs.get("medium") or {}).get("url", "")
        out.append(ChannelResult(
            channel_id=channel_id,
            title=sanitize_title(html.unescape(snip.get("title", "Untitled channel"))),
            thumbnail=thumb,
        ))
    return out


def youtube_channel_info(settings: Settings, channel_id: str, known_channel: Optional[ChannelResult] = None) -> ChannelResult:
    if known_channel is not None and known_channel.uploads_playlist:
        return ChannelResult.from_dict(asdict(known_channel))
    params = {
        "part": "snippet,contentDetails",
        "id": channel_id,
        "maxResults": "1",
        "key": settings.api_key.strip(),
    }
    url = "https://www.googleapis.com/youtube/v3/channels?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body).get("error", {}).get("message", body)
        except Exception:
            detail = body
        raise RuntimeError(f"YouTube API error {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not contact YouTube API: {e.reason}") from e
    items = payload.get("items", [])
    if not items:
        raise RuntimeError("YouTube channel no longer exists or is unavailable.")
    item = items[0]
    snip = item.get("snippet", {})
    thumbs = snip.get("thumbnails", {})
    thumb = (thumbs.get("default") or thumbs.get("medium") or {}).get("url", "")
    uploads = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
    return ChannelResult(
        channel_id=channel_id,
        title=sanitize_title(html.unescape(snip.get("title", known_channel.title if known_channel else "Channel"))),
        thumbnail=thumb or (known_channel.thumbnail if known_channel else ""),
        uploads_playlist=uploads,
    )


def youtube_channel_videos(
    settings: Settings,
    channel_id: str,
    page_token: str = "",
    max_results: Optional[int] = None,
    known_channel: Optional[ChannelResult] = None,
) -> tuple[list[VideoResult], str, ChannelResult]:
    """Return a page of newest uploads and the API nextPageToken."""
    if not settings.api_key.strip():
        raise RuntimeError("Enter a YouTube Data API v3 key first.")
    channel = youtube_channel_info(settings, channel_id, known_channel)
    if not channel.uploads_playlist:
        return [], "", channel
    params = {
        "part": "snippet,contentDetails",
        "playlistId": channel.uploads_playlist,
        "maxResults": str(max(1, min(50, int(settings.max_results if max_results is None else max_results)))),
        "key": settings.api_key.strip(),
    }
    if page_token:
        params["pageToken"] = page_token
    url = "https://www.googleapis.com/youtube/v3/playlistItems?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body).get("error", {}).get("message", body)
        except Exception:
            detail = body
        raise RuntimeError(f"YouTube API error {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not contact YouTube API: {e.reason}") from e

    out: list[VideoResult] = []
    for item in payload.get("items", []):
        snip = item.get("snippet", {})
        content = item.get("contentDetails", {})
        vid = content.get("videoId") or snip.get("resourceId", {}).get("videoId")
        title = sanitize_title(html.unescape(snip.get("title", "Untitled")))
        if not vid or title.casefold() in {"deleted video", "private video"}:
            continue
        thumbs = snip.get("thumbnails", {})
        thumb = (thumbs.get("default") or thumbs.get("medium") or {}).get("url", "")
        out.append(VideoResult(
            video_id=vid,
            title=title,
            channel=channel.title,
            channel_id=channel.channel_id,
            thumbnail=thumb,
        ))
    enrich_youtube_details(settings, out)
    return out, str(payload.get("nextPageToken", "") or ""), channel


def soap_envelope(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"<s:Body>{body}</s:Body></s:Envelope>"
    ).encode("utf-8")


def didl_wrap(inner: str) -> str:
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/" '
        'xmlns:av="urn:schemas-sony-com:av">'
        f"{inner}</DIDL-Lite>"
    )


def get_xml_text(root: ET.Element, local_name: str, default: str = "") -> str:
    for elem in root.iter():
        if elem.tag.rsplit("}", 1)[-1] == local_name:
            return elem.text or default
    return default


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DLNARequestHandler(http.server.BaseHTTPRequestHandler):
    server_version = f"PS3YouTubeDLNA/{APP_VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def version_string(self) -> str:
        return SERVER_HEADER

    @property
    def state(self) -> BridgeState:
        return self.server.bridge_state  # type: ignore[attr-defined]

    def _log_client_request(self, method: str, path: str) -> None:
        ua = self.headers.get("User-Agent", "")
        xav = self.headers.get("X-AV-Client-Info", "")
        soap = self.headers.get("SOAPAction", "")
        who = self.client_address[0] if self.client_address else "?"
        extra = []
        if ua:
            extra.append(f"UA={ua}")
        if xav:
            extra.append(f"X-AV={xav}")
        if soap:
            extra.append(f"SOAP={soap}")
        suffix = " | " + " | ".join(extra) if extra else ""
        self.state.log(f"DLNA {who}: {method} {path}{suffix}")

    def log_message(self, fmt: str, *args) -> None:
        # Keep GUI log useful; do not spam every thumbnail/HTTP detail.
        if self.path.startswith("/stream/"):
            self.state.log("HTTP: " + (fmt % args))

    def _send_bytes(self, code: int, content_type: str, body: bytes, extra: Optional[dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def do_HEAD(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/ytproxy/"):
            m = re.fullmatch(r"/ytproxy/([0-9a-f]{32})", path)
            if not m:
                self.send_error(404)
                return
            self._serve_youtube_source_proxy(m.group(1), head_only=True)
            return
        if path.startswith("/stream/"):
            m = re.fullmatch(r"/stream/([A-Za-z0-9_-]{6,20})\.mpg", path)
            if not m:
                self.send_error(404)
                return
            item = self.state.find_video(m.group(1))
            duration = int(item.duration_seconds) if item else 0
            seek_start = parse_npt_seconds(self.headers.get("TimeSeekRange.dlna.org", ""))
            self.send_response(200)
            self.send_header("Content-Type", "video/mpeg")
            self.send_header("Connection", "close")
            self.send_header("Accept-Ranges", "none")
            self.send_header("transferMode.dlna.org", "Streaming")
            self.send_header("contentFeatures.dlna.org", CONTENT_FEATURES)
            self.send_header("TimeSeekRange.dlna.org", npt_response(seek_start, duration))
            if self.headers.get("getAvailableSeekRange.dlna.org", "").strip() == "1":
                end = f"{float(duration):.3f}" if duration > 0 else ""
                self.send_header("availableSeekRange.dlna.org", f"1 npt=0.000-{end}")
            self.end_headers()
            self.close_connection = True
            return
        if path.startswith("/thumb/"):
            m = re.fullmatch(r"/thumb/([A-Za-z0-9_-]{6,20})\.jpg", path)
            if not m:
                self.send_error(404)
                return
            self._serve_thumbnail(m.group(1), head_only=True)
            return
        self.do_GET()

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/ytproxy/"):
            m = re.fullmatch(r"/ytproxy/([0-9a-f]{32})", path)
            if not m:
                self.send_error(404)
                return
            self._serve_youtube_source_proxy(m.group(1), head_only=False)
            return
        self._log_client_request("GET", path)
        if path in ("/", "/device.xml"):
            ip = self.state.settings.advertised_ip.strip() or detect_local_ip()
            body = DEVICE_XML.format(
                friendly_name=xml_escape(self.state.settings.friendly_name),
                version=APP_VERSION,
                udn=xml_escape(self.state.settings.udn),
                ip=xml_escape(ip),
                port=self.state.settings.port,
            ).encode("utf-8")
            self._send_bytes(200, 'text/xml; charset="utf-8"', body, {"EXT": ""})
        elif path == "/ContentDirectory/scpd.xml":
            self._send_bytes(200, 'text/xml; charset="utf-8"', CONTENT_DIRECTORY_SCPD.encode("utf-8"), {"EXT": ""})
        elif path == "/ConnectionManager/scpd.xml":
            self._send_bytes(200, 'text/xml; charset="utf-8"', CONNECTION_MANAGER_SCPD.encode("utf-8"), {"EXT": ""})
        elif path == "/X_MS_MediaReceiverRegistrar/scpd.xml":
            self._send_bytes(200, 'text/xml; charset="utf-8"', MEDIA_RECEIVER_SCPD.encode("utf-8"), {"EXT": ""})
        elif path.startswith("/stream/"):
            m = re.fullmatch(r"/stream/([A-Za-z0-9_-]{6,20})\.mpg", path)
            if not m:
                self.send_error(404)
                return
            self._stream_youtube(m.group(1))
        elif path.startswith("/thumb/"):
            m = re.fullmatch(r"/thumb/([A-Za-z0-9_-]{6,20})\.jpg", path)
            if not m:
                self.send_error(404)
                return
            self._serve_thumbnail(m.group(1), head_only=False)
        elif path == "/status.json":
            payload = json.dumps({
                "app": APP_NAME,
                "version": APP_VERSION,
                "results": [asdict(x) for x in self.state.snapshot_results()],
                "query": self.state.current_query,
                "favorites": [asdict(x) for x in self.state.snapshot_favorites()],
                "history": [{"id": e["id"], "query": e["query"], "count": len(e["results"])} for e in self.state.snapshot_history()],
                "update_id": self.state.update_id,
            }, ensure_ascii=False, indent=2).encode("utf-8")
            self._send_bytes(200, "application/json; charset=utf-8", payload)
        else:
            self.send_error(404)

    def _serve_youtube_source_proxy(self, token: str, head_only: bool = False) -> None:
        source = self.state.get_youtube_source(token)
        if source is None:
            self.send_error(404, "Expired YouTube source")
            return

        source_url = str(source.get("url") or "")
        source_headers = dict(source.get("headers") or {})
        if not source_url:
            self.send_error(404, "Missing YouTube source")
            return

        range_header = self.headers.get("Range", "").strip()
        requested_start = 0
        requested_end: Optional[int] = None
        if range_header:
            m = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
            if not m:
                self.send_error(416, "Unsupported Range")
                return
            requested_start = int(m.group(1))
            if m.group(2):
                requested_end = int(m.group(2))
                if requested_end < requested_start:
                    self.send_error(416, "Invalid Range")
                    return

        def open_range(start: int, end: int):
            headers = dict(source_headers)
            headers["Range"] = f"bytes={start}-{end}"
            headers["Accept-Encoding"] = "identity"
            req = urllib.request.Request(source_url, headers=headers)
            return urllib.request.urlopen(req, timeout=20)

        first_end = requested_start + YOUTUBE_PROXY_CHUNK_BYTES - 1
        if requested_end is not None:
            first_end = min(first_end, requested_end)

        first_response = None
        last_error: Optional[Exception] = None
        for attempt in range(YOUTUBE_PROXY_RETRIES):
            try:
                first_response = open_range(requested_start, first_end)
                break
            except Exception as e:
                last_error = e
                if attempt + 1 < YOUTUBE_PROXY_RETRIES:
                    time.sleep(min(0.25 * (2 ** attempt), 2.0))
        if first_response is None:
            self.state.log(f"YouTube chunk proxy could not open source: {last_error}")
            self.send_error(502, "Could not read YouTube source")
            return

        try:
            content_range = first_response.headers.get("Content-Range", "")
            cr = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range.strip(), re.I)
            total_size: Optional[int] = None
            actual_start = requested_start
            actual_end = first_end
            if cr:
                actual_start = int(cr.group(1))
                actual_end = int(cr.group(2))
                if cr.group(3) != "*":
                    total_size = int(cr.group(3))
            else:
                # GoogleVideo normally honours Range. A full 200 response would
                # defeat the anti-throttling proxy, so only accept it when the
                # source is genuinely smaller than one bounded chunk.
                content_len = first_response.headers.get("Content-Length")
                if content_len and requested_start == 0:
                    total_size = int(content_len)
                    actual_end = max(0, total_size - 1)
                if getattr(first_response, "status", 200) != 206 and (total_size or 0) > YOUTUBE_PROXY_CHUNK_BYTES:
                    raise RuntimeError("Upstream ignored bounded Range request")

            if actual_start != requested_start:
                raise RuntimeError(f"Unexpected upstream range start {actual_start}, wanted {requested_start}")

            final_end = requested_end
            if total_size is not None:
                last_byte = max(0, total_size - 1)
                final_end = min(final_end, last_byte) if final_end is not None else last_byte
            if final_end is None:
                final_end = actual_end

            response_code = 206 if range_header else 200
            self.send_response(response_code)
            self.send_header("Content-Type", first_response.headers.get("Content-Type", "application/octet-stream"))
            self.send_header("Accept-Ranges", "bytes")
            if total_size is not None:
                self.send_header("Content-Length", str(max(0, final_end - requested_start + 1)))
                if response_code == 206:
                    self.send_header("Content-Range", f"bytes {requested_start}-{final_end}/{total_size}")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if head_only:
                return

            current = requested_start

            def relay_response(resp, expected_end: int) -> int:
                nonlocal current
                while current <= expected_end:
                    data = resp.read(min(256 * 1024, expected_end - current + 1))
                    if not data:
                        break
                    self.wfile.write(data)
                    current += len(data)
                return current

            # Relay the already-open first bounded request.
            relay_response(first_response, min(actual_end, final_end))
            try:
                first_response.close()
            except Exception:
                pass
            first_response = None

            # Continue with bounded 8 MiB requests. Each individual request is
            # comfortably below YouTube's ~10 MiB throttling threshold.
            while current <= final_end:
                chunk_end = min(current + YOUTUBE_PROXY_CHUNK_BYTES - 1, final_end)
                completed = False
                for attempt in range(YOUTUBE_PROXY_RETRIES):
                    resp = None
                    try:
                        resp = open_range(current, chunk_end)
                        before = current
                        relay_response(resp, chunk_end)
                        if current > chunk_end:
                            completed = True
                            break
                        if current == before:
                            raise RuntimeError("empty upstream range response")
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        raise
                    except Exception as e:
                        last_error = e
                        if attempt + 1 < YOUTUBE_PROXY_RETRIES:
                            time.sleep(min(0.25 * (2 ** attempt), 2.0))
                    finally:
                        if resp is not None:
                            try:
                                resp.close()
                            except Exception:
                                pass
                if not completed:
                    raise RuntimeError(f"YouTube range retry limit reached near byte {current}: {last_error}")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            self.state.log(f"YouTube chunk proxy error: {e}")
        finally:
            if first_response is not None:
                try:
                    first_response.close()
                except Exception:
                    pass

    def _serve_thumbnail(self, video_id: str, head_only: bool = False) -> None:
        item = self.state.find_video(video_id)
        if item is None or not item.thumbnail:
            self.send_error(404, "Thumbnail unavailable")
            return
        THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = THUMB_CACHE_DIR / f"{video_id}.jpg"
        data = b""
        try:
            if cache_path.exists() and cache_path.stat().st_size > 0:
                data = cache_path.read_bytes()
            else:
                req = urllib.request.Request(item.thumbnail, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as response:
                    data = response.read(2 * 1024 * 1024)
                if not data:
                    raise RuntimeError("empty thumbnail")
                cache_path.write_bytes(data)
        except Exception as e:
            self.state.log(f"Thumbnail fetch failed for {video_id}: {e}")
            self.send_error(502, "Could not fetch thumbnail")
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Connection", "close")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)
        self.close_connection = True

    def do_SUBSCRIBE(self) -> None:
        self._log_client_request("SUBSCRIBE", self.path)
        if not self.path.startswith("/ContentDirectory/event"):
            self.send_error(412, "Unsupported event subscription")
            return

        requested_sid = self.headers.get("SID", "").strip()
        timeout_header = self.headers.get("TIMEOUT", "Second-1800")
        is_new = not requested_sid
        if requested_sid:
            timeout = self.state.renew_subscriber(requested_sid, timeout_header)
            if timeout is None:
                self.send_error(412, "Unknown SID")
                return
            sid = requested_sid
        else:
            callback_header = self.headers.get("CALLBACK", "").strip()
            match = re.search(r"<([^>]+)>", callback_header)
            callback = match.group(1).strip() if match else ""
            if not callback.startswith(("http://", "https://")):
                self.send_error(412, "Missing CALLBACK")
                return
            if self.headers.get("NT", "").lower() != "upnp:event":
                self.send_error(412, "Invalid NT")
                return
            sid, timeout = self.state.add_subscriber(callback, timeout_header)

        self.send_response(200)
        self.send_header("SID", sid)
        self.send_header("TIMEOUT", f"Second-{timeout}")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if is_new:
            # UPnP event subscribers expect an initial state notification.
            threading.Timer(0.05, lambda: self.state.notify_content_changed(only_sid=sid)).start()

    def do_UNSUBSCRIBE(self) -> None:
        self._log_client_request("UNSUBSCRIBE", self.path)
        sid = self.headers.get("SID", "").strip()
        if sid:
            self.state.remove_subscriber(sid)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_POST(self) -> None:
        self._log_client_request("POST", self.path)
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        action_header = self.headers.get("SOAPAction", "").strip('"')
        action = action_header.rsplit("#", 1)[-1] if "#" in action_header else ""
        if not action and raw:
            try:
                root = ET.fromstring(raw)
                for elem in root.iter():
                    local = elem.tag.rsplit("}", 1)[-1]
                    if local not in {"Envelope", "Body"}:
                        action = local
                        break
            except ET.ParseError:
                pass

        if self.path.startswith("/ContentDirectory/"):
            self._content_directory(action, raw)
        elif self.path.startswith("/ConnectionManager/"):
            self._connection_manager(action)
        elif self.path.startswith("/X_MS_MediaReceiverRegistrar/"):
            self._media_receiver_registrar(action)
        else:
            self.send_error(404)

    def _content_directory(self, action: str, raw: bytes) -> None:
        if action == "Browse":
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                self.send_error(400, "Bad SOAP XML")
                return
            object_id = get_xml_text(root, "ObjectID", "0")
            browse_flag = get_xml_text(root, "BrowseFlag", "BrowseDirectChildren")
            browse_filter = get_xml_text(root, "Filter", "*")
            try:
                start = max(0, int(get_xml_text(root, "StartingIndex", "0")))
                count = max(0, int(get_xml_text(root, "RequestedCount", "0")))
            except ValueError:
                start, count = 0, 0
            self.state.log(
                f"Browse ObjectID={object_id!r} Flag={browse_flag} "
                f"Start={start} Count={count} Filter={browse_filter!r}"
            )
            didl, number_returned, total = self._browse(object_id, browse_flag, start, count)
            self.state.log(f"Browse reply ObjectID={object_id!r}: {number_returned}/{total} object(s)")
            escaped_didl = xml_escape(didl)
            body = (
                '<u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
                f"<Result>{escaped_didl}</Result>"
                f"<NumberReturned>{number_returned}</NumberReturned>"
                f"<TotalMatches>{total}</TotalMatches>"
                f"<UpdateID>{self.state.update_id}</UpdateID>"
                "</u:BrowseResponse>"
            )
            self._send_bytes(200, 'text/xml; charset="utf-8"', soap_envelope(body), {
                "EXT": "",
            })
        elif action == "GetSystemUpdateID":
            body = (
                '<u:GetSystemUpdateIDResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
                f"<Id>{self.state.update_id}</Id>"
                "</u:GetSystemUpdateIDResponse>"
            )
            self._send_bytes(200, 'text/xml; charset="utf-8"', soap_envelope(body), {"EXT": ""})
        elif action == "GetSearchCapabilities":
            body = (
                '<u:GetSearchCapabilitiesResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
                "<SearchCaps></SearchCaps></u:GetSearchCapabilitiesResponse>"
            )
            self._send_bytes(200, 'text/xml; charset="utf-8"', soap_envelope(body), {"EXT": ""})
        elif action == "GetSortCapabilities":
            body = (
                '<u:GetSortCapabilitiesResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
                "<SortCaps></SortCaps></u:GetSortCapabilitiesResponse>"
            )
            self._send_bytes(200, 'text/xml; charset="utf-8"', soap_envelope(body), {"EXT": ""})
        else:
            self._soap_fault(401, f"Unsupported ContentDirectory action: {action}")

    def _connection_manager(self, action: str) -> None:
        if action == "GetProtocolInfo":
            body = (
                '<u:GetProtocolInfoResponse xmlns:u="urn:schemas-upnp-org:service:ConnectionManager:1">'
                f"<Source>{xml_escape(PROTOCOL_INFO + "," + PROTOCOL_INFO_DETAILED)}</Source><Sink></Sink>"
                "</u:GetProtocolInfoResponse>"
            )
        elif action == "GetCurrentConnectionIDs":
            body = (
                '<u:GetCurrentConnectionIDsResponse xmlns:u="urn:schemas-upnp-org:service:ConnectionManager:1">'
                "<ConnectionIDs>0</ConnectionIDs></u:GetCurrentConnectionIDsResponse>"
            )
        elif action == "GetCurrentConnectionInfo":
            body = (
                '<u:GetCurrentConnectionInfoResponse xmlns:u="urn:schemas-upnp-org:service:ConnectionManager:1">'
                "<RcsID>-1</RcsID><AVTransportID>-1</AVTransportID>"
                f"<ProtocolInfo>{xml_escape(PROTOCOL_INFO)}</ProtocolInfo>"
                "<PeerConnectionManager></PeerConnectionManager><PeerConnectionID>-1</PeerConnectionID>"
                "<Direction>Output</Direction><Status>OK</Status>"
                "</u:GetCurrentConnectionInfoResponse>"
            )
        else:
            self._soap_fault(401, f"Unsupported ConnectionManager action: {action}")
            return
        self._send_bytes(200, 'text/xml; charset="utf-8"', soap_envelope(body), {"EXT": ""})

    def _media_receiver_registrar(self, action: str) -> None:
        ns = "urn:microsoft.com:service:X_MS_MediaReceiverRegistrar:1"
        if action in ("IsAuthorized", "IsValidated"):
            body = f'<u:{action}Response xmlns:u="{ns}"><Result>1</Result></u:{action}Response>'
        elif action == "RegisterDevice":
            body = f'<u:RegisterDeviceResponse xmlns:u="{ns}"><RegistrationRespMsg></RegistrationRespMsg></u:RegisterDeviceResponse>'
        else:
            self._soap_fault(401, "Invalid Action")
            return
        self._send_bytes(200, 'text/xml; charset="utf-8"', soap_envelope(body), {"EXT": ""})

    def _soap_fault(self, code: int, description: str) -> None:
        body = (
            '<s:Fault>'
            '<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>'
            '<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
            f'<errorCode>{int(code)}</errorCode><errorDescription>{xml_escape(description)}</errorDescription>'
            '</UPnPError></detail></s:Fault>'
        )
        self._send_bytes(500, 'text/xml; charset="utf-8"', soap_envelope(body), {"EXT": ""})

    def _browse(self, object_id: str, browse_flag: str, start: int, count: int) -> tuple[str, int, int]:
        ip = self.state.settings.advertised_ip.strip() or detect_local_ip()
        port = self.state.settings.port

        def container_xml(cid: str, parent: str, title: str, child_count: int, media_class: bool = True) -> str:
            extra = '<av:mediaClass>V</av:mediaClass>' if media_class else ''
            return (
                f'<container id="{xml_escape(cid)}" parentID="{xml_escape(parent)}" restricted="1" searchable="0" childCount="{child_count}">'
                f'<dc:title>{xml_escape(title)}</dc:title>'
                '<upnp:class>object.container.storageFolder</upnp:class>'
                f'{extra}<upnp:storageUsed>-1</upnp:storageUsed>'
                '</container>'
            )

        def video_xml(item: VideoResult, parent_id: str, item_id: str) -> str:
            url = f"http://{ip}:{port}/stream/{urllib.parse.quote(item.video_id)}.mpg"
            thumb_url = f"http://{ip}:{port}/thumb/{urllib.parse.quote(item.video_id)}.jpg"
            title = item.title
            if item.channel:
                title = f"{title}  —  {item.channel}"
            width, height = target_resolution(item, int(self.state.settings.max_height))
            duration = dlna_duration(item.duration_seconds)
            res_attrs = (
                f'protocolInfo="{xml_escape(PROTOCOL_INFO)}" '
                f'size="-1" duration="{duration}" resolution="{width}x{height}" '
                'bitrate="1500000" nrAudioChannels="2" sampleFrequency="48000"'
            )
            pieces = [
                f'<item id="{xml_escape(item_id)}" parentID="{xml_escape(parent_id)}" restricted="1" dlna:dlnaManaged="00000000">',
                f'<dc:title>{xml_escape(title)}</dc:title>',
                '<upnp:class>object.item.videoItem</upnp:class>',
                '<av:mediaClass>V</av:mediaClass>',
                f'<dc:creator>{xml_escape(item.channel)}</dc:creator>' if item.channel else '',
                f'<upnp:albumArtURI dlna:profileID="JPEG_TN">{xml_escape(thumb_url)}</upnp:albumArtURI>' if item.thumbnail else '',
                f'<res {res_attrs}>{xml_escape(url)}</res>',
                '</item>',
            ]
            return ''.join(pieces)

        def page(items: list, renderer) -> tuple[str, int, int]:
            total = len(items)
            subset = items[start:] if count == 0 else items[start:start + count]
            return didl_wrap(''.join(renderer(x) for x in subset)), len(subset), total

        def encode_query(query: str) -> str:
            raw = query.encode('utf-8')
            token = base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')
            return token or '_'

        def decode_query(token: str) -> str:
            if not token or token == '_':
                return ''
            try:
                token += '=' * ((4 - len(token) % 4) % 4)
                return base64.urlsafe_b64decode(token.encode('ascii')).decode('utf-8')[:PS3_SEARCH_MAX_CHARS]
            except Exception:
                return ''

        def keyboard_object(query: str) -> str:
            return f"ps3kbd:{encode_query(query)}"

        def keyboard_parent(query: str) -> str:
            if not query:
                return 'ps3search'
            return keyboard_object(query[:-1]) if len(query) > 1 else keyboard_object('')

        def keyboard_choices(query: str) -> list[tuple[str, str, int]]:
            choices: list[tuple[str, str, int]] = []
            clean = query[:PS3_SEARCH_MAX_CHARS]
            # Keep a one-click reset at the very top of every non-empty typing
            # level.  The target is the canonical empty keyboard container, so
            # the PS3 immediately sees A-Z/0-9 again without backing through
            # every character folder.
            if clean:
                choices.append((keyboard_object(''), "↩ NEW SEARCH", 38))
            if clean.strip():
                run_token = self.state.register_ps3_query(clean)
                choices.append((f"ps3run:{run_token}", f"ENTER / SEARCH: {clean}", 51))
            if clean:
                choices.append((keyboard_object(clean[:-1]), "⌫ DELETE LAST", 43))
            if clean and not clean.endswith(' ') and len(clean) < PS3_SEARCH_MAX_CHARS:
                choices.append((keyboard_object(clean + ' '), "␠ SPACE", 43))
            elif not clean:
                # Prevent leading spaces; show SPACE only once text exists.
                pass
            if len(clean) < PS3_SEARCH_MAX_CHARS:
                for ch in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
                    choices.append((keyboard_object(clean + ch), ch, 43))
                for ch in '0123456789':
                    choices.append((keyboard_object(clean + ch), ch, 43))
                choices.append((keyboard_object(clean + '-'), '-', 43))
                choices.append((keyboard_object(clean + "'"), "'", 43))
            return choices

        def channel_keyboard_object(query: str) -> str:
            return f"chkbd:{encode_query(query)}"

        def channel_keyboard_parent(query: str) -> str:
            if not query:
                return 'chsearch'
            return channel_keyboard_object(query[:-1]) if len(query) > 1 else channel_keyboard_object('')

        def channel_keyboard_choices(query: str) -> list[tuple[str, str, int]]:
            choices: list[tuple[str, str, int]] = []
            clean = query[:PS3_SEARCH_MAX_CHARS]
            if clean:
                choices.append((channel_keyboard_object(''), "↩ NEW CHANNEL SEARCH", 38))
            if clean.strip():
                run_token = self.state.register_channel_query(clean)
                choices.append((f"chrun:{run_token}", f"ENTER / FIND CHANNEL: {clean}", self.state.settings.max_results + 1))
            if clean:
                choices.append((channel_keyboard_object(clean[:-1]), "⌫ DELETE LAST", 43))
            if clean and not clean.endswith(' ') and len(clean) < PS3_SEARCH_MAX_CHARS:
                choices.append((channel_keyboard_object(clean + ' '), "␠ SPACE", 43))
            if len(clean) < PS3_SEARCH_MAX_CHARS:
                for ch in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
                    choices.append((channel_keyboard_object(clean + ch), ch, 43))
                for ch in '0123456789':
                    choices.append((channel_keyboard_object(clean + ch), ch, 43))
                choices.append((channel_keyboard_object(clean + '-'), '-', 43))
                choices.append((channel_keyboard_object(clean + "'"), "'", 43))
            return choices

        current = self.state.snapshot_results()
        favorites = self.state.snapshot_favorites()
        subscriptions = self.state.snapshot_channel_subscriptions()
        history = self.state.snapshot_history()
        home = self.state.snapshot_home()
        popular = self.state.snapshot_popular()
        recommendations_on = bool(self.state.settings.recommendations_enabled)

        if object_id == "0":
            root_children = []
            if recommendations_on:
                root_children.append(("home", f"Home ({len(home)})", len(home)))
            root_children.extend([
                ("ps3search", "Search Videos on PS3", 38),
                ("chsearch", "Search Channels on PS3", 38),
                ("yt", self.state.library_title(), len(current)),
                ("fav", f"Favorites ({len(favorites)})", len(favorites)),
                ("channelsubs", f"Subscriptions ({len(subscriptions)})", len(subscriptions)),
                ("history", f"Search History ({len(history)})", len(history)),
            ])
            if browse_flag == "BrowseMetadata":
                inner = (
                    f'<container id="0" parentID="-1" restricted="1" searchable="0" childCount="{len(root_children)}">'
                    '<dc:title>Root</dc:title><upnp:class>object.container</upnp:class>'
                    '<upnp:storageUsed>-1</upnp:storageUsed></container>'
                )
                return didl_wrap(inner), 1, 1
            return page(root_children, lambda x: container_xml(x[0], "0", x[1], x[2], x[0] != "history"))

        if object_id == "home":
            if not recommendations_on:
                return didl_wrap(""), 0, 0
            # Refresh lazily. If we have a cached Home, serve it instantly and
            # refresh stale recommendations in the background.
            if self.state.home_is_stale():
                if home:
                    threading.Thread(target=self.state.refresh_home, daemon=True, name="HomeRefresh").start()
                else:
                    try:
                        home = self.state.refresh_home()
                        popular = self.state.snapshot_popular()
                    except Exception as e:
                        self.state.log(f"Home refresh failed: {e}")
            home = self.state.snapshot_home()
            if browse_flag == "BrowseMetadata":
                return didl_wrap(container_xml("home", "0", f"Home ({len(home)})", len(home) + 1)), 1, 1
            children: list[tuple[str, object]] = [("popular", None)] + [("video", item) for item in home]
            total = len(children)
            subset = children[start:] if count == 0 else children[start:start + count]
            rendered = []
            for kind, payload in subset:
                if kind == "popular":
                    rendered.append(container_xml("popular", "home", f"Popular Right Now ({len(popular)})", len(popular)))
                else:
                    item = payload
                    rendered.append(video_xml(item, "home", f"home:{item.video_id}"))
            return didl_wrap(''.join(rendered)), len(subset), total

        if object_id == "popular":
            if not recommendations_on:
                return didl_wrap(""), 0, 0
            if not popular:
                try:
                    self.state.refresh_home()
                except Exception as e:
                    self.state.log(f"Popular refresh failed: {e}")
                popular = self.state.snapshot_popular()
            if browse_flag == "BrowseMetadata":
                return didl_wrap(container_xml("popular", "home", f"Popular Right Now ({len(popular)})", len(popular))), 1, 1
            return page(popular, lambda item: video_xml(item, "popular", f"popular:{item.video_id}"))

        if object_id == "ps3search":
            if browse_flag == "BrowseMetadata":
                return didl_wrap(container_xml("ps3search", "0", "Search Videos on PS3", 38)), 1, 1
            choices = keyboard_choices('')
            return page(choices, lambda x: container_xml(x[0], "ps3search", x[1], x[2]))

        if object_id.startswith("ps3kbd:"):
            token = object_id.split(':', 1)[1]
            query = decode_query(token)
            choices = keyboard_choices(query)
            if browse_flag == "BrowseMetadata":
                shown = query if query else "(empty)"
                return didl_wrap(container_xml(object_id, keyboard_parent(query), f"Search: {shown}", len(choices))), 1, 1
            return page(choices, lambda x: container_xml(x[0], object_id, x[1], x[2]))

        if object_id.startswith("ps3run:") and object_id.count(':') == 1:
            token = object_id.split(':', 1)[1]
            query = self.state.resolve_ps3_query(token).strip()
            if browse_flag == "BrowseMetadata":
                if not query:
                    # If the XMB reuses a stale ENTER ObjectID after a server
                    # restart, present a harmless empty results container rather
                    # than returning malformed metadata.
                    return didl_wrap(container_xml(object_id, "ps3search", "Search results", 0)), 1, 1
                # Up to 50 videos plus the always-present NEW SEARCH shortcut.
                return didl_wrap(container_xml(object_id, keyboard_object(query), f"Results: {query}", 51)), 1, 1
            if not query:
                self.state.log(f"PS3 requested unknown/stale search token {token!r}.")
                return didl_wrap(""), 0, 0
            try:
                results = self.state.run_ps3_search(query)
            except Exception as e:
                self.state.log(f"PS3 search failed for {query!r}: {e}")
                err = [("ps3search-error", f"SEARCH FAILED — check PC log", 0)]
                return page(err, lambda x: container_xml(x[0], object_id, x[1], x[2], False))
            # Put NEW SEARCH at the very top of the results folder.  It points
            # straight back to the empty keyboard, while the video items keep
            # their compact ObjectIDs.  Include the shortcut in pagination so
            # the PS3 sees it as item #1, followed by the videos.
            result_children: list[tuple[str, object]] = [("new", None)]
            result_children.extend(("video", item) for item in results)
            total = len(result_children)
            subset = result_children[start:] if count == 0 else result_children[start:start + count]
            rendered: list[str] = []
            for kind, payload in subset:
                if kind == "new":
                    rendered.append(container_xml(keyboard_object(''), object_id, "↩ NEW SEARCH", 38))
                else:
                    item = payload
                    rendered.append(video_xml(item, object_id, f"pv:{token}:{item.video_id}"))
            return didl_wrap(''.join(rendered)), len(subset), total

        if object_id == "chsearch":
            if browse_flag == "BrowseMetadata":
                return didl_wrap(container_xml("chsearch", "0", "Search Channels on PS3", 38)), 1, 1
            choices = channel_keyboard_choices('')
            return page(choices, lambda x: container_xml(x[0], "chsearch", x[1], x[2]))

        if object_id.startswith("chkbd:"):
            token = object_id.split(':', 1)[1]
            query = decode_query(token)
            choices = channel_keyboard_choices(query)
            if browse_flag == "BrowseMetadata":
                shown = query if query else "(empty)"
                return didl_wrap(container_xml(object_id, channel_keyboard_parent(query), f"Channel search: {shown}", len(choices))), 1, 1
            return page(choices, lambda x: container_xml(x[0], object_id, x[1], x[2]))

        if object_id.startswith("chrun:") and object_id.count(':') == 1:
            token = object_id.split(':', 1)[1]
            query = self.state.resolve_channel_query(token).strip()
            if browse_flag == "BrowseMetadata":
                return didl_wrap(container_xml(object_id, "chsearch", f"Channels: {query or 'Search results'}", self.state.settings.max_results + 1)), 1, 1
            if not query:
                return didl_wrap(""), 0, 0
            try:
                channels = self.state.run_channel_search(query)
            except Exception as e:
                self.state.log(f"PS3 channel search failed for {query!r}: {e}")
                err = [("channel-search-error", "CHANNEL SEARCH FAILED — check PC log", 0)]
                return page(err, lambda x: container_xml(x[0], object_id, x[1], x[2], False))
            children: list[tuple[str, object]] = [("new", None)] + [("channel", ch) for ch in channels]
            total = len(children)
            subset = children[start:] if count == 0 else children[start:start + count]
            rendered = []
            for kind, payload in subset:
                if kind == "new":
                    rendered.append(container_xml(channel_keyboard_object(''), object_id, "↩ NEW CHANNEL SEARCH", 38))
                else:
                    ch = payload
                    rendered.append(container_xml(f"chan:{ch.channel_id}", object_id, ch.title, self.state.settings.max_results + 2))
            return didl_wrap(''.join(rendered)), len(subset), total

        if object_id == "channelsubs":
            if browse_flag == "BrowseMetadata":
                return didl_wrap(container_xml("channelsubs", "0", f"Subscriptions ({len(subscriptions)})", len(subscriptions))), 1, 1
            return page(subscriptions, lambda ch: container_xml(f"chan:{ch.channel_id}", "channelsubs", ch.title, self.state.settings.max_results + 2))

        if object_id.startswith("chan:") and object_id.count(':') == 1:
            channel_id = object_id.split(':', 1)[1]
            known = self.state.find_channel(channel_id)
            title = known.title if known else "Channel"
            if browse_flag == "BrowseMetadata":
                return didl_wrap(container_xml(object_id, "channelsubs" if self.state.is_channel_subscribed(channel_id) else "chsearch", title, self.state.settings.max_results + 2)), 1, 1
            try:
                videos, next_page, channel = self.state.get_channel_page(channel_id, "")
                self.state.record_channel_visit(channel)
            except Exception as e:
                self.state.log(f"Could not open channel {channel_id}: {e}")
                return didl_wrap(container_xml("channel-open-error", object_id, "CHANNEL LOAD FAILED — check PC log", 0, False)), 1, 1
            action_id = ("unsub:" if self.state.is_channel_subscribed(channel_id) else "sub:") + channel_id
            action_title = "✓ SUBSCRIBED — UNSUBSCRIBE" if self.state.is_channel_subscribed(channel_id) else "★ SUBSCRIBE TO CHANNEL"
            children: list[tuple[str, object]] = [("action", (action_id, action_title))] + [("video", item) for item in videos]
            if next_page:
                page_token = self.state.register_channel_page(channel_id, next_page)
                children.append(("next", page_token))
            total = len(children)
            subset = children[start:] if count == 0 else children[start:start + count]
            rendered = []
            for kind, payload in subset:
                if kind == "action":
                    aid, atitle = payload
                    rendered.append(container_xml(aid, object_id, atitle, 1))
                elif kind == "next":
                    rendered.append(container_xml(f"chpage:{payload}", object_id, "NEXT PAGE ▶", self.state.settings.max_results + 1))
                else:
                    item = payload
                    rendered.append(video_xml(item, object_id, f"cv:{channel_id}:{item.video_id}"))
            return didl_wrap(''.join(rendered)), len(subset), total

        if object_id.startswith("chpage:") and object_id.count(':') == 1:
            token = object_id.split(':', 1)[1]
            channel_id, page_token = self.state.resolve_channel_page(token)
            if not channel_id:
                return didl_wrap(""), 0, 0
            known = self.state.find_channel(channel_id)
            title = known.title if known else "Channel"
            if browse_flag == "BrowseMetadata":
                return didl_wrap(container_xml(object_id, f"chan:{channel_id}", f"{title} — next videos", self.state.settings.max_results + 1)), 1, 1
            try:
                videos, next_page, channel = self.state.get_channel_page(channel_id, page_token)
            except Exception as e:
                self.state.log(f"Could not load next channel page {channel_id}: {e}")
                return didl_wrap(""), 0, 0
            children: list[tuple[str, object]] = [("video", item) for item in videos]
            if next_page:
                next_token = self.state.register_channel_page(channel_id, next_page)
                children.append(("next", next_token))
            total = len(children)
            subset = children[start:] if count == 0 else children[start:start + count]
            rendered = []
            for kind, payload in subset:
                if kind == "next":
                    rendered.append(container_xml(f"chpage:{payload}", object_id, "NEXT PAGE ▶", self.state.settings.max_results + 1))
                else:
                    item = payload
                    rendered.append(video_xml(item, object_id, f"cv:{channel_id}:{item.video_id}"))
            return didl_wrap(''.join(rendered)), len(subset), total

        if (object_id.startswith("sub:") or object_id.startswith("unsub:")) and object_id.count(':') == 1:
            action, channel_id = object_id.split(':', 1)
            channel = self.state.find_channel(channel_id)
            if channel is None:
                return didl_wrap(""), 0, 0
            if browse_flag == "BrowseMetadata":
                title = "Subscribe" if action == "sub" else "Unsubscribe"
                return didl_wrap(container_xml(object_id, f"chan:{channel_id}", title, 1)), 1, 1
            if action == "sub":
                # Resolve channel details so the uploads playlist is saved too.
                try:
                    _videos, _next, channel = self.state.get_channel_page(channel_id, "")
                except Exception:
                    pass
                self.state.add_channel_subscription(channel)
                msg = "✓ SUBSCRIBED — press CIRCLE to return"
            else:
                self.state.remove_channel_subscription(channel_id)
                msg = "UNSUBSCRIBED — press CIRCLE to return"
            child = container_xml("channel-action-done", object_id, msg, 0, False)
            return didl_wrap(child), 1, 1

        if object_id in {"channel-search-error", "channel-open-error", "channel-action-done"}:
            return didl_wrap(container_xml(object_id, "chsearch", "Status", 0, False)), 1, 1

        if object_id == "ps3search-error":
            return didl_wrap(container_xml("ps3search-error", "ps3search", "SEARCH FAILED — check PC log", 0, False)), 1, 1

        if object_id == "yt":
            if browse_flag == "BrowseMetadata":
                return didl_wrap(container_xml("yt", "0", self.state.library_title(), len(current))), 1, 1
            return page(current, lambda item: video_xml(item, "yt", f"yt:{item.video_id}"))

        if object_id == "fav":
            if browse_flag == "BrowseMetadata":
                return didl_wrap(container_xml("fav", "0", f"Favorites ({len(favorites)})", len(favorites))), 1, 1
            return page(favorites, lambda item: video_xml(item, "fav", f"fav:{item.video_id}"))

        if object_id == "history":
            if browse_flag == "BrowseMetadata":
                return didl_wrap(container_xml("history", "0", f"Search History ({len(history)})", len(history), False)), 1, 1
            return page(
                history,
                lambda entry: container_xml(
                    f"hist:{entry['id']}",
                    "history",
                    f"{entry['query']} ({len(entry['results'])})",
                    len(entry['results']),
                ),
            )

        if object_id.startswith("hist:") and object_id.count(":") == 1:
            entry_id = object_id.split(":", 1)[1]
            entry = self.state.history_entry(entry_id)
            if not entry:
                return didl_wrap(""), 0, 0
            hist_results = entry.get("results", [])
            if browse_flag == "BrowseMetadata":
                return didl_wrap(container_xml(object_id, "history", f"{entry['query']} ({len(hist_results)})", len(hist_results))), 1, 1
            return page(hist_results, lambda item: video_xml(item, object_id, f"{object_id}:{item.video_id}"))

        # Item metadata can be requested from every library area, including a
        # search launched entirely from the PS3 keyboard-folder interface.
        parent_id = None
        video_id = None
        if object_id.startswith("home:"):
            parent_id, video_id = "home", object_id.split(":", 1)[1]
        elif object_id.startswith("popular:"):
            parent_id, video_id = "popular", object_id.split(":", 1)[1]
        elif object_id.startswith("yt:"):
            parent_id, video_id = "yt", object_id.split(":", 1)[1]
        elif object_id.startswith("fav:"):
            parent_id, video_id = "fav", object_id.split(":", 1)[1]
        elif object_id.startswith("hist:") and object_id.count(":") >= 2:
            parts = object_id.split(":")
            parent_id = ":".join(parts[:2])
            video_id = parts[-1]
        elif object_id.startswith("cv:") and object_id.count(":") == 2:
            _, channel_id, video_id = object_id.split(":", 2)
            parent_id = f"chan:{channel_id}"
        elif object_id.startswith("pv:") and object_id.count(":") == 2:
            _, token, video_id = object_id.split(":", 2)
            parent_id = f"ps3run:{token}"
        elif object_id.startswith("ps3item:") and object_id.count(":") == 2:
            # Backward compatibility for ObjectIDs cached from v1.0/v1.0.1.
            _, token, video_id = object_id.split(":", 2)
            parent_id = f"ps3run:{token}"

        if parent_id and video_id:
            match = self.state.find_video(video_id)
            if not match:
                return didl_wrap(""), 0, 0
            return didl_wrap(video_xml(match, parent_id, object_id)), 1, 1

        return didl_wrap(""), 0, 0

    def _stream_youtube(self, video_id: str) -> None:
        seek_header = self.headers.get("TimeSeekRange.dlna.org", "").strip()
        seek_start = parse_npt_seconds(seek_header)
        item_meta = self.state.find_video(video_id)
        duration_seconds = int(item_meta.duration_seconds) if item_meta else 0
        if duration_seconds > 0:
            seek_start = min(seek_start, max(0.0, duration_seconds - 0.25))
        if yt_dlp is None:
            self.state.log("Cannot stream: yt-dlp is not installed.")
            self.send_error(500, "yt-dlp is not installed")
            return
        ffmpeg = find_ffmpeg(self.state.settings.ffmpeg_path)
        if not ffmpeg:
            self.state.log("Cannot stream: FFmpeg was not found.")
            self.send_error(500, "FFmpeg not found")
            return

        if seek_start > 0:
            self.state.log(f"PS3 time-seek request for {video_id}: {seek_start:.3f}s; restarting transcode there.")
        else:
            self.state.log(f"PS3 requested YouTube video {video_id}; resolving stream...")
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        proxy_tokens: list[str] = []
        try:
            max_height = int(self.state.settings.max_height)
            # Modern YouTube frequently exposes the useful video and audio as
            # separate streams.  Do NOT require a pre-merged A/V format.
            # Prefer separate best-video + best-audio up to the configured
            # height, then fall back to a combined stream if one exists.
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "format": (
                    f"bv[height<=?{max_height}][vcodec^=avc1]+ba[acodec^=mp4a]/"
                    f"bv[height<=?{max_height}][vcodec^=avc1]+ba/"
                    f"bv[height<=?{max_height}]+ba/"
                    f"b[height<=?{max_height}]/bv+ba/b"
                ),
                "noplaylist": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(watch_url, download=False)

            if not isinstance(info, dict):
                raise RuntimeError("yt-dlp returned no video information")

            requested = info.get("requested_formats")
            if requested:
                candidates = [f for f in requested if isinstance(f, dict) and f.get("url")]
            else:
                candidates = [info] if info.get("url") else []

            if not candidates:
                raise RuntimeError("yt-dlp returned no playable media URLs")

            video_fmt = next((f for f in candidates if f.get("vcodec") not in (None, "none")), None)
            audio_fmt = next((f for f in candidates if f.get("acodec") not in (None, "none")), None)

            if video_fmt is None:
                raise RuntimeError("yt-dlp returned no video stream")

            # If a single selected format already has both video and audio,
            # FFmpeg only needs one input. Otherwise feed the separate video
            # and audio CDN URLs to FFmpeg and let it transcode/mux them.
            combined = (
                video_fmt.get("acodec") not in (None, "none")
                and (audio_fmt is video_fmt or audio_fmt is None)
            )
            if not combined and audio_fmt is None:
                raise RuntimeError("yt-dlp returned video but no audio stream")

            def add_ffmpeg_input(command: list[str], fmt: dict) -> None:
                token = self.state.register_youtube_source(fmt, info)
                proxy_tokens.append(token)
                local_url = f"http://127.0.0.1:{self.state.settings.port}/ytproxy/{token}"
                command += [
                    "-reconnect", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_on_network_error", "1",
                    "-reconnect_on_http_error", "4xx,5xx",
                    "-reconnect_delay_max", "2",
                ]
                if seek_start > 0:
                    command += ["-ss", f"{seek_start:.3f}"]
                command += ["-i", local_url]

            cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning"]
            add_ffmpeg_input(cmd, video_fmt)
            if combined:
                cmd += ["-map", "0:v:0?", "-map", "0:a:0?"]
                self.state.log(
                    f"YouTube format {video_fmt.get('format_id', '?')}: combined A/V -> FFmpeg"
                )
            else:
                assert audio_fmt is not None
                add_ffmpeg_input(cmd, audio_fmt)
                cmd += ["-map", "0:v:0?", "-map", "1:a:0?"]
                self.state.log(
                    "YouTube formats "
                    f"video={video_fmt.get('format_id', '?')} "
                    f"audio={audio_fmt.get('format_id', '?')}: separate streams -> FFmpeg"
                )
        except Exception as e:
            self.state.remove_youtube_sources(proxy_tokens)
            self.state.log(f"YouTube resolver failed for {video_id}: {e}")
            self.send_error(502, f"Could not resolve YouTube video: {e}")
            return

        cmd += [
            "-sn",
            "-dn",
            # The PS3 Media Server family traditionally feeds the PS3 a VOB-style
            # MPEG Program Stream when transcoding MPEG-2 + AC-3. FFmpeg's `vob`
            # muxer is still MPEG-PS, but produces the pack/system headers the PS3
            # is happiest with. A short GOP also gives the decoder a keyframe very
            # quickly instead of leaving a black frame while it waits.
            "-fflags", "+genpts",
            # Absolute safety cap: even if YouTube's fallback selector returns
            # an odd source, FFmpeg never sends more than the selected height.
            "-vf", f"scale=-2:min({max_height}\\,ih)",
            "-c:v", "mpeg2video",
            "-pix_fmt", "yuv420p",
            "-g", "12",
            "-bf", "2",
            "-q:v", "2",
            "-qmin", "2",
            "-qmax", "4",
            "-maxrate", "10000k",
            "-bufsize", "1835k",
            "-c:a", "ac3",
            "-b:a", "448k",
            "-ac", "2",
            "-ar", "48000",
            "-max_muxing_queue_size", "2048",
            "-muxpreload", "0",
            "-muxdelay", "0",
            "-avoid_negative_ts", "make_zero",
            "-f", "vob",
            "pipe:1",
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                bufsize=1024 * 1024,
            )
        except Exception as e:
            self.state.remove_youtube_sources(proxy_tokens)
            self.state.log(f"Could not start FFmpeg: {e}")
            self.send_error(500, f"Could not start FFmpeg: {e}")
            return

        previous_proc = None
        with self.state.active_streams_lock:
            previous_proc = self.state.active_streams.get(video_id)
            self.state.active_streams[video_id] = proc
        if previous_proc is not None and previous_proc is not proc and previous_proc.poll() is None:
            try:
                previous_proc.terminate()
                self.state.log(f"Replaced previous stream for {video_id} (seek/reconnect).")
            except Exception:
                pass

        def drain_stderr() -> None:
            assert proc.stderr is not None
            lines = []
            try:
                for raw_line in iter(proc.stderr.readline, b""):
                    line = raw_line.decode("utf-8", "replace").strip()
                    if line:
                        lines.append(line)
                        if len(lines) > 10:
                            lines.pop(0)
            finally:
                if proc.returncode not in (None, 0) and lines:
                    self.state.log("FFmpeg: " + " | ".join(lines[-3:]))

        threading.Thread(target=drain_stderr, daemon=True).start()

        playback_started = time.monotonic()
        learned_watch = False
        try:
            assert proc.stdout is not None

            # Do NOT tell the PS3 "200 OK" until FFmpeg has already produced a
            # useful amount of real MPEG-PS data. The PS3 is old and impatient:
            # if headers arrive and the body then stalls while FFmpeg initializes,
            # it can abandon playback and leave the XMB on a black screen.
            prebuffer_target = (256 if seek_start > 0 else 512) * 1024
            prebuffer = bytearray()
            prebuffer_started = time.monotonic()
            while len(prebuffer) < prebuffer_target:
                chunk = proc.stdout.read(min(128 * 1024, prebuffer_target - len(prebuffer)))
                if not chunk:
                    break
                prebuffer.extend(chunk)

            prebuffer_time = time.monotonic() - prebuffer_started
            if not prebuffer:
                rc = proc.poll()
                raise RuntimeError(f"FFmpeg produced no MPEG data (exit={rc})")

            self.state.log(
                f"PS3 playback buffer ready: {len(prebuffer) / 1024:.0f} KiB "
                f"in {prebuffer_time:.2f}s; sending stream now."
            )

            self.send_response(200)
            self.send_header("Content-Type", "video/mpeg")
            self.send_header("Connection", "close")
            self.send_header("Accept-Ranges", "none")
            self.send_header("transferMode.dlna.org", "Streaming")
            self.send_header("contentFeatures.dlna.org", CONTENT_FEATURES)
            self.send_header("TimeSeekRange.dlna.org", npt_response(seek_start, duration_seconds))
            if self.headers.get("getAvailableSeekRange.dlna.org", "").strip() == "1":
                end = f"{float(duration_seconds):.3f}" if duration_seconds > 0 else ""
                self.send_header("availableSeekRange.dlna.org", f"1 npt=0.000-{end}")
            self.end_headers()
            self.close_connection = True

            self.wfile.write(prebuffer)
            self.wfile.flush()
            sent = len(prebuffer)
            self.state.log(f"PS3 received first {sent / 1024:.0f} KiB of MPEG-PS data.")

            while True:
                chunk = proc.stdout.read(128 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                sent += len(chunk)
                if not learned_watch and self.state.settings.recommendations_enabled:
                    elapsed = time.monotonic() - playback_started
                    if elapsed >= WATCH_LEARN_MIN_SECONDS and sent >= WATCH_LEARN_MIN_BYTES:
                        item = self.state.find_video(video_id)
                        if item is not None:
                            self.state.record_watch(item, elapsed)
                            learned_watch = True
            self.state.log(f"Stream {video_id} ended after sending {sent / (1024*1024):.1f} MiB.")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.state.log(f"PS3 stopped stream {video_id} after receiving data.")
        except Exception as e:
            self.state.log(f"Streaming error for {video_id}: {e}")
        finally:
            self.state.remove_youtube_sources(proxy_tokens)
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            with self.state.active_streams_lock:
                if self.state.active_streams.get(video_id) is proc:
                    self.state.active_streams.pop(video_id, None)


class SSDPServer(threading.Thread):
    def __init__(self, state: BridgeState):
        super().__init__(daemon=True, name="SSDPServer")
        self.state = state
        self.sock: Optional[socket.socket] = None

    def _location(self) -> str:
        ip = self.state.settings.advertised_ip.strip() or detect_local_ip()
        return f"http://{ip}:{self.state.settings.port}/device.xml"

    def _targets(self) -> list[tuple[str, str]]:
        u = self.state.settings.udn
        return [
            ("upnp:rootdevice", f"uuid:{u}::upnp:rootdevice"),
            (f"uuid:{u}", f"uuid:{u}"),
            ("urn:schemas-upnp-org:device:MediaServer:1", f"uuid:{u}::urn:schemas-upnp-org:device:MediaServer:1"),
            ("urn:schemas-upnp-org:service:ContentDirectory:1", f"uuid:{u}::urn:schemas-upnp-org:service:ContentDirectory:1"),
            ("urn:schemas-upnp-org:service:ConnectionManager:1", f"uuid:{u}::urn:schemas-upnp-org:service:ConnectionManager:1"),
            ("urn:microsoft.com:service:X_MS_MediaReceiverRegistrar:1", f"uuid:{u}::urn:microsoft.com:service:X_MS_MediaReceiverRegistrar:1"),
        ]

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock = sock
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            sock.bind(("", SSDP_PORT))
        except OSError as e:
            self.state.log(f"SSDP could not bind UDP {SSDP_PORT}: {e}")
            return
        try:
            mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as e:
            self.state.log(f"SSDP multicast join warning: {e}")
        sock.settimeout(1.0)
        self.state.log(f"SSDP discovery listening on UDP {SSDP_PORT}.")
        self.send_alive()
        last_notify = time.monotonic()

        while self.state.running.is_set():
            if time.monotonic() - last_notify > 30:
                self.send_alive()
                last_notify = time.monotonic()
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", "ignore")
            if not text.upper().startswith("M-SEARCH"):
                continue
            headers = {}
            for line in text.replace("\r", "").split("\n")[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().upper()] = v.strip()
            st = headers.get("ST", "")
            for target, usn in self._targets():
                if st in ("ssdp:all", target):
                    response = (
                        "HTTP/1.1 200 OK\r\n"
                        "CACHE-CONTROL: max-age=1800\r\n"
                        "EXT:\r\n"
                        f"LOCATION: {self._location()}\r\n"
                        f"SERVER: {SERVER_HEADER}\r\n"
                        f"ST: {target}\r\n"
                        f"USN: {usn}\r\n"
                        "\r\n"
                    ).encode("utf-8")
                    try:
                        sock.sendto(response, addr)
                    except OSError:
                        pass

        try:
            self.send_byebye()
        except Exception:
            pass
        sock.close()

    def _notify(self, nts: str) -> None:
        if not self.sock:
            return
        for nt, usn in self._targets():
            msg = (
                "NOTIFY * HTTP/1.1\r\n"
                f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
                "CACHE-CONTROL: max-age=1800\r\n"
                f"LOCATION: {self._location()}\r\n"
                f"NT: {nt}\r\n"
                f"NTS: {nts}\r\n"
                f"SERVER: {SERVER_HEADER}\r\n"
                f"USN: {usn}\r\n"
                "\r\n"
            ).encode("utf-8")
            try:
                self.sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
            except OSError:
                pass

    def send_alive(self) -> None:
        self._notify("ssdp:alive")

    def send_byebye(self) -> None:
        self._notify("ssdp:byebye")

    def stop(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


class BridgeServer:
    def __init__(self, state: BridgeState):
        self.state = state
        self.httpd: Optional[ThreadedHTTPServer] = None
        self.http_thread: Optional[threading.Thread] = None
        self.ssdp: Optional[SSDPServer] = None
        self.state.refresh_callback = self.announce_library_update

    def announce_library_update(self) -> None:
        if not self.state.running.is_set() or self.ssdp is None:
            return
        def worker() -> None:
            # Repeat alive announcements so older renderers such as the PS3 have
            # another chance to re-check the ContentDirectory update ID.
            for _ in range(3):
                if not self.state.running.is_set() or self.ssdp is None:
                    return
                try:
                    self.ssdp.send_alive()
                except Exception:
                    return
                time.sleep(0.15)
            self.state.log("Broadcast refreshed DLNA library announcement.")
        threading.Thread(target=worker, daemon=True, name="DLNARefreshAnnounce").start()

    def start(self) -> None:
        if self.state.running.is_set():
            return
        self.state.running.set()
        try:
            self.httpd = ThreadedHTTPServer(("0.0.0.0", self.state.settings.port), DLNARequestHandler)
            self.httpd.bridge_state = self.state  # type: ignore[attr-defined]
        except Exception:
            self.state.running.clear()
            raise
        self.http_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="DLNAHTTP")
        self.http_thread.start()
        self.ssdp = SSDPServer(self.state)
        self.ssdp.start()
        ip = self.state.settings.advertised_ip.strip() or detect_local_ip()
        self.state.log(f"DLNA server started as '{self.state.settings.friendly_name}'.")
        self.state.log("PS3 compatibility profile: XMB keyboard + thumbnails + 480p buffered VOB/MPEG-PS + 8 MiB resilient YouTube chunk proxy.")
        self.state.log(f"HTTP server: http://{ip}:{self.state.settings.port}/device.xml")
        self.state.log("On PS3: Settings > Network Settings > Media Server Connection = Enable, then Video > Search for Media Servers.")

    def stop(self) -> None:
        if not self.state.running.is_set():
            return
        self.state.running.clear()
        if self.ssdp:
            try:
                self.ssdp.send_byebye()
                self.ssdp.stop()
            except Exception:
                pass
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
        with self.state.active_streams_lock:
            procs = list(self.state.active_streams.values())
            self.state.active_streams.clear()
        for proc in procs:
            try:
                proc.terminate()
            except Exception:
                pass
        self.state.log("Server stopped.")


class App(tk.Tk):
    BG = "#0b1018"
    PANEL = "#121a25"
    PANEL_ALT = "#0f1620"
    BORDER = "#263244"
    TEXT = "#f2f5f9"
    MUTED = "#93a1b5"
    ACCENT = "#4d8dff"
    ACCENT_HOVER = "#6aa1ff"
    GOOD = "#28c76f"
    BAD = "#ff5d68"
    WARNING = "#f5b942"

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.configure(bg=self.BG)
        self._configure_responsive_window()
        self._resize_after_id = None
        self.settings = Settings.load()
        if not self.settings.advertised_ip:
            self.settings.advertised_ip = detect_local_ip()
        self.state_obj = BridgeState(self.settings)
        self.server_obj = BridgeServer(self.state_obj)
        self._apply_theme()
        self._build_ui()
        self._load_to_ui()
        self.after(100, self._drain_logs)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_responsive_window(self) -> None:
        """Size the window to the current screen instead of assuming a desktop-sized display."""
        screen_w = max(640, int(self.winfo_screenwidth()))
        screen_h = max(480, int(self.winfo_screenheight()))

        # Leave room for desktop panels/taskbars and window decorations.
        target_w = min(1180, max(700, screen_w - 80))
        target_h = min(790, max(500, screen_h - 100))
        target_w = min(target_w, screen_w)
        target_h = min(target_h, screen_h)

        min_w = min(target_w, max(680, min(860, screen_w - 140)))
        min_h = min(target_h, max(460, min(560, screen_h - 160)))
        self.minsize(min_w, min_h)

        x = max(0, (screen_w - target_w) // 2)
        y = max(0, (screen_h - target_h) // 2)
        self.geometry(f"{target_w}x{target_h}+{x}+{y}")

    def _queue_responsive_layout(self, _event=None) -> None:
        if self._resize_after_id is not None:
            try:
                self.after_cancel(self._resize_after_id)
            except Exception:
                pass
        self._resize_after_id = self.after(60, self._apply_responsive_layout)

    def _apply_responsive_layout(self) -> None:
        self._resize_after_id = None
        width = max(1, self.winfo_width())

        # Give the library more room on smaller laptop displays while keeping
        # the settings column usable.
        sidebar_width = 320 if width >= 1120 else 290 if width >= 940 else 260
        try:
            self.body.columnconfigure(0, minsize=sidebar_width)
            self.sidebar_canvas.configure(width=sidebar_width)
        except Exception:
            pass

        # Resize the variable-width result columns to the space actually available.
        try:
            tree_w = max(360, self.results_tree.winfo_width())
            fav_w = 42
            id_w = 110 if tree_w >= 720 else 90
            usable = max(260, tree_w - fav_w - id_w - 34)
            channel_w = max(110, int(usable * (0.30 if tree_w >= 760 else 0.25)))
            title_w = max(170, usable - channel_w)
            self.results_tree.column("fav", width=fav_w, stretch=False)
            self.results_tree.column("id", width=id_w, stretch=False)
            self.results_tree.column("channel", width=channel_w, minwidth=90, stretch=True)
            self.results_tree.column("title", width=title_w, minwidth=150, stretch=True)
        except Exception:
            pass

    def _sidebar_mousewheel(self, event) -> None:
        if getattr(event, "num", None) == 4:
            amount = -3
        elif getattr(event, "num", None) == 5:
            amount = 3
        else:
            delta = getattr(event, "delta", 0)
            amount = -int(delta / 120) if delta else 0
            if amount == 0 and delta:
                amount = -1 if delta > 0 else 1
        if amount:
            self.sidebar_canvas.yview_scroll(amount, "units")

    def _enable_sidebar_wheel(self, _event=None) -> None:
        self.bind_all("<MouseWheel>", self._sidebar_mousewheel)
        self.bind_all("<Button-4>", self._sidebar_mousewheel)
        self.bind_all("<Button-5>", self._sidebar_mousewheel)

    def _disable_sidebar_wheel(self, _event=None) -> None:
        self.unbind_all("<MouseWheel>")
        self.unbind_all("<Button-4>")
        self.unbind_all("<Button-5>")

    def _apply_theme(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("App.TFrame", background=self.BG)
        style.configure("Card.TFrame", background=self.PANEL, relief="flat")
        style.configure("Inset.TFrame", background=self.PANEL_ALT)
        style.configure("TLabel", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT, font=("Segoe UI Semibold", 20))
        style.configure("Subtitle.TLabel", background=self.BG, foreground=self.MUTED, font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI Semibold", 11))
        style.configure("Muted.TLabel", background=self.PANEL, foreground=self.MUTED, font=("Segoe UI", 9))
        style.configure("Tiny.TLabel", background=self.PANEL, foreground=self.MUTED, font=("Segoe UI", 8))
        style.configure("Badge.TLabel", background=self.PANEL_ALT, foreground=self.MUTED, font=("Segoe UI Semibold", 9), padding=(8, 4))

        style.configure(
            "TEntry",
            fieldbackground=self.PANEL_ALT,
            foreground=self.TEXT,
            insertcolor=self.TEXT,
            bordercolor=self.BORDER,
            lightcolor=self.BORDER,
            darkcolor=self.BORDER,
            padding=7,
        )
        style.map("TEntry", bordercolor=[("focus", self.ACCENT)])
        style.configure(
            "TCombobox",
            fieldbackground=self.PANEL_ALT,
            background=self.PANEL_ALT,
            foreground=self.TEXT,
            arrowcolor=self.TEXT,
            bordercolor=self.BORDER,
            lightcolor=self.BORDER,
            darkcolor=self.BORDER,
            padding=6,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", self.PANEL_ALT)],
            foreground=[("readonly", self.TEXT)],
            bordercolor=[("focus", self.ACCENT)],
        )
        style.configure(
            "TSpinbox",
            fieldbackground=self.PANEL_ALT,
            foreground=self.TEXT,
            arrowcolor=self.TEXT,
            bordercolor=self.BORDER,
            lightcolor=self.BORDER,
            darkcolor=self.BORDER,
            padding=6,
        )
        style.configure("TCheckbutton", background=self.PANEL, foreground=self.TEXT)
        style.map("TCheckbutton", background=[("active", self.PANEL)])

        style.configure(
            "TButton",
            background=self.PANEL_ALT,
            foreground=self.TEXT,
            bordercolor=self.BORDER,
            lightcolor=self.BORDER,
            darkcolor=self.BORDER,
            padding=(10, 7),
            font=("Segoe UI Semibold", 9),
        )
        style.map(
            "TButton",
            background=[("active", "#1b2635"), ("pressed", "#202e40")],
            bordercolor=[("focus", self.ACCENT)],
        )
        style.configure(
            "Accent.TButton",
            background=self.ACCENT,
            foreground="white",
            bordercolor=self.ACCENT,
            lightcolor=self.ACCENT,
            darkcolor=self.ACCENT,
            padding=(13, 8),
            font=("Segoe UI Semibold", 10),
        )
        style.map("Accent.TButton", background=[("active", self.ACCENT_HOVER), ("pressed", "#3779e6")])
        style.configure(
            "Danger.TButton",
            background="#3a2025",
            foreground="#ff9aa2",
            bordercolor="#593038",
            lightcolor="#593038",
            darkcolor="#593038",
        )
        style.map("Danger.TButton", background=[("active", "#4a252c")])

        style.configure(
            "Treeview",
            background=self.PANEL_ALT,
            fieldbackground=self.PANEL_ALT,
            foreground=self.TEXT,
            bordercolor=self.BORDER,
            rowheight=30,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Treeview.Heading",
            background="#182231",
            foreground=self.MUTED,
            bordercolor=self.BORDER,
            font=("Segoe UI Semibold", 9),
            padding=(7, 7),
        )
        style.map(
            "Treeview",
            background=[("selected", "#244c86")],
            foreground=[("selected", "white")],
        )
        style.map("Treeview.Heading", background=[("active", "#202d40")])

        style.configure("TNotebook", background=self.BG, borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=self.PANEL_ALT,
            foreground=self.MUTED,
            borderwidth=0,
            padding=(16, 9),
            font=("Segoe UI Semibold", 9),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", self.PANEL), ("active", "#1a2534")],
            foreground=[("selected", self.TEXT), ("active", self.TEXT)],
        )
        style.configure("Horizontal.TProgressbar", background=self.ACCENT, troughcolor=self.PANEL_ALT, bordercolor=self.PANEL_ALT)
        style.configure("Vertical.TScrollbar", background=self.PANEL_ALT, troughcolor=self.PANEL, bordercolor=self.PANEL)

        self.option_add("*TCombobox*Listbox.background", self.PANEL_ALT)
        self.option_add("*TCombobox*Listbox.foreground", self.TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", "#244c86")
        self.option_add("*TCombobox*Listbox.selectForeground", "white")

    def _card(self, parent, *, row: int, column: int, columnspan: int = 1, sticky: str = "nsew", padx=(0, 0), pady=(0, 0)) -> ttk.Frame:
        outer = tk.Frame(parent, bg=self.BORDER, bd=0, highlightthickness=0)
        outer.grid(row=row, column=column, columnspan=columnspan, sticky=sticky, padx=padx, pady=pady)
        inner = ttk.Frame(outer, style="Card.TFrame", padding=14)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        return inner

    def _field_label(self, parent, text: str, row: int, column: int = 0, *, pady=(3, 3)) -> None:
        ttk.Label(parent, text=text, style="Muted.TLabel").grid(row=row, column=column, sticky="w", pady=pady)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # Header
        header = ttk.Frame(self, style="App.TFrame", padding=(18, 14, 18, 10))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        title_wrap = ttk.Frame(header, style="App.TFrame")
        title_wrap.grid(row=0, column=0, sticky="w")
        ttk.Label(title_wrap, text="PS3 YouTube", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(title_wrap, text="YouTube → DLNA → XMB Video", style="Subtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))

        version = tk.Label(
            header,
            text=f"v{APP_VERSION}",
            bg="#182231",
            fg=self.MUTED,
            font=("Segoe UI Semibold", 9),
            padx=10,
            pady=5,
        )
        version.grid(row=0, column=1, sticky="w", padx=(14, 0))

        status_wrap = ttk.Frame(header, style="App.TFrame")
        status_wrap.grid(row=0, column=2, rowspan=2, sticky="e")
        self.status_var = tk.StringVar(value="Stopped")
        self.status_badge = tk.Label(
            status_wrap,
            textvariable=self.status_var,
            bg="#3a2025",
            fg="#ff9aa2",
            font=("Segoe UI Semibold", 9),
            padx=12,
            pady=6,
        )
        self.status_badge.grid(row=0, column=0, padx=(0, 10))
        self.server_btn = ttk.Button(status_wrap, text="Start server", style="Accent.TButton", command=self._toggle_server)
        self.server_btn.grid(row=0, column=1)

        # Main two-column layout
        self.body = ttk.Frame(self, style="App.TFrame", padding=(18, 4, 18, 18))
        body = self.body
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=0, minsize=320)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # The settings column is vertically scrollable. This is important on
        # 720p/768p laptop panels where the full settings stack cannot fit.
        sidebar_shell = ttk.Frame(body, style="App.TFrame")
        sidebar_shell.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        sidebar_shell.columnconfigure(0, weight=1)
        sidebar_shell.rowconfigure(0, weight=1)

        self.sidebar_canvas = tk.Canvas(
            sidebar_shell,
            bg=self.BG,
            highlightthickness=0,
            borderwidth=0,
            width=320,
        )
        sidebar_scroll = ttk.Scrollbar(sidebar_shell, orient="vertical", command=self.sidebar_canvas.yview)
        self.sidebar_canvas.configure(yscrollcommand=sidebar_scroll.set)
        self.sidebar_canvas.grid(row=0, column=0, sticky="nsew")
        sidebar_scroll.grid(row=0, column=1, sticky="ns")

        sidebar = ttk.Frame(self.sidebar_canvas, style="App.TFrame")
        sidebar.columnconfigure(0, weight=1)
        sidebar_window = self.sidebar_canvas.create_window((0, 0), window=sidebar, anchor="nw")

        def _sync_sidebar_scrollregion(_event=None):
            self.sidebar_canvas.configure(scrollregion=self.sidebar_canvas.bbox("all"))

        def _sync_sidebar_width(event):
            self.sidebar_canvas.itemconfigure(sidebar_window, width=max(1, event.width))

        sidebar.bind("<Configure>", _sync_sidebar_scrollregion)
        self.sidebar_canvas.bind("<Configure>", _sync_sidebar_width)
        self.sidebar_canvas.bind("<Enter>", self._enable_sidebar_wheel)
        self.sidebar_canvas.bind("<Leave>", self._disable_sidebar_wheel)
        sidebar.bind("<Enter>", self._enable_sidebar_wheel)
        sidebar.bind("<Leave>", self._disable_sidebar_wheel)

        # Server card
        server = self._card(sidebar, row=0, column=0, sticky="ew", pady=(0, 10))
        server.columnconfigure(0, weight=1)
        ttk.Label(server, text="Server", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(server, text="How your PC appears on the PS3 network.", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 10))

        self._field_label(server, "Server name", 2)
        self.name_var = tk.StringVar()
        ttk.Entry(server, textvariable=self.name_var).grid(row=3, column=0, sticky="ew", pady=(0, 7))

        ip_row = ttk.Frame(server, style="Card.TFrame")
        ip_row.grid(row=4, column=0, sticky="ew", pady=(0, 7))
        ip_row.columnconfigure(0, weight=1)
        left_ip = ttk.Frame(ip_row, style="Card.TFrame")
        left_ip.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        left_ip.columnconfigure(0, weight=1)
        ttk.Label(left_ip, text="Advertised IP", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.ip_var = tk.StringVar()
        ttk.Entry(left_ip, textvariable=self.ip_var).grid(row=1, column=0, sticky="ew", pady=(3, 0))
        right_port = ttk.Frame(ip_row, style="Card.TFrame")
        right_port.grid(row=0, column=1, sticky="e")
        ttk.Label(right_port, text="Port", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar()
        ttk.Entry(right_port, textvariable=self.port_var, width=7).grid(row=1, column=0, pady=(3, 0))

        ttk.Button(server, text="Detect local IP", command=lambda: self.ip_var.set(detect_local_ip())).grid(row=5, column=0, sticky="ew", pady=(0, 8))

        self._field_label(server, "FFmpeg", 6)
        ff_row = ttk.Frame(server, style="Card.TFrame")
        ff_row.grid(row=7, column=0, sticky="ew")
        ff_row.columnconfigure(0, weight=1)
        self.ffmpeg_var = tk.StringVar()
        ttk.Entry(ff_row, textvariable=self.ffmpeg_var).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(ff_row, text="Browse", command=self._browse_ffmpeg).grid(row=0, column=1)

        # YouTube / playback card
        youtube = self._card(sidebar, row=1, column=0, sticky="ew", pady=(0, 10))
        youtube.columnconfigure(0, weight=1)
        ttk.Label(youtube, text="YouTube & playback", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(youtube, text="Search metadata + PS3 transcode preferences.", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 10))

        self._field_label(youtube, "YouTube Data API key", 2)
        api_row = ttk.Frame(youtube, style="Card.TFrame")
        api_row.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        api_row.columnconfigure(0, weight=1)
        self.api_var = tk.StringVar()
        self.api_entry = ttk.Entry(api_row, textvariable=self.api_var, show="•")
        self.api_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.show_key_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(api_row, text="Show", variable=self.show_key_var, command=self._toggle_show_key).grid(row=0, column=1)

        opts = ttk.Frame(youtube, style="Card.TFrame")
        opts.grid(row=4, column=0, sticky="ew")
        for c in range(2):
            opts.columnconfigure(c, weight=1)

        ttk.Label(opts, text="Results", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(opts, text="Quality cap", style="Muted.TLabel").grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.max_results_var = tk.StringVar()
        ttk.Spinbox(opts, from_=1, to=50, textvariable=self.max_results_var, width=8).grid(row=1, column=0, sticky="ew", pady=(3, 7))
        self.height_var = tk.StringVar()
        ttk.Combobox(opts, textvariable=self.height_var, values=("360", "480"), state="readonly", width=8).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(3, 7))

        ttk.Label(opts, text="Region", style="Muted.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Label(opts, text="Language", style="Muted.TLabel").grid(row=2, column=1, sticky="w", padx=(8, 0))
        self.region_var = tk.StringVar()
        self.lang_var = tk.StringVar()
        ttk.Entry(opts, textvariable=self.region_var).grid(row=3, column=0, sticky="ew", pady=(3, 0))
        ttk.Entry(opts, textvariable=self.lang_var).grid(row=3, column=1, sticky="ew", padx=(8, 0), pady=(3, 0))

        rec_box = ttk.Frame(youtube, style="Card.TFrame")
        rec_box.grid(row=5, column=0, sticky="ew", pady=(11, 0))
        rec_box.columnconfigure(0, weight=1)
        self.recommendations_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            rec_box,
            text="Personalized Home recommendations",
            variable=self.recommendations_var,
            command=self._recommendations_toggled,
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            rec_box,
            text="Learns only from this bridge: watched videos, favorites and searches.",
            style="Tiny.TLabel",
            wraplength=260,
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        actions = self._card(sidebar, row=2, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        ttk.Button(actions, text="Save settings", command=self._save_settings).grid(row=0, column=0, sticky="ew", pady=(0, 7))
        ttk.Button(actions, text="Refresh PS3 library", command=self._republish_to_ps3).grid(row=1, column=0, sticky="ew", pady=(0, 7))
        ttk.Button(actions, text="Reset recommendation learning", command=self._reset_recommendations).grid(row=2, column=0, sticky="ew", pady=(0, 7))
        ttk.Button(actions, text="Clear search history", style="Danger.TButton", command=self._clear_history).grid(row=3, column=0, sticky="ew")

        # Main panel
        main = ttk.Frame(body, style="App.TFrame")
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        search = self._card(main, row=0, column=0, sticky="ew", pady=(0, 10))
        search.columnconfigure(0, weight=1)
        ttk.Label(search, text="Search YouTube", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(search, text="Results are published immediately to XMB → Video → PS3 YouTube.", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 9))

        search_row = ttk.Frame(search, style="Card.TFrame")
        search_row.grid(row=2, column=0, sticky="ew")
        search_row.columnconfigure(0, weight=1)
        self.query_var = tk.StringVar()
        self.query_entry = ttk.Combobox(search_row, textvariable=self.query_var, font=("Segoe UI", 11))
        self.query_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.query_entry.bind("<Return>", lambda e: self._search())
        self.query_entry.configure(postcommand=self._refresh_history_ui)
        self.search_btn = ttk.Button(search_row, text="Search & publish", style="Accent.TButton", command=self._search)
        self.search_btn.grid(row=0, column=1)

        self.search_progress = ttk.Progressbar(search, mode="indeterminate", style="Horizontal.TProgressbar")
        self.search_progress.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.search_progress.grid_remove()

        notebook = ttk.Notebook(main)
        notebook.grid(row=1, column=0, sticky="nsew")

        # Library tab
        library_tab = ttk.Frame(notebook, style="Card.TFrame", padding=12)
        library_tab.columnconfigure(0, weight=1)
        library_tab.rowconfigure(1, weight=1)
        notebook.add(library_tab, text="  Library  ")

        info_bar = ttk.Frame(library_tab, style="Card.TFrame")
        info_bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 9))
        info_bar.columnconfigure(0, weight=1)
        self.results_summary_var = tk.StringVar(value="No search published yet")
        ttk.Label(info_bar, textvariable=self.results_summary_var, style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.fav_count_var = tk.StringVar(value="Favorites: 0")
        ttk.Label(info_bar, textvariable=self.fav_count_var, style="Badge.TLabel").grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.sub_count_var = tk.StringVar(value="Subscriptions: 0")
        ttk.Label(info_bar, textvariable=self.sub_count_var, style="Badge.TLabel").grid(row=0, column=2, sticky="e", padx=(8, 0))

        self.results_tree = ttk.Treeview(
            library_tab,
            columns=("fav", "title", "channel", "id"),
            show="headings",
            height=14,
            selectmode="extended",
        )
        self.results_tree.heading("fav", text="★")
        self.results_tree.heading("title", text="Title")
        self.results_tree.heading("channel", text="Channel")
        self.results_tree.heading("id", text="Video ID")
        self.results_tree.column("fav", width=42, stretch=False, anchor="center")
        self.results_tree.column("title", width=520, stretch=True)
        self.results_tree.column("channel", width=190, stretch=True)
        self.results_tree.column("id", width=110, stretch=False)
        scroll = ttk.Scrollbar(library_tab, orient="vertical", command=self.results_tree.yview)
        hscroll = ttk.Scrollbar(library_tab, orient="horizontal", command=self.results_tree.xview)
        self.results_tree.configure(yscrollcommand=scroll.set, xscrollcommand=hscroll.set)
        self.results_tree.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")
        hscroll.grid(row=2, column=0, sticky="ew", pady=(2, 0))

        fav_buttons = ttk.Frame(library_tab, style="Card.TFrame")
        fav_buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(9, 0))
        for c in range(2):
            fav_buttons.columnconfigure(c, weight=1)
        ttk.Button(fav_buttons, text="★ Add selected to Favorites", command=self._add_selected_favorites).grid(row=0, column=0, sticky="ew", padx=(0, 5), pady=(0, 6))
        ttk.Button(fav_buttons, text="Remove selected from Favorites", command=self._remove_selected_favorites).grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=(0, 6))
        ttk.Button(fav_buttons, text="＋ Subscribe selected channel(s)", command=self._subscribe_selected_channels).grid(row=1, column=0, sticky="ew", padx=(0, 5))
        ttk.Button(fav_buttons, text="Unsubscribe selected channel(s)", command=self._unsubscribe_selected_channels).grid(row=1, column=1, sticky="ew", padx=(5, 0))

        # Activity tab
        activity_tab = ttk.Frame(notebook, style="Card.TFrame", padding=12)
        activity_tab.columnconfigure(0, weight=1)
        activity_tab.rowconfigure(1, weight=1)
        notebook.add(activity_tab, text="  Activity  ")
        ttk.Label(activity_tab, text="Server activity", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.log_text = tk.Text(
            activity_tab,
            wrap="word",
            height=12,
            state="disabled",
            bg="#0a0f16",
            fg="#bdc8d8",
            insertbackground=self.TEXT,
            selectbackground="#244c86",
            selectforeground="white",
            relief="flat",
            padx=10,
            pady=10,
            font=("Consolas" if os.name == "nt" else "DejaVu Sans Mono", 9),
        )
        log_scroll = ttk.Scrollbar(activity_tab, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=1, column=0, sticky="nsew")
        log_scroll.grid(row=1, column=1, sticky="ns")

        footer = ttk.Label(
            main,
            text="Tip: keep this app open while the PS3 is browsing or playing videos.",
            style="Subtitle.TLabel",
        )
        footer.grid(row=2, column=0, sticky="w", pady=(8, 0))

        self.bind("<Configure>", self._queue_responsive_layout, add="+")
        self.after(100, self._apply_responsive_layout)

    def _set_server_status(self, running: bool) -> None:
        if running:
            self.status_var.set(f"Online  •  {self.settings.advertised_ip}:{self.settings.port}")
            self.status_badge.configure(bg="#173629", fg="#72e6a4")
            self.server_btn.configure(text="Stop server", style="Danger.TButton")
        else:
            self.status_var.set("Offline")
            self.status_badge.configure(bg="#3a2025", fg="#ff9aa2")
            self.server_btn.configure(text="Start server", style="Accent.TButton")

    def _set_search_busy(self, busy: bool) -> None:
        if busy:
            self.search_btn.configure(state="disabled", text="Searching…")
            self.search_progress.grid()
            self.search_progress.start(10)
        else:
            self.search_progress.stop()
            self.search_progress.grid_remove()
            self.search_btn.configure(state="normal", text="Search & publish")

    def _load_to_ui(self) -> None:
        s = self.settings
        self.name_var.set(s.friendly_name)
        self.ip_var.set(s.advertised_ip)
        self.port_var.set(str(s.port))
        self.ffmpeg_var.set(s.ffmpeg_path or (find_ffmpeg("") or ""))
        self.api_var.set(s.api_key)
        self.max_results_var.set(str(s.max_results))
        self.region_var.set(s.region_code)
        self.lang_var.set(s.relevance_language)
        self.height_var.set(str(s.max_height))
        self.recommendations_var.set(bool(s.recommendations_enabled))
        self._refresh_history_ui()
        self._refresh_favorite_count()
        self._refresh_subscription_count()
        current = self.state_obj.snapshot_results()
        if current:
            self._show_results(current)
        else:
            self.results_summary_var.set("No search published yet")
        self._set_server_status(False)

    def _sync_from_ui(self) -> None:
        try:
            port = int(self.port_var.get().strip())
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            raise RuntimeError("Port must be a number between 1 and 65535.")
        try:
            max_results = int(self.max_results_var.get().strip())
            if not (1 <= max_results <= 50):
                raise ValueError
        except ValueError:
            raise RuntimeError("Results must be between 1 and 50.")
        try:
            max_height = int(self.height_var.get().strip())
            if max_height not in (360, 480):
                raise ValueError
        except ValueError:
            raise RuntimeError("PS3 quality cap must be 360 or 480.")

        self.settings.api_key = self.api_var.get().strip()
        self.settings.friendly_name = self.name_var.get().strip() or "PS3 YouTube"
        self.settings.advertised_ip = self.ip_var.get().strip() or detect_local_ip()
        self.settings.port = port
        self.settings.ffmpeg_path = self.ffmpeg_var.get().strip()
        self.settings.max_results = max_results
        self.settings.region_code = self.region_var.get().strip().upper()
        self.settings.relevance_language = self.lang_var.get().strip()
        self.settings.max_height = max_height
        self.settings.recommendations_enabled = bool(self.recommendations_var.get())

    def _recommendations_toggled(self) -> None:
        enabled = bool(self.recommendations_var.get())
        self.settings.recommendations_enabled = enabled
        try:
            self.settings.save()
        except Exception:
            pass
        self.state_obj._bump_library("Enabled personalized Home" if enabled else "Disabled personalized Home")
        if enabled and self.state_obj.running.is_set() and self.state_obj.home_is_stale():
            threading.Thread(target=self._refresh_home_background, daemon=True, name="HomeRefreshUI").start()

    def _refresh_home_background(self) -> None:
        try:
            self.state_obj.refresh_home()
        except Exception as e:
            self.state_obj.log(f"Home refresh failed: {e}")

    def _reset_recommendations(self) -> None:
        if not messagebox.askyesno(APP_NAME, "Reset learned watch/channel activity and cached Home recommendations?\n\nFavorites, subscriptions and search history will be kept."):
            return
        self.state_obj.clear_recommendation_learning()

    def _save_settings(self) -> None:
        try:
            self._sync_from_ui()
            self.settings.save()
            self.state_obj.log(f"Settings saved to {CONFIG_FILE}")
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))

    def _browse_ffmpeg(self) -> None:
        name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        path = filedialog.askopenfilename(title="Select FFmpeg executable", initialfile=name)
        if path:
            self.ffmpeg_var.set(path)

    def _toggle_show_key(self) -> None:
        self.api_entry.configure(show="" if self.show_key_var.get() else "•")

    def _toggle_server(self) -> None:
        if self.state_obj.running.is_set():
            self.server_obj.stop()
            self._set_server_status(False)
            return
        try:
            self._sync_from_ui()
            self.settings.save()
            if yt_dlp is None:
                command = "python -m pip install -r requirements.txt" if os.name == "nt" else "python3 -m pip install --break-system-packages -r requirements.txt"
                raise RuntimeError(f"yt-dlp is missing. Run: {command}")
            ff = find_ffmpeg(self.settings.ffmpeg_path)
            if not ff:
                raise RuntimeError("FFmpeg was not found. Install FFmpeg or select the FFmpeg executable in Server settings.")
            self.settings.ffmpeg_path = ff
            self.ffmpeg_var.set(ff)
            self.server_obj.start()
            self._set_server_status(True)
            if self.settings.recommendations_enabled and self.state_obj.home_is_stale():
                threading.Thread(target=self._refresh_home_background, daemon=True, name="HomeRefreshStartup").start()
        except Exception as e:
            self.state_obj.running.clear()
            self._set_server_status(False)
            messagebox.showerror(APP_NAME, f"Could not start server:\n\n{e}")

    def _search(self) -> None:
        query = self.query_var.get().strip()
        if not query:
            self.query_entry.focus_set()
            return
        try:
            self._sync_from_ui()
            self.settings.save()
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        self._set_search_busy(True)
        self.state_obj.log(f"Searching YouTube API for: {query}")

        def worker() -> None:
            try:
                results = youtube_search(self.settings, query)
                self.state_obj.set_results(results, query)
                self.after(0, lambda: self._show_results(results))
            except Exception as e:
                self.state_obj.log(f"Search failed: {e}")
                self.after(0, lambda err=str(e): messagebox.showerror(APP_NAME, err))
            finally:
                self.after(0, lambda: self._set_search_busy(False))

        threading.Thread(target=worker, daemon=True, name="YouTubeSearch").start()

    def _republish_to_ps3(self) -> None:
        if not self.state_obj.running.is_set():
            messagebox.showinfo(APP_NAME, "Start the DLNA server first.")
            return
        self.state_obj.republish()

    def _refresh_history_ui(self) -> None:
        if not hasattr(self, "query_entry"):
            return
        queries = [entry["query"] for entry in self.state_obj.snapshot_history()]
        self.query_entry.configure(values=queries)

    def _refresh_favorite_count(self) -> None:
        if hasattr(self, "fav_count_var"):
            self.fav_count_var.set(f"★ {len(self.state_obj.snapshot_favorites())} favorites")

    def _refresh_subscription_count(self) -> None:
        if hasattr(self, "sub_count_var"):
            self.sub_count_var.set(f"◉ {len(self.state_obj.snapshot_channel_subscriptions())} subscriptions")

    def _selected_result_objects(self) -> list[VideoResult]:
        out = []
        for iid in self.results_tree.selection():
            values = self.results_tree.item(iid, "values")
            if len(values) < 4:
                continue
            item = self.state_obj.find_video(str(values[3]))
            if item is not None:
                out.append(item)
        return out

    def _add_selected_favorites(self) -> None:
        selected = self._selected_result_objects()
        if not selected:
            return
        self.state_obj.add_favorites(selected)
        self._refresh_favorite_count()
        self._show_results(self.state_obj.snapshot_results())

    def _remove_selected_favorites(self) -> None:
        selected = self._selected_result_objects()
        if not selected:
            return
        self.state_obj.remove_favorites([item.video_id for item in selected])
        self._refresh_favorite_count()
        self._show_results(self.state_obj.snapshot_results())

    def _subscribe_selected_channels(self) -> None:
        selected = self._selected_result_objects()
        channels: dict[str, ChannelResult] = {}
        for item in selected:
            if item.channel_id:
                channels[item.channel_id] = ChannelResult(item.channel_id, item.channel)
        if not channels:
            return
        for channel in channels.values():
            self.state_obj.add_channel_subscription(channel)
        self._refresh_subscription_count()

    def _unsubscribe_selected_channels(self) -> None:
        selected = self._selected_result_objects()
        channel_ids = {item.channel_id for item in selected if item.channel_id}
        if not channel_ids:
            return
        for channel_id in channel_ids:
            self.state_obj.remove_channel_subscription(channel_id)
        self._refresh_subscription_count()

    def _clear_history(self) -> None:
        if not self.state_obj.snapshot_history():
            return
        if not messagebox.askyesno(APP_NAME, "Clear all saved search history? Favorites will be kept."):
            return
        self.state_obj.clear_history()
        self._refresh_history_ui()

    def _show_results(self, results: list[VideoResult]) -> None:
        favorites = {x.video_id for x in self.state_obj.snapshot_favorites()}
        for iid in self.results_tree.get_children():
            self.results_tree.delete(iid)
        for result in results:
            self.results_tree.insert(
                "",
                "end",
                values=("★" if result.video_id in favorites else "", result.title, result.channel, result.video_id),
            )
        query = self.state_obj.snapshot_query().strip()
        if results:
            suffix = f" • {self.settings.max_height}p max"
            self.results_summary_var.set(f"{len(results)} results published" + (f" for “{query}”" if query else "") + suffix)
        else:
            self.results_summary_var.set("No results published")
        self._refresh_favorite_count()
        self._refresh_subscription_count()
        self._refresh_history_ui()

    def _drain_logs(self) -> None:
        changed = False
        while True:
            try:
                line = self.state_obj.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
            changed = True
        self.after(100 if changed else 250, self._drain_logs)

    def _on_close(self) -> None:
        try:
            self._sync_from_ui()
            self.settings.save()
        except Exception:
            pass
        self.server_obj.stop()
        self.destroy()

def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
