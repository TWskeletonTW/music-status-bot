import asyncio
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from collections import OrderedDict
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import sounddevice as sd

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

__version__ = "v1.2.1"
VERSION = __version__
LRCLIB_BASE_URL = "https://lrclib.net/api"


def parse_optional_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None

    value = value.strip()
    if not value:
        return None

    try:
        return int(value)
    except ValueError:
        return None


def parse_optional_float(value: Optional[str], default: float) -> float:
    if value is None:
        return default

    value = value.strip()
    if not value:
        return default

    try:
        return float(value)
    except ValueError:
        return default


def parse_int_with_default(value: Optional[str], default: int) -> int:
    parsed = parse_optional_int(value)
    if parsed is None:
        return default
    return parsed


def parse_bool_with_default(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default

    normalized = value.strip().lower()
    if not normalized:
        return default

    if normalized in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "disable", "disabled"}:
        return False
    return default


def parse_id_set(value: Optional[str]) -> set[int]:
    if not value:
        return set()

    ids: set[int] = set()
    for item in re.split(r"[,\s]+", value.strip()):
        if not item:
            continue
        try:
            ids.add(int(item))
        except ValueError:
            continue
    return ids


def resolve_data_file_path(value: Optional[str], default_name: str) -> str:
    raw = (value or default_name).strip() or default_name
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return str(path)


TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = parse_optional_int(os.getenv("DISCORD_GUILD_ID"))
COMMAND_SYNC_MODE = os.getenv("COMMAND_SYNC_MODE", "guild" if GUILD_ID else "global").strip().lower()
CLEAR_GLOBAL_COMMANDS_ON_START = parse_bool_with_default(os.getenv("CLEAR_GLOBAL_COMMANDS_ON_START"), False)
ALLOW_EVERYONE_CONTROL = parse_bool_with_default(os.getenv("ALLOW_EVERYONE_CONTROL"), False)
AUTHORIZED_USER_IDS = parse_id_set(os.getenv("AUTHORIZED_USER_IDS"))
AUTHORIZED_ROLE_IDS = parse_id_set(os.getenv("AUTHORIZED_ROLE_IDS"))
PANEL_STATE_FILE = resolve_data_file_path(os.getenv("PANEL_STATE_FILE"), "panel_state.json")
LYRICS_OVERRIDE_FILE = resolve_data_file_path(os.getenv("LYRICS_OVERRIDE_FILE"), "lyrics_overrides.json")
MEDIA_INPUT_NAME = os.getenv("MEDIA_INPUT_NAME", "CABLE Output (VB-Audio Virtual Cable)").strip()
LYRIC_ADVANCE_SECONDS = parse_optional_float(os.getenv("LYRIC_ADVANCE_SECONDS"), 2.0)
LYRICS_TIME_OFFSET_SECONDS = parse_optional_float(os.getenv("LYRICS_TIME_OFFSET_SECONDS"), 0.0)
LYRICS_AUTO_ACCEPT_SCORE = parse_optional_float(os.getenv("LYRICS_AUTO_ACCEPT_SCORE"), 28.0)
MEDIA_REFRESH_SECONDS = max(
    0.1,
    parse_optional_float(os.getenv("MEDIA_REFRESH_SECONDS"), 1.0),
)
PANEL_UPDATE_INTERVAL_SECONDS = max(
    0.0,
    parse_optional_float(os.getenv("PANEL_UPDATE_INTERVAL_SECONDS"), 5.0),
)
AUDIO_SAMPLERATE = max(
    8000,
    parse_int_with_default(os.getenv("AUDIO_SAMPLERATE"), 48000),
)
AUDIO_CHANNELS = max(
    1,
    parse_int_with_default(os.getenv("AUDIO_CHANNELS"), 2),
)
AUDIO_BLOCKSIZE = max(
    120,
    parse_int_with_default(os.getenv("AUDIO_BLOCKSIZE"), 960),
)
AUDIO_QUEUE_SIZE = max(
    1,
    parse_int_with_default(os.getenv("AUDIO_QUEUE_SIZE"), 20),
)
AUDIO_BUFFER_TIMEOUT_SECONDS = max(
    0.0,
    parse_optional_float(os.getenv("AUDIO_BUFFER_TIMEOUT_SECONDS"), 0.02),
)
MAX_LYRIC_CACHE = max(1, parse_int_with_default(os.getenv("MAX_LYRIC_CACHE"), 10))
LYRICS_OVERRIDE_MAX_ENTRIES = max(1, parse_int_with_default(os.getenv("LYRICS_OVERRIDE_MAX_ENTRIES"), 1000))
PRESENCE_REFRESH_SECONDS = max(
    1.0,
    parse_optional_float(os.getenv("PRESENCE_REFRESH_SECONDS"), 10.0),
)
MEDIA_STATE_TIMEOUT_SECONDS = max(
    1.0,
    parse_optional_float(os.getenv("MEDIA_STATE_TIMEOUT_SECONDS"), 5.0),
)
MEDIA_STATE_RETRY_COUNT = max(
    1,
    parse_int_with_default(os.getenv("MEDIA_STATE_RETRY_COUNT"), 3),
)
MEDIA_STATE_RETRY_DELAY_SECONDS = max(
    0.0,
    parse_optional_float(os.getenv("MEDIA_STATE_RETRY_DELAY_SECONDS"), 0.3),
)
LRCLIB_GET_RETRY_COUNT = max(
    0,
    parse_int_with_default(os.getenv("LRCLIB_GET_RETRY_COUNT"), 1),
)
LRCLIB_GET_RETRY_DELAY_SECONDS = max(
    0.0,
    parse_optional_float(os.getenv("LRCLIB_GET_RETRY_DELAY_SECONDS"), 0.5),
)
PCM_FRAME_SIZE = AUDIO_BLOCKSIZE * AUDIO_CHANNELS * 2
RPC_E_CALL_CANCELED = -2147418110  # 0x80010002

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    )

    HAS_MEDIA_CONTROL = True
except Exception:
    MediaManager = None
    HAS_MEDIA_CONTROL = False

logger = logging.getLogger("music_status_bot")


def _consume_task_exception(task: asyncio.Task):
    """避免被放到背景完成的 task 產生 exception was never retrieved 警告。"""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("背景任務稍後完成時發生錯誤")


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

intents = discord.Intents.default()


_PLAYBACK_INT_MAP = {
    0: "closed",
    1: "opened",
    2: "changing",
    3: "stopped",
    4: "playing",
    5: "paused",
}

# 分鐘位數與 parse_lrc 的 \d+ 一致，避免超長曲目標籤殘留。
_TAG_PATTERN = re.compile(r"\[[0-9]+:[0-5]?[0-9](?:\.[0-9]+)?\]")
_TITLE_CLEANUP_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\s*\((?:official|audio|video|lyric|lyrics|visualizer|live|remaster|remastered|explicit|clean|cover)[^)]*\)",
        r"\s*\[(?:official|audio|video|lyric|lyrics|live|remaster|remastered|explicit|clean|cover)[^\]]*\]",
        r"\s*-\s*(?:official|audio|video|lyric|lyrics|live|remaster|remastered|cover).*$",
        r"\s+feat\.?\s+.+$",
        r"\s+ft\.?\s+.+$",
    )
]


def normalize_playback_status(value) -> Optional[str]:
    if value is None:
        return None

    try:
        return _PLAYBACK_INT_MAP.get(int(value))
    except (ValueError, TypeError):
        pass

    raw = str(value).strip()
    if not raw:
        return None

    if "." in raw:
        raw = raw.rsplit(".", 1)[-1]

    return raw.lower() or None


@dataclass
class LyricLine:
    time_seconds: float
    text: str


@dataclass
class LyricCandidate:
    track_name: str
    artist_name: str
    duration: Optional[float]
    synced_lines: list["LyricLine"]
    plain_text: Optional[str]
    lrclib_id: Optional[int] = None
    score: float = 0.0
    source: str = "search"

    @property
    def label(self) -> str:
        dur = f"{int(self.duration // 60)}:{int(self.duration % 60):02d}" if self.duration else "--:--"
        has_synced = "🎵" if self.synced_lines else "📄"
        return f"{has_synced} {self.track_name} — {self.artist_name} ({dur})"[:100]

    @property
    def description(self) -> str:
        source_label = {
            "get": "精準查詢",
            "search": "歌名+歌手搜尋",
            "title": "歌名搜尋",
            "override": "手動記憶",
        }.get(self.source, self.source)
        return f"{source_label} / 信心 {self.score:.1f}"[:100]


@dataclass
class MediaState:
    title: Optional[str] = None
    artist: Optional[str] = None
    app_id: Optional[str] = None
    position_seconds: Optional[float] = None
    duration_seconds: Optional[float] = None
    playback_status: Optional[str] = None

    @property
    def has_track(self) -> bool:
        return bool(self.title)

    @property
    def is_playing(self) -> bool:
        return self.playback_status == "playing"

    @property
    def playback_label(self) -> str:
        if self.playback_status == "playing":
            return "播放中"
        if self.playback_status == "paused":
            return "已暫停"
        return "未播放/未知"



def get_input_device_index(name: str) -> Optional[int]:
    exact_match = None
    partial_match = None

    for idx, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] <= 0:
            continue

        device_name = str(device["name"])
        if device_name.lower() == name.lower():
            exact_match = idx
            break

        if partial_match is None and name.lower() in device_name.lower():
            partial_match = idx

    return exact_match if exact_match is not None else partial_match



def format_seconds(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "--:--"

    total = int(seconds)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"



def build_progress_bar(position: Optional[float], duration: Optional[float], width: int = 18) -> str:
    if position is None or duration is None or duration <= 0:
        return "─" * width

    ratio = max(0.0, min(1.0, position / duration))
    knob = min(width - 1, int(ratio * width))
    chars = []
    for i in range(width):
        if i == knob:
            chars.append("●")
        elif i < knob:
            chars.append("━")
        else:
            chars.append("─")
    return "".join(chars)



def to_seconds(value) -> Optional[float]:
    if value is None:
        return None

    try:
        if hasattr(value, "total_seconds"):
            return float(value.total_seconds())
        if isinstance(value, (int, float)):
            return float(value) / 10_000_000.0
        if hasattr(value, "duration"):
            return float(value.duration) / 10_000_000.0
        if hasattr(value, "seconds") and hasattr(value, "microseconds"):
            return float(value.seconds) + float(value.microseconds) / 1_000_000.0
    except Exception:
        return None

    return None



def sanitize_title(title: str) -> str:
    cleaned = (title or "").strip()
    for pattern in _TITLE_CLEANUP_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—")
    return cleaned or (title or "").strip()



def sanitize_artist(artist: str) -> str:
    cleaned = (artist or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


_SEARCH_TITLE_REMOVE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\s*\((?:feat|ft|featuring)\.?[^)]*\)",
        r"\s*\[(?:feat|ft|featuring)\.?[^\]]*\]",
        r"\s*[-–—]\s*(?:feat|ft|featuring)\.?\s+.*$",
        r"\s*\((?:cover|cover ver|cover version|ver\.?|version|tv size|short ver\.?|inst\.?|instrumental|off vocal|karaoke|remix|live)[^)]*\)",
        r"\s*\[(?:cover|cover ver|cover version|ver\.?|version|tv size|short ver\.?|inst\.?|instrumental|off vocal|karaoke|remix|live)[^\]]*\]",
        r"\s*[-–—]\s*(?:cover|cover ver|cover version|ver\.?|version|tv size|short ver\.?|inst\.?|instrumental|off vocal|karaoke|remix|live).*$",
    )
]



def clamp_text_input_default(value: Optional[str], limit: int = 100) -> str:
    if not value:
        return ""
    return value.strip()[:limit]


def normalize_search_title(title: str) -> str:
    cleaned = sanitize_title(title)

    for pattern in _SEARCH_TITLE_REMOVE_PATTERNS:
        cleaned = pattern.sub("", cleaned)

    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—")
    return cleaned or (title or "").strip()


def normalize_search_artist(artist: str, title: str = "") -> str:
    cleaned = sanitize_artist(artist)

    # 搜尋歌詞時一律只取第一個破折號前的藝人名稱，
    # 顯示時是否保留原始字串由其他地方決定。
    parts = re.split(r"\s+[—–-]\s+", cleaned, maxsplit=1)
    if len(parts) == 2:
        cleaned = parts[0].strip()

    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—")
    return cleaned or (artist or "").strip()



def parse_lrc(text: str) -> list[LyricLine]:
    lines: list[LyricLine] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        tags = re.findall(r"\[(\d+):([0-5]?\d(?:\.\d+)?)\]", line)
        if not tags:
            continue

        lyric_text = _TAG_PATTERN.sub("", line).strip()
        if not lyric_text:
            lyric_text = "♪"

        for minute_text, second_text in tags:
            try:
                minute = int(minute_text)
                second = float(second_text)
            except ValueError:
                continue
            lines.append(LyricLine(time_seconds=minute * 60 + second, text=lyric_text))

    lines.sort(key=lambda item: item.time_seconds)
    return lines



def _http_get_json(
    url: str,
    extra_headers: Optional[dict[str, str]] = None,
    *,
    retries: int = 0,
    retry_delay: float = 0.5,
):
    headers = {
        "User-Agent": f"MusicStatusBot/{VERSION}",
        "Accept": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    request = urllib.request.Request(url, headers=headers)
    last_error: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError:
            # 404 等明確 HTTP 回應不重試，交給呼叫端判斷。
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= retries:
                raise
            logger.warning(
                "HTTP 查詢暫時失敗，%.1f 秒後重試（第 %s/%s 次）：%s",
                retry_delay,
                attempt + 1,
                retries,
                exc,
            )
            time.sleep(retry_delay)

    if last_error is not None:
        raise last_error
    raise RuntimeError("HTTP 查詢失敗")



def _log_lrclib_transient_failure(stage: str, exc: BaseException) -> None:
    logger.warning("LRCLIB %s 暫時無法連線或逾時，已略過本次歌詞查詢：%s", stage, exc)




_VERSION_KEYWORD_SOURCE = {
    "live": [r"\blive\b", r"ライブ"],
    "remix": [r"\bremix(?:ed)?\b", r"リミックス"],
    "remaster": [r"\bremaster(?:ed)?\b", r"\b\d{4}\s*remaster(?:ed)?\b"],
    "acoustic": [r"\bacoustic\b", r"アコースティック"],
    "instrumental": [r"\binstrumental\b", r"\binst\.?\b", r"off\s*vocal", r"カラオケ"],
    "karaoke": [r"\bkaraoke\b"],
    "cover": [r"\bcover\b", r"歌ってみた"],
    "tv_size": [r"\btv\s*size\b", r"\bshort\s*ver\.?\b", r"ショート"],
    "movie": [r"\bmovie\s*ver\.?\b"],
    "radio_edit": [r"\bradio\s*edit\b"],
    "extended": [r"\bextended\b"],
    "feat": [r"\bfeat(?:uring)?\.?\b", r"\bft\.?\b"],
}

_VERSION_KEYWORD_PATTERNS = {
    tag: [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    for tag, patterns in _VERSION_KEYWORD_SOURCE.items()
}


def normalize_compare_text(value: str) -> str:
    text = (value or "").lower()
    text = text.replace("’", "'").replace("`", "'")
    text = re.sub(r"[\(\)\[\]{}]", " ", text)
    text = re.sub(r"[|/\\:;,_~]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -–—")
    return text


def extract_version_tags(value: str) -> set[str]:
    text = (value or "").lower()
    tags: set[str] = set()
    for tag, patterns in _VERSION_KEYWORD_PATTERNS.items():
        if any(pattern.search(text) for pattern in patterns):
            tags.add(tag)
    return tags


def split_artist_tokens(value: str) -> set[str]:
    raw = (value or "").lower()
    raw = re.sub(r"\b(feat(?:uring)?|ft|with)\.?\b", ",", raw, flags=re.IGNORECASE)
    parts = re.split(r"\s*(?:,|、|/|&|\+| x |×| and )\s*", raw)
    tokens = set()
    for part in parts:
        normalized = normalize_compare_text(part)
        if normalized:
            tokens.add(normalized)
    return tokens


def parse_item_duration(item: dict) -> Optional[float]:
    value = item.get("duration")
    try:
        return float(value) if value is not None else None
    except Exception:
        return None


def parse_item_id(item: dict) -> Optional[int]:
    value = item.get("id")
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _score_lyrics_candidate(item: dict, *, title: str, artist: str, duration: Optional[float], source: str = "search") -> float:
    score = 0.0
    track_name = str(item.get("trackName") or item.get("name") or "").strip()
    artist_name = str(item.get("artistName") or "").strip()

    target_title = normalize_compare_text(title)
    candidate_title = normalize_compare_text(track_name)
    target_artist = normalize_compare_text(artist)
    candidate_artist = normalize_compare_text(artist_name)

    if candidate_title and target_title:
        if candidate_title == target_title:
            score += 14
        elif target_title in candidate_title or candidate_title in target_title:
            score += 8
        else:
            ratio = SequenceMatcher(None, target_title, candidate_title).ratio()
            if ratio >= 0.88:
                score += 6
            elif ratio >= 0.78:
                score += 3
            else:
                score -= 4

    if target_artist:
        if candidate_artist == target_artist:
            score += 10
        elif candidate_artist and (target_artist in candidate_artist or candidate_artist in target_artist):
            score += 6
        else:
            target_tokens = split_artist_tokens(artist)
            candidate_tokens = split_artist_tokens(artist_name)
            overlap = target_tokens & candidate_tokens
            if overlap:
                score += min(8.0, 2.5 * len(overlap))
            elif candidate_artist:
                score -= 5
            else:
                score -= 2

    target_tags = extract_version_tags(title)
    candidate_tags = extract_version_tags(track_name)

    for tag in sorted(target_tags & candidate_tags):
        score += 2 if tag != "feat" else 1

    for tag in sorted(candidate_tags - target_tags):
        if tag == "feat":
            score -= 2
        elif tag in {"instrumental", "karaoke"}:
            score -= 10
        else:
            score -= 8

    for tag in sorted(target_tags - candidate_tags):
        if tag not in {"feat"}:
            score -= 3

    item_duration = parse_item_duration(item)
    if duration is not None and duration > 0:
        if item_duration is None:
            score -= 2
        else:
            delta = abs(float(item_duration) - float(duration))
            if delta <= 1:
                score += 8
            elif delta <= 3:
                score += 5
            elif delta <= 6:
                score += 2
            elif delta <= 8:
                score += 0
            else:
                score -= 6

    if item.get("instrumental") and "instrumental" not in target_tags and "karaoke" not in target_tags:
        score -= 8

    if item.get("syncedLyrics"):
        score += 5
    elif item.get("plainLyrics"):
        score += 1

    if source == "get":
        score += 3

    return score


def make_lyric_candidate_from_item(
    item: dict,
    *,
    title: str,
    artist: str,
    duration: Optional[float],
    source: str,
) -> Optional[LyricCandidate]:
    synced_text = str(item.get("syncedLyrics") or "").strip()
    plain_text = str(item.get("plainLyrics") or "").strip()

    if not synced_text and not plain_text:
        return None

    synced_lines = parse_lrc(synced_text) if synced_text else []
    score = _score_lyrics_candidate(
        item,
        title=title,
        artist=artist,
        duration=duration,
        source=source,
    )

    return LyricCandidate(
        track_name=str(item.get("trackName") or item.get("name") or title or "").strip(),
        artist_name=str(item.get("artistName") or artist or "").strip(),
        duration=parse_item_duration(item),
        synced_lines=synced_lines,
        plain_text=plain_text or None,
        lrclib_id=parse_item_id(item),
        score=score,
        source=source,
    )


def _candidate_dedupe_key(candidate: LyricCandidate) -> str:
    if candidate.lrclib_id is not None:
        return f"id:{candidate.lrclib_id}"
    duration_key = str(int(round(candidate.duration or 0)))
    return "|".join(
        [
            normalize_compare_text(candidate.track_name),
            normalize_compare_text(candidate.artist_name),
            duration_key,
            "synced" if candidate.synced_lines else "plain",
        ]
    )


def _add_candidate(
    candidates_by_key: dict[str, LyricCandidate],
    item: dict,
    *,
    title: str,
    artist: str,
    duration: Optional[float],
    source: str,
):
    candidate = make_lyric_candidate_from_item(
        item,
        title=title,
        artist=artist,
        duration=duration,
        source=source,
    )
    if candidate is None:
        return

    key = _candidate_dedupe_key(candidate)
    old = candidates_by_key.get(key)
    if old is None:
        candidates_by_key[key] = candidate
        return

    # 同一筆資料重複出現時，保留較高信心分；若分數相近，優先保留同步歌詞。
    if candidate.score > old.score or (
        abs(candidate.score - old.score) < 0.01 and candidate.synced_lines and not old.synced_lines
    ):
        candidates_by_key[key] = candidate


def _candidate_result(
    candidate: LyricCandidate,
    status_text: str,
    candidates: list[LyricCandidate],
) -> tuple[list[LyricLine], str, Optional[str], list[LyricCandidate]]:
    if candidate.synced_lines:
        return candidate.synced_lines, status_text, None, candidates
    return [], status_text, candidate.plain_text, candidates


def fetch_synced_lyrics_for_track(
    title: str, artist: str, duration: Optional[float], auto_select: bool = True
) -> tuple[list[LyricLine], str, Optional[str], list[LyricCandidate]]:
    if not title:
        return [], "目前沒有可顯示的同步歌詞。", None, []

    cleaned_title = normalize_search_title(title)
    cleaned_artist = normalize_search_artist(artist, title)
    candidates_by_key: dict[str, LyricCandidate] = {}

    params = {
        "track_name": cleaned_title,
        "artist_name": cleaned_artist,
    }
    if duration is not None and duration > 0:
        params["duration"] = str(int(round(duration)))

    get_url = f"{LRCLIB_BASE_URL}/get?{urllib.parse.urlencode(params)}"

    try:
        item = _http_get_json(get_url, retries=LRCLIB_GET_RETRY_COUNT, retry_delay=LRCLIB_GET_RETRY_DELAY_SECONDS)
        if isinstance(item, dict):
            _add_candidate(
                candidates_by_key,
                item,
                title=cleaned_title,
                artist=cleaned_artist,
                duration=duration,
                source="get",
            )
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            logger.warning("LRCLIB /get 查詢失敗：%s", exc)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _log_lrclib_transient_failure("/get", exc)
    except Exception:
        logger.exception("LRCLIB /get 發生未預期錯誤")

    search_queries: list[tuple[str, str, str]] = []
    best_after_get = max(candidates_by_key.values(), key=lambda c: c.score, default=None)
    if not (auto_select and best_after_get is not None and best_after_get.score >= LYRICS_AUTO_ACCEPT_SCORE):
        query = " ".join(part for part in [cleaned_title, cleaned_artist] if part).strip()
        if query:
            search_queries.append((query, cleaned_artist, "search"))
        if cleaned_title and cleaned_title != query:
            search_queries.append((cleaned_title, cleaned_artist, "title"))

    seen_queries: set[str] = set()
    for search_query, search_artist, source in search_queries:
        if not search_query or search_query in seen_queries:
            continue
        seen_queries.add(search_query)

        try:
            search_url = f"{LRCLIB_BASE_URL}/search?{urllib.parse.urlencode({'q': search_query})}"
            items = _http_get_json(search_url, retries=LRCLIB_GET_RETRY_COUNT, retry_delay=LRCLIB_GET_RETRY_DELAY_SECONDS)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        _add_candidate(
                            candidates_by_key,
                            item,
                            title=cleaned_title,
                            artist=search_artist,
                            duration=duration,
                            source=source,
                        )
        except urllib.error.HTTPError as exc:
            logger.warning("LRCLIB /search 查詢失敗：%s", exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            _log_lrclib_transient_failure("/search", exc)
        except Exception:
            logger.exception("LRCLIB /search 發生未預期錯誤")

    candidates = sorted(candidates_by_key.values(), key=lambda c: c.score, reverse=True)[:24]
    if not candidates:
        return [], "查不到這首歌的同步歌詞。", None, []

    best = candidates[0]
    if auto_select and best.score >= LYRICS_AUTO_ACCEPT_SCORE:
        status_text = (
            f"已自動套用同步歌詞：{best.track_name} — {best.artist_name}（信心 {best.score:.1f}）"
            if best.synced_lines
            else f"已自動套用完整歌詞：{best.track_name} — {best.artist_name}（信心 {best.score:.1f}）"
        )
        return _candidate_result(best, status_text, candidates)

    if auto_select:
        return (
            [],
            f"找到 {len(candidates)} 筆可能歌詞，但信心不足（最高 {best.score:.1f}，門檻 {LYRICS_AUTO_ACCEPT_SCORE:.1f}），請手動選擇。",
            None,
            candidates,
        )

    return [], f"找到 {len(candidates)} 筆可能歌詞，請手動選擇。", None, candidates



def serialize_lyric_lines(lines: list[LyricLine]) -> list[dict]:
    return [{"time_seconds": line.time_seconds, "text": line.text} for line in lines]


def deserialize_lyric_lines(items) -> list[LyricLine]:
    lines: list[LyricLine] = []
    if not isinstance(items, list):
        return lines
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            lines.append(LyricLine(time_seconds=float(item.get("time_seconds", 0)), text=str(item.get("text", ""))))
        except Exception:
            continue
    lines.sort(key=lambda line: line.time_seconds)
    return lines


def write_json_atomic(path: str, data: dict):
    target_path = os.path.abspath(path)
    target_dir = os.path.dirname(target_path) or "."
    os.makedirs(target_dir, exist_ok=True)
    tmp_path = f"{target_path}.{os.getpid()}.tmp"

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


class MediaInputSource(discord.AudioSource):
    def __init__(
        self,
        *,
        device_index: int,
        samplerate: int = AUDIO_SAMPLERATE,
        channels: int = AUDIO_CHANNELS,
        blocksize: int = AUDIO_BLOCKSIZE,
        queue_size: int = AUDIO_QUEUE_SIZE,
    ):
        self._closed = False
        self._stop_event = threading.Event()
        self._buffer: queue.Queue[bytes] = queue.Queue(maxsize=queue_size)
        self._samplerate = samplerate
        self._channels = channels
        self._blocksize = blocksize
        self._frame_size = blocksize * channels * 2

        self.stream = None
        try:
            sd.check_input_settings(
                device=device_index,
                channels=channels,
                samplerate=samplerate,
                dtype="int16",
            )

            self.stream = sd.InputStream(
                device=device_index,
                channels=channels,
                samplerate=samplerate,
                dtype="int16",
                blocksize=blocksize,
                callback=self._callback,
            )
            self.stream.start()
        except Exception:
            if self.stream is not None:
                try:
                    self.stream.close()
                except Exception:
                    pass
            raise

    def _normalize_frame(self, data: bytes) -> bytes:
        if len(data) == self._frame_size:
            return data
        if len(data) < self._frame_size:
            return data + bytes(self._frame_size - len(data))
        return data[:self._frame_size]

    def _callback(self, indata, frames, time_info, status):
        if status:
            logger.warning("sounddevice 狀態警告: %s", status)

        if self._stop_event.is_set():
            return

        try:
            data = self._normalize_frame(indata.copy().tobytes())
            self._buffer.put_nowait(data)
        except queue.Full:
            pass
        except Exception:
            logger.exception("音訊 callback 發生錯誤")

    def read(self) -> bytes:
        if self._stop_event.is_set():
            return bytes(self._frame_size)

        try:
            return self._normalize_frame(self._buffer.get(timeout=AUDIO_BUFFER_TIMEOUT_SECONDS))
        except queue.Empty:
            return bytes(self._frame_size)

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        if self._closed:
            return

        self._closed = True
        self._stop_event.set()

        if self.stream is None:
            return

        try:
            self.stream.stop()
        except Exception:
            pass

        try:
            self.stream.close()
        except Exception:
            pass


async def cleanup_voice_client(vc: discord.VoiceClient):
    source = vc.source

    try:
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await asyncio.sleep(0.2)
    except Exception:
        logger.exception("停止播放時發生錯誤")

    if source and hasattr(source, "cleanup"):
        try:
            source.cleanup()
        except Exception:
            logger.exception("清理音源時發生錯誤")


async def get_media_state() -> MediaState:
    global HAS_MEDIA_CONTROL

    if not HAS_MEDIA_CONTROL:
        return MediaState()

    for attempt in range(MEDIA_STATE_RETRY_COUNT):
        try:
            manager = await MediaManager.request_async()
            current = manager.get_current_session()
            if current is None:
                return MediaState()

            props = await current.try_get_media_properties_async()
            timeline = current.get_timeline_properties()
            playback = current.get_playback_info()

            title = str(getattr(props, "title", "") or "").strip() or None
            artist = str(getattr(props, "artist", "") or "").strip() or None
            app_id = str(current.source_app_user_model_id or "").strip() or None

            playback_status = getattr(playback, "playback_status", None)
            status_text = normalize_playback_status(playback_status)

            position_seconds = to_seconds(getattr(timeline, "position", None))
            start_seconds = to_seconds(getattr(timeline, "start_time", None))
            end_seconds = to_seconds(getattr(timeline, "end_time", None))

            duration_seconds = None
            if start_seconds is not None and end_seconds is not None and end_seconds >= start_seconds:
                duration_seconds = max(0.0, end_seconds - start_seconds)
                if position_seconds is not None:
                    position_seconds = max(0.0, position_seconds - start_seconds)

            return MediaState(
                title=title,
                artist=artist,
                app_id=app_id,
                position_seconds=position_seconds,
                duration_seconds=duration_seconds,
                playback_status=status_text,
            )
        except ModuleNotFoundError as exc:
            HAS_MEDIA_CONTROL = False
            logger.warning("媒體資訊功能停用：%s", exc)
            return MediaState()
        except OSError as exc:
            if getattr(exc, "winerror", None) == RPC_E_CALL_CANCELED:
                if attempt + 1 < MEDIA_STATE_RETRY_COUNT:
                    logger.warning(
                        "讀取媒體資訊被 Windows 取消（第 %s/%s 次），%.1f 秒後重試",
                        attempt + 1,
                        MEDIA_STATE_RETRY_COUNT,
                        MEDIA_STATE_RETRY_DELAY_SECONDS,
                    )
                    await asyncio.sleep(MEDIA_STATE_RETRY_DELAY_SECONDS)
                    continue

                logger.warning("讀取媒體資訊被 Windows 取消，沿用上次狀態")
                return MediaState()

            logger.exception("讀取媒體資訊失敗")
            return MediaState()
        except Exception:
            logger.exception("讀取媒體資訊失敗")
            return MediaState()

    return MediaState()



async def get_current_media_session():
    global HAS_MEDIA_CONTROL

    if not HAS_MEDIA_CONTROL:
        return None

    try:
        manager = await MediaManager.request_async()
        return manager.get_current_session()
    except ModuleNotFoundError as exc:
        HAS_MEDIA_CONTROL = False
        logger.warning("控制功能停用：%s", exc)
        return None
    except Exception:
        logger.exception("取得媒體 session 失敗")
        return None


async def control_media(action: str) -> tuple[bool, str]:
    if not HAS_MEDIA_CONTROL:
        return False, "目前環境不支援媒體控制功能。"

    session = await get_current_media_session()
    if session is None:
        return False, "目前找不到可控制的播放器。"

    try:
        if action == "play":
            result = await session.try_play_async()
        elif action == "pause":
            result = await session.try_pause_async()
        elif action == "next":
            result = await session.try_skip_next_async()
        elif action == "prev":
            result = await session.try_skip_previous_async()
        else:
            return False, "不支援的控制動作。"

        if result:
            return True, "控制命令已送出。"
        return False, "播放器沒有接受這個控制命令。"
    except Exception:
        logger.exception("控制播放器失敗: %s", action)
        return False, "控制播放器時發生錯誤。"


async def send_ephemeral(interaction: discord.Interaction, content: str):
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


async def ensure_allowed_guild(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        await send_ephemeral(interaction, "這個指令只能在伺服器中使用。")
        return False

    if GUILD_ID and interaction.guild.id != GUILD_ID:
        await send_ephemeral(interaction, "這個 Bot 只允許在指定的單一伺服器中使用。")
        return False

    return True


async def ensure_authorized_user(interaction: discord.Interaction) -> bool:
    if ALLOW_EVERYONE_CONTROL:
        return True

    user = interaction.user
    if user.id in AUTHORIZED_USER_IDS:
        return True

    if isinstance(user, discord.Member):
        permissions = user.guild_permissions
        if permissions.administrator or permissions.manage_guild:
            return True
        if AUTHORIZED_ROLE_IDS and any(role.id in AUTHORIZED_ROLE_IDS for role in user.roles):
            return True

    client = interaction.client
    if isinstance(client, commands.Bot):
        try:
            if await client.is_owner(user):
                return True
        except Exception:
            pass

    await send_ephemeral(interaction, "你沒有權限操作這個音樂機器人。")
    return False


async def ensure_command_allowed(interaction: discord.Interaction) -> bool:
    return await ensure_allowed_guild(interaction) and await ensure_authorized_user(interaction)


class MusicStatusBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.media_task: Optional[asyncio.Task] = None
        self.lyric_fetch_task: Optional[asyncio.Task] = None
        self.pending_media_task: Optional[asyncio.Task] = None
        self.panel_restore_task: Optional[asyncio.Task] = None
        self.cached_media_state = MediaState()
        self.last_presence_name: Optional[str] = None
        self.panel_message: Optional[discord.Message] = None
        self.panel_guild_id: Optional[int] = None
        self.last_panel_signature: Optional[str] = None
        self.last_panel_update_monotonic: float = 0.0
        self.voice_lock = asyncio.Lock()
        self.panel_lock = asyncio.Lock()
        self.panel_view: Optional["PanelView"] = None
        self._close_started = False
        self.lyric_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lyrics")
        self.lyric_request_generation = 0

        self.lyric_lines: list[LyricLine] = []
        self.current_lyric_index: int = -1
        self.plain_lyrics_text: Optional[str] = None
        self.last_track_key: Optional[str] = None
        self.last_media_snapshot_monotonic: float = time.monotonic()
        self.last_media_position_seconds: float = 0.0
        self.lyric_status_text: str = "目前沒有可顯示的同步歌詞。"
        self.lyric_cache: OrderedDict[str, tuple[list[LyricLine], Optional[str], list[LyricCandidate]]] = OrderedDict()
        self.lyric_status_cache: OrderedDict[str, str] = OrderedDict()
        self.lyric_overrides: dict[str, dict] = self.load_lyrics_overrides()

        # 歌詞 UI 狀態
        self.lyrics_display_enabled: bool = True
        self.lyrics_auto_select_enabled: bool = True
        self.lyrics_mode: str = "auto"  # "auto" | "synced" | "plain"
        self.cover_candidates: list[LyricCandidate] = []

    def get_panel_view(self) -> "PanelView":
        if self.panel_view is None:
            self.panel_view = PanelView(self)
        else:
            self.panel_view.refresh()
        return self.panel_view

    async def sync_application_commands(self):
        mode = COMMAND_SYNC_MODE
        if mode in {"off", "none", "false", "0"}:
            if CLEAR_GLOBAL_COMMANDS_ON_START:
                self.tree.clear_commands(guild=None)
                global_synced = await self.tree.sync()
                logger.info("已清理全域指令，目前全域指令數：%s", len(global_synced))
            else:
                logger.info("已略過斜線指令同步")
            return

        if mode == "guild":
            if not GUILD_ID:
                logger.warning("COMMAND_SYNC_MODE=guild 但沒有設定 DISCORD_GUILD_ID，改用全域同步")
                synced = await self.tree.sync()
                logger.info("已同步 %s 個全域斜線指令", len(synced))
                return

            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            guild_synced = await self.tree.sync(guild=guild)
            logger.info("已同步 %s 個伺服器指令到 guild %s", len(guild_synced), GUILD_ID)

            if CLEAR_GLOBAL_COMMANDS_ON_START:
                self.tree.clear_commands(guild=None)
                global_synced = await self.tree.sync()
                logger.info("已清理全域指令，目前全域指令數：%s", len(global_synced))
            return

        if mode == "global":
            if CLEAR_GLOBAL_COMMANDS_ON_START:
                logger.warning("COMMAND_SYNC_MODE=global 時不會自動清理全域指令，避免把正式全域指令清空")
            synced = await self.tree.sync()
            logger.info("已同步 %s 個全域斜線指令", len(synced))
            return

        logger.warning("未知的 COMMAND_SYNC_MODE=%s，已略過斜線指令同步", COMMAND_SYNC_MODE)

    async def setup_hook(self):
        self.add_view(self.get_panel_view())
        await self.sync_application_commands()
        self.media_task = asyncio.create_task(self.media_loop())
        self.panel_restore_task = asyncio.create_task(self.restore_panel_from_state())

    async def close(self):
        if self._close_started:
            return

        self._close_started = True

        if self.media_task:
            self.media_task.cancel()
            try:
                await self.media_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("關閉背景更新任務失敗")
            finally:
                self.media_task = None

        if self.lyric_fetch_task:
            self.lyric_fetch_task.cancel()
            try:
                await self.lyric_fetch_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("關閉歌詞查詢任務失敗")
            finally:
                self.lyric_fetch_task = None

        if self.panel_restore_task:
            self.panel_restore_task.cancel()
            try:
                await self.panel_restore_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("關閉面板恢復任務失敗")
            finally:
                self.panel_restore_task = None

        if self.pending_media_task:
            pending_task = self.pending_media_task
            self.pending_media_task = None
            pending_task.cancel()
            try:
                done, _ = await asyncio.wait({pending_task}, timeout=1.0)
                if pending_task in done:
                    try:
                        await pending_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        logger.exception("關閉 pending_media_task 失敗")
                else:
                    logger.warning("pending_media_task 未能在 timeout 內結束，將在完成後消化結果")
                    pending_task.add_done_callback(_consume_task_exception)
            except Exception:
                logger.exception("等待 pending_media_task 關閉時發生錯誤")

        self.lyric_executor.shutdown(wait=False, cancel_futures=True)

        for vc in list(self.voice_clients):
            try:
                await cleanup_voice_client(vc)
                if vc.is_connected():
                    await vc.disconnect(force=True)
            except Exception:
                logger.exception("關閉語音連線失敗")

        await super().close()

    def build_track_key(self, state: MediaState) -> Optional[str]:
        if not state.has_track:
            return None
        return "|".join(
            [
                normalize_search_title(state.title or ""),
                normalize_search_artist(state.artist or "", state.title or ""),
                str(int(state.duration_seconds or 0)),
            ]
        )

    def estimate_position_seconds(self) -> Optional[float]:
        state = self.cached_media_state
        if state.position_seconds is None:
            return None

        if not state.is_playing:
            return state.position_seconds

        elapsed = time.monotonic() - self.last_media_snapshot_monotonic
        position = self.last_media_position_seconds + elapsed
        if state.duration_seconds is not None:
            position = min(position, state.duration_seconds)
        return max(0.0, position)

    def estimate_lyric_position_seconds(self) -> Optional[float]:
        position = self.estimate_position_seconds()
        if position is None:
            return None
        return max(0.0, position + LYRIC_ADVANCE_SECONDS + LYRICS_TIME_OFFSET_SECONDS)

    def get_current_lyric_index(self, position: Optional[float]) -> int:
        if position is None or not self.lyric_lines:
            return -1

        index = -1
        for idx, line in enumerate(self.lyric_lines):
            if line.time_seconds <= position:
                index = idx
            else:
                break
        return index

    def reset_lyrics(self, status_text: str):
        self.lyric_lines = []
        self.current_lyric_index = -1
        self.plain_lyrics_text = None
        self.cover_candidates = []
        self.lyric_status_text = status_text

    def get_cached_lyrics(self, track_key: str) -> Optional[tuple[list[LyricLine], str, Optional[str], list[LyricCandidate]]]:
        entry = self.lyric_cache.get(track_key)
        if entry is None:
            return None

        lines, plain_text, candidates = entry
        status_text = self.lyric_status_cache.get(track_key, "已載入同步歌詞。")
        self.lyric_cache.move_to_end(track_key)
        self.lyric_status_cache.move_to_end(track_key)
        return lines, status_text, plain_text, candidates

    def store_cached_lyrics(
        self,
        track_key: str,
        lines: list[LyricLine],
        status_text: str,
        plain_text: Optional[str],
        candidates: list[LyricCandidate],
    ):
        self.lyric_cache[track_key] = (lines, plain_text, candidates)
        self.lyric_status_cache[track_key] = status_text
        self.lyric_cache.move_to_end(track_key)
        self.lyric_status_cache.move_to_end(track_key)

        while len(self.lyric_cache) > MAX_LYRIC_CACHE:
            oldest_key, _ = self.lyric_cache.popitem(last=False)
            self.lyric_status_cache.pop(oldest_key, None)

    def load_lyrics_overrides(self) -> dict[str, dict]:
        if not os.path.exists(LYRICS_OVERRIDE_FILE):
            return {}
        try:
            with open(LYRICS_OVERRIDE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            logger.warning("lyrics_overrides.json 格式錯誤，已略過讀取")
        except Exception:
            logger.exception("讀取歌詞手動記憶失敗")
        return {}

    def prune_lyrics_overrides(self):
        if len(self.lyric_overrides) <= LYRICS_OVERRIDE_MAX_ENTRIES:
            return

        def sort_key(item):
            _, entry = item
            if isinstance(entry, dict):
                try:
                    return int(entry.get("updated_at") or 0)
                except Exception:
                    return 0
            return 0

        sorted_items = sorted(self.lyric_overrides.items(), key=sort_key)
        remove_count = len(sorted_items) - LYRICS_OVERRIDE_MAX_ENTRIES
        for key, _ in sorted_items[:remove_count]:
            self.lyric_overrides.pop(key, None)

    def save_lyrics_overrides(self):
        try:
            self.prune_lyrics_overrides()
            write_json_atomic(LYRICS_OVERRIDE_FILE, self.lyric_overrides)
        except Exception:
            logger.exception("寫入歌詞手動記憶失敗")

    def get_lyrics_override(
        self,
        track_key: str,
    ) -> Optional[tuple[list[LyricLine], str, Optional[str], list[LyricCandidate]]]:
        entry = self.lyric_overrides.get(track_key)
        if not isinstance(entry, dict):
            return None

        status_text = str(entry.get("status_text") or "已套用手動記憶歌詞。")
        if entry.get("type") == "none":
            return [], status_text, None, []

        lines = deserialize_lyric_lines(entry.get("synced_lines"))
        plain_text = entry.get("plain_text")
        if plain_text is not None:
            plain_text = str(plain_text)

        try:
            duration = float(entry["duration"]) if entry.get("duration") is not None else None
        except Exception:
            duration = None
        try:
            lrclib_id = int(entry["lrclib_id"]) if entry.get("lrclib_id") is not None else None
        except Exception:
            lrclib_id = None
        try:
            score = float(entry.get("score") or 0.0)
        except Exception:
            score = 0.0

        candidate = LyricCandidate(
            track_name=str(entry.get("track_name") or ""),
            artist_name=str(entry.get("artist_name") or ""),
            duration=duration,
            synced_lines=lines,
            plain_text=plain_text,
            lrclib_id=lrclib_id,
            score=score,
            source="override",
        )
        candidates = [candidate] if (lines or plain_text) else []
        return lines, status_text, plain_text, candidates

    def save_lyrics_override_for_candidate(self, track_key: str, candidate: LyricCandidate, status_text: str):
        self.lyric_overrides[track_key] = {
            "type": "lyrics",
            "status_text": status_text,
            "track_name": candidate.track_name,
            "artist_name": candidate.artist_name,
            "duration": candidate.duration,
            "lrclib_id": candidate.lrclib_id,
            "score": candidate.score,
            "source": candidate.source,
            "synced_lines": serialize_lyric_lines(candidate.synced_lines),
            "plain_text": candidate.plain_text,
            "updated_at": int(time.time()),
        }
        self.save_lyrics_overrides()

    def save_lyrics_override_none(self, track_key: str, status_text: str):
        self.lyric_overrides[track_key] = {
            "type": "none",
            "status_text": status_text,
            "updated_at": int(time.time()),
        }
        self.save_lyrics_overrides()

    async def get_media_state_safely(
        self,
        timeout: Optional[float] = None,
        fallback: Optional[MediaState] = None,
    ) -> MediaState:
        effective_timeout = MEDIA_STATE_TIMEOUT_SECONDS if timeout is None else timeout

        if self.pending_media_task is not None:
            if self.pending_media_task.done():
                previous_task = self.pending_media_task
                self.pending_media_task = None
                try:
                    stale_result = previous_task.result()
                    if stale_result.has_track or fallback is None:
                        fallback = stale_result
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("上一輪 get_media_state 任務失敗")
            else:
                logger.warning("上一個 get_media_state 任務尚未完成，使用上次狀態")
                return fallback if fallback is not None else MediaState()

        task = asyncio.create_task(get_media_state())
        self.pending_media_task = task
        done, _ = await asyncio.wait({task}, timeout=effective_timeout)

        if task in done:
            self.pending_media_task = None
            try:
                return task.result()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("get_media_state 背景任務失敗")
                return fallback if fallback is not None else MediaState()

        logger.warning("get_media_state 超時（%.1fs），使用上次狀態", effective_timeout)
        return fallback if fallback is not None else MediaState()

    def save_panel_state(self):
        if self.panel_message is None or self.panel_guild_id is None:
            return

        data = {
            "guild_id": self.panel_guild_id,
            "channel_id": self.panel_message.channel.id,
            "message_id": self.panel_message.id,
        }
        try:
            write_json_atomic(PANEL_STATE_FILE, data)
        except Exception:
            logger.exception("儲存面板狀態失敗")

    def delete_panel_state(self):
        try:
            if os.path.exists(PANEL_STATE_FILE):
                os.remove(PANEL_STATE_FILE)
        except Exception:
            logger.exception("刪除面板狀態失敗")

    async def restore_panel_from_state(self):
        await self.wait_until_ready()
        if not os.path.exists(PANEL_STATE_FILE):
            return

        try:
            with open(PANEL_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            guild_id = int(data["guild_id"])
            channel_id = int(data["channel_id"])
            message_id = int(data["message_id"])

            channel = self.get_channel(channel_id)
            if channel is None:
                channel = await self.fetch_channel(channel_id)

            if not hasattr(channel, "fetch_message"):
                logger.warning("面板狀態指向的頻道不是文字頻道，已清除面板狀態")
                self.delete_panel_state()
                return

            message = await channel.fetch_message(message_id)
            actual_guild_id = getattr(getattr(channel, "guild", None), "id", None)
            if actual_guild_id is not None and actual_guild_id != guild_id:
                logger.warning("面板狀態的 guild_id 與頻道實際 guild 不符，已清除面板狀態")
                self.delete_panel_state()
                return

            self.panel_message = message
            self.panel_guild_id = actual_guild_id or guild_id
            self.last_panel_signature = None
            self.last_panel_update_monotonic = 0.0
            logger.info(
                "已恢復狀態面板：guild=%s channel=%s message=%s",
                self.panel_guild_id,
                channel_id,
                message_id,
            )
        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
            discord.InvalidData,
            KeyError,
            ValueError,
            json.JSONDecodeError,
        ):
            logger.warning("無法恢復狀態面板，已清除面板狀態檔", exc_info=True)
            self.delete_panel_state()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("恢復狀態面板失敗")

    async def fetch_lyrics_in_executor(self, state: MediaState) -> tuple[list[LyricLine], str, Optional[str], list[LyricCandidate]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self.lyric_executor,
            fetch_synced_lyrics_for_track,
            state.title or "",
            state.artist or "",
            state.duration_seconds,
            self.lyrics_auto_select_enabled,
        )

    async def load_lyrics_for_track(self, track_key: str, state: MediaState, request_generation: int):
        try:
            override = self.get_lyrics_override(track_key)
            if override is not None:
                lines, status_text, plain_text, candidates = override
                if self.last_track_key == track_key and self.lyric_request_generation == request_generation:
                    self.lyric_lines = lines
                    self.lyric_status_text = status_text
                    self.plain_lyrics_text = plain_text
                    self.cover_candidates = candidates
                    self.current_lyric_index = self.get_current_lyric_index(self.estimate_lyric_position_seconds())
                    await self.update_panel_if_needed(force=True)
                return

            cached = self.get_cached_lyrics(track_key)
            if cached is not None:
                if self.last_track_key == track_key and self.lyric_request_generation == request_generation:
                    lines, status_text, plain_text, candidates = cached
                    self.lyric_lines = lines
                    self.lyric_status_text = status_text
                    self.plain_lyrics_text = plain_text
                    self.cover_candidates = candidates
                    self.current_lyric_index = self.get_current_lyric_index(self.estimate_lyric_position_seconds())
                    await self.update_panel_if_needed(force=True)
                return

            lines, status_text, plain_text, candidates = await self.fetch_lyrics_in_executor(state)
            self.store_cached_lyrics(track_key, lines, status_text, plain_text, candidates)

            if self.last_track_key != track_key or self.lyric_request_generation != request_generation:
                return

            self.lyric_lines = lines
            self.lyric_status_text = status_text
            self.plain_lyrics_text = plain_text
            self.cover_candidates = candidates
            # 自動套用時記錄是第 0 筆（評分最高）
            self.current_lyric_index = self.get_current_lyric_index(self.estimate_lyric_position_seconds())
            await self.update_panel_if_needed(force=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("載入歌詞失敗")
            if self.last_track_key == track_key and self.lyric_request_generation == request_generation:
                self.reset_lyrics("查詢歌詞時發生錯誤。")
                await self.update_panel_if_needed(force=True)

    async def media_loop(self):
        await self.wait_until_ready()
        tick = 0
        presence_every_ticks = max(1, int(round(PRESENCE_REFRESH_SECONDS / MEDIA_REFRESH_SECONDS)))

        while not self.is_closed():
            try:
                self.cached_media_state = await self.get_media_state_safely(
                    timeout=MEDIA_STATE_TIMEOUT_SECONDS, fallback=self.cached_media_state
                )

                self.last_media_snapshot_monotonic = time.monotonic()
                self.last_media_position_seconds = self.cached_media_state.position_seconds or 0.0

                track_key = self.build_track_key(self.cached_media_state)
                if track_key != self.last_track_key:
                    self.last_track_key = track_key
                    self.lyric_request_generation += 1
                    current_generation = self.lyric_request_generation

                    if self.lyric_fetch_task:
                        self.lyric_fetch_task.cancel()
                        self.lyric_fetch_task = None

                    if track_key is None:
                        self.reset_lyrics("目前沒有可顯示的同步歌詞。")
                    else:
                        self.reset_lyrics("正在查詢歌詞...")
                        state_snapshot = MediaState(
                            title=self.cached_media_state.title,
                            artist=self.cached_media_state.artist,
                            app_id=self.cached_media_state.app_id,
                            position_seconds=self.cached_media_state.position_seconds,
                            duration_seconds=self.cached_media_state.duration_seconds,
                            playback_status=self.cached_media_state.playback_status,
                        )
                        self.lyric_fetch_task = asyncio.create_task(
                            self.load_lyrics_for_track(track_key, state_snapshot, current_generation)
                        )

                self.current_lyric_index = self.get_current_lyric_index(self.estimate_lyric_position_seconds())

                if tick % presence_every_ticks == 0:
                    await self.update_presence(self.cached_media_state)

                await self.update_panel_if_needed(force=False)
            except Exception:
                logger.exception("背景更新迴圈發生錯誤")

            tick += 1
            await asyncio.sleep(MEDIA_REFRESH_SECONDS)

    async def update_presence(self, state: MediaState):
        if state.has_track:
            name = f"{state.title}" if not state.artist else f"{state.title} - {state.artist}"
            name = name[:128]
            if self.last_presence_name != name:
                await self.change_presence(
                    activity=discord.Activity(type=discord.ActivityType.listening, name=name),
                    status=discord.Status.online,
                )
                self.last_presence_name = name
            return

        fallback = "音樂狀態待命中"
        if self.last_presence_name != fallback:
            await self.change_presence(
                activity=discord.Game(name=fallback),
                status=discord.Status.online,
            )
            self.last_presence_name = fallback

    def get_panel_voice_client(self) -> Optional[discord.VoiceClient]:
        if self.panel_guild_id is None:
            return None

        guild = self.get_guild(self.panel_guild_id)
        if guild is None:
            return None

        return guild.voice_client

    def make_panel_signature(self, state: MediaState) -> str:
        vc = self.get_panel_voice_client()
        guild_name = vc.guild.name if vc and vc.guild else ""
        channel_name = vc.channel.name if vc and vc.channel else ""
        return "|".join(
            [
                state.title or "",
                state.artist or "",
                state.app_id or "",
                state.playback_status or "",
                str(int(self.estimate_position_seconds() or 0)),
                str(int(state.duration_seconds or 0)),
                str(self.current_lyric_index),
                self.lyric_status_text,
                guild_name,
                channel_name,
                str(self.lyrics_display_enabled),
                str(self.lyrics_auto_select_enabled),
                self.lyrics_mode,
                str(bool(self.plain_lyrics_text)),
                str(bool(self.cover_candidates)),
            ]
        )

    def build_track_embed(self, state: MediaState) -> discord.Embed:
        vc = self.get_panel_voice_client()
        position = self.estimate_position_seconds()
        embed = discord.Embed(color=discord.Color.blurple())

        if state.has_track:
            display_artist = (state.artist or "").strip() or "未知歌手"
            embed.description = f"**{state.title or '未知標題'}**\n{display_artist}"
            progress = build_progress_bar(position, state.duration_seconds)
            progress_text = (
                f"`{progress}`\n"
                f"{format_seconds(position)} / {format_seconds(state.duration_seconds)}"
            )
            embed.add_field(name="狀態", value=state.playback_label, inline=True)
            embed.add_field(name="來源", value=(state.app_id or "未知"), inline=True)
            embed.add_field(name="進度", value=progress_text, inline=False)
        else:
            embed.description = "目前沒有可顯示的歌曲資訊。"

        if vc and vc.guild and vc.channel:
            embed.add_field(name="語音頻道", value=f"{vc.guild.name} / {vc.channel.name}", inline=False)
        else:
            embed.add_field(name="語音頻道", value="尚未加入", inline=False)

        return embed

    def build_lyric_embed(self) -> Optional[discord.Embed]:
        if not self.lyrics_display_enabled:
            return None

        embed = discord.Embed(color=discord.Color.dark_blue())

        if not self.cached_media_state.has_track:
            embed.description = "目前沒有歌曲，無法顯示歌詞。"
            return embed

        has_synced = bool(self.lyric_lines)
        has_plain = bool(self.plain_lyrics_text)
        mode = self.lyrics_mode

        # 根據模式決定要顯示哪種歌詞
        show_synced = (mode == "auto" and has_synced) or (mode == "synced" and has_synced)
        show_plain = (mode == "auto" and not has_synced and has_plain) or (mode == "plain" and has_plain)

        if show_synced:
            current = self.current_lyric_index
            if current < 0:
                next_line = self.lyric_lines[0].text if self.lyric_lines else ""
                embed.description = f"等待歌詞開始...\n\n{next_line}"
                return embed

            previous_line = self.lyric_lines[current - 1].text if current - 1 >= 0 else ""
            current_line = self.lyric_lines[current].text
            next_line = self.lyric_lines[current + 1].text if current + 1 < len(self.lyric_lines) else ""

            chunks = []
            if previous_line:
                chunks.append(previous_line)
            chunks.append(f"**{current_line}**")
            if next_line:
                chunks.append(next_line)
            embed.description = "\n".join(chunks)
        elif show_plain:
            text = self.plain_lyrics_text or ""
            if len(text) > 4000:
                text = text[:4000] + "\n..."
            embed.description = text
            embed.set_footer(text="完整歌詞（無同步時間軸）")
        else:
            embed.description = self.lyric_status_text

        return embed

    def build_panel_embeds(self, state: MediaState) -> list[discord.Embed]:
        embeds = [self.build_track_embed(state)]
        lyric_embed = self.build_lyric_embed()
        if lyric_embed is not None:
            embeds.append(lyric_embed)
        return embeds

    async def clear_panel(self, *, delete_message: bool):
        message = self.panel_message
        self.panel_message = None
        self.panel_guild_id = None
        self.last_panel_signature = None
        self.last_panel_update_monotonic = 0.0
        self.delete_panel_state()

        if delete_message and message is not None:
            try:
                await message.delete()
            except discord.NotFound:
                pass
            except Exception:
                logger.exception("刪除面板訊息失敗")

    async def update_panel_if_needed(self, force: bool = False):
        if self.panel_message is None:
            return

        async with self.panel_lock:
            if self.panel_message is None:
                return

            signature = self.make_panel_signature(self.cached_media_state)
            if not force and signature == self.last_panel_signature:
                return

            now = time.monotonic()
            if (
                not force
                and PANEL_UPDATE_INTERVAL_SECONDS > 0
                and self.last_panel_update_monotonic > 0
                and (now - self.last_panel_update_monotonic) < PANEL_UPDATE_INTERVAL_SECONDS
            ):
                return

            try:
                await self.panel_message.edit(
                    embeds=self.build_panel_embeds(self.cached_media_state),
                    view=self.get_panel_view(),
                )
                self.last_panel_signature = signature
                self.last_panel_update_monotonic = now
            except discord.NotFound:
                logger.warning("面板訊息不存在，已停止自動更新")
                await self.clear_panel(delete_message=False)
            except Exception:
                logger.exception("更新面板失敗")

    async def start_panel(self, channel: discord.abc.Messageable, guild: discord.Guild) -> discord.Message:
        async with self.panel_lock:
            logger.info("start_panel：開始建立面板")
            await self.clear_panel(delete_message=True)
            self.panel_guild_id = guild.id

            logger.info("start_panel：正在取得媒體狀態")
            self.cached_media_state = await self.get_media_state_safely(
                timeout=MEDIA_STATE_TIMEOUT_SECONDS, fallback=self.cached_media_state
            )

            self.last_media_snapshot_monotonic = time.monotonic()
            self.last_media_position_seconds = self.cached_media_state.position_seconds or 0.0
            self.current_lyric_index = self.get_current_lyric_index(self.estimate_lyric_position_seconds())

            logger.info("start_panel：正在送出面板訊息")
            message = await channel.send(
                embeds=self.build_panel_embeds(self.cached_media_state),
                view=self.get_panel_view(),
            )
            self.panel_message = message
            self.last_panel_signature = self.make_panel_signature(self.cached_media_state)
            self.last_panel_update_monotonic = time.monotonic()
            self.save_panel_state()
            logger.info("start_panel：面板建立完成")
            return message


class CoverCandidateView(discord.ui.View):
    """用於選擇原版歌詞候選的 ephemeral Select"""

    _NONE_VALUE = "__none__"

    def __init__(
        self,
        bot: "MusicStatusBot",
        candidates: list[LyricCandidate],
        track_key: Optional[str] = None,
    ):
        super().__init__(timeout=120)
        self._bot = bot
        # 綁定建立此選單當下的曲目；即使選擇時已換歌，記憶仍寫到正確曲目。
        self._track_key = track_key if track_key is not None else bot.last_track_key
        # Discord Select 最多 25 個選項；保留 1 個給「清除歌詞」。
        self._candidates = candidates[:24]

        best_idx = self._find_best_idx(self._candidates)

        options = [
            discord.SelectOption(
                label=c.label,
                value=str(i),
                default=(i == best_idx),
                description=c.description,
            )
            for i, c in enumerate(self._candidates)
        ]
        options.append(discord.SelectOption(
            label="無（清除歌詞）",
            value=self._NONE_VALUE,
            emoji="🚫",
            description="不套用任何歌詞",
        ))

        select = discord.ui.Select(
            placeholder="選擇要套用的歌詞來源（已預選信心最高的選項）",
            options=options,
            custom_id="cover_candidate_select",
        )
        select.callback = self._on_select
        self.add_item(select)

    @staticmethod
    def _find_best_idx(candidates: list[LyricCandidate]) -> int:
        if not candidates:
            return 0
        best_idx = 0
        best_score = float("-inf")
        for i, c in enumerate(candidates):
            if c.score > best_score:
                best_score = c.score
                best_idx = i
        return best_idx

    async def _on_select(self, interaction: discord.Interaction):
        if not await ensure_command_allowed(interaction):
            return

        value = interaction.data["values"][0]
        bot = self._bot
        track_key = self._track_key
        # 選擇當下是否仍是同一首歌；若已換歌，只寫記憶、不動現在的面板顯示。
        is_current = bool(track_key) and track_key == bot.last_track_key

        if value == self._NONE_VALUE:
            status_text = "使用者已選擇不顯示歌詞。"
            if track_key:
                bot.store_cached_lyrics(track_key, [], status_text, None, self._candidates)
                bot.save_lyrics_override_none(track_key, status_text)
            if is_current:
                # 清除歌詞但保留候選清單，讓使用者還能改選
                bot.lyric_lines = []
                bot.plain_lyrics_text = None
                bot.lyric_status_text = status_text
                bot.current_lyric_index = -1
                await bot.update_panel_if_needed(force=True)
            await interaction.response.edit_message(content="🚫 已清除歌詞，仍可從選單更換來源。", view=None)
            return

        idx = int(value)
        c = self._candidates[idx]
        status_text = (
            f"已套用同步歌詞：{c.track_name} — {c.artist_name}"
            if c.synced_lines
            else f"已套用完整歌詞：{c.track_name} — {c.artist_name}"
        )
        if track_key:
            bot.store_cached_lyrics(track_key, c.synced_lines, status_text, c.plain_text, self._candidates)
            bot.save_lyrics_override_for_candidate(track_key, c, status_text)
        if is_current:
            bot.lyric_lines = c.synced_lines
            bot.plain_lyrics_text = c.plain_text
            # 套用後仍保留候選清單，讓使用者可以再改選
            bot.lyric_status_text = status_text
            bot.current_lyric_index = bot.get_current_lyric_index(bot.estimate_lyric_position_seconds())
            await bot.update_panel_if_needed(force=True)
        await interaction.response.edit_message(
            content=f"✅ 已套用：**{c.track_name}** — {c.artist_name}", view=None
        )


class LyricsSearchModal(discord.ui.Modal, title="手動搜尋歌詞"):
    song_name = discord.ui.TextInput(
        label="歌名",
        required=True,
        max_length=100,
    )
    artist_name = discord.ui.TextInput(
        label="歌手 / 樂團（可留空）",
        required=False,
        max_length=100,
    )

    def __init__(self, bot: "MusicStatusBot"):
        super().__init__()
        self._bot = bot
        state = bot.cached_media_state
        if state.title:
            self.song_name.default = clamp_text_input_default(
                normalize_search_title(state.title), 100
            )
        if state.artist:
            self.artist_name.default = clamp_text_input_default(
                normalize_search_artist(state.artist, state.title or ""), 100
            )

    async def on_submit(self, interaction: discord.Interaction):
        if not await ensure_command_allowed(interaction):
            return

        raw_title = self.song_name.value.strip()
        raw_artist = self.artist_name.value.strip()

        title = normalize_search_title(raw_title)
        artist = normalize_search_artist(raw_artist, title)

        bot = self._bot
        # 記下送出搜尋當下的曲目，回來時若已換歌就不要把結果寫進現在的共用狀態。
        target_track_key = bot.last_track_key

        await interaction.response.send_message(
            content=f"🔍 正在搜尋「{title}」{f'— {artist}' if artist else ''}...",
            ephemeral=True,
        )

        try:
            loop = asyncio.get_running_loop()
            # 與背景查詢共用同一個單一執行緒 executor，序列化 LRCLIB 請求。
            lines, status_text, plain_text, candidates = await loop.run_in_executor(
                bot.lyric_executor,
                fetch_synced_lyrics_for_track,
                title,
                artist,
                bot.cached_media_state.duration_seconds,
                False,  # 手動搜尋固定用手動模式，讓使用者自己選
            )
        except Exception:
            logger.exception("手動搜尋歌詞失敗")
            await interaction.edit_original_response(content="搜尋時發生錯誤，請稍後再試。")
            return

        if not candidates:
            await interaction.edit_original_response(content=f"❌ {status_text}")
            return

        track_changed = bot.last_track_key != target_track_key
        if not track_changed and target_track_key:
            bot.cover_candidates = candidates
            bot.store_cached_lyrics(
                target_track_key,
                [],
                "找到可能的歌詞，請點選「選擇歌詞來源」。",
                None,
                candidates,
            )
            await bot.update_panel_if_needed(force=True)

        note = (
            "\n⚠️ 目前播放的歌曲已經換了，這次選擇只會記到你剛搜尋的那首。"
            if track_changed
            else ""
        )
        await interaction.edit_original_response(
            content="請選擇要套用的歌詞來源：" + note,
            view=CoverCandidateView(bot, candidates, track_key=target_track_key),
        )


class PanelView(discord.ui.View):
    """面板互動元件：歌詞開關、模式選擇（含原版查詢設定）"""

    _SELECT_LYRICS = "__select_lyrics__"
    _TOGGLE_AUTO = "__toggle_auto__"
    _MANUAL_SEARCH = "__manual_search__"

    def __init__(self, bot: "MusicStatusBot"):
        super().__init__(timeout=None)
        self._bot = bot
        self._build()

    def refresh(self):
        self._build()

    def _build(self):
        self.clear_items()
        lyrics_on = self._bot.lyrics_display_enabled

        # ── Row 0：歌詞模式 Select（只在歌詞開啟時顯示）──
        if lyrics_on:
            mode = self._bot.lyrics_mode
            auto_on = self._bot.lyrics_auto_select_enabled
            has_candidates = bool(self._bot.cover_candidates)

            options = [
                discord.SelectOption(
                    label="自動",
                    value="auto",
                    description="有同步歌詞就滾動，沒有就顯示完整歌詞",
                    default=mode == "auto",
                    emoji="✨",
                ),
                discord.SelectOption(
                    label="動態歌詞",
                    value="synced",
                    description="只顯示同步滾動歌詞",
                    default=mode == "synced",
                    emoji="🎵",
                ),
                discord.SelectOption(
                    label="完整歌詞",
                    value="plain",
                    description="只顯示完整純文字歌詞",
                    default=mode == "plain",
                    emoji="📄",
                ),
                discord.SelectOption(
                    label="原版查詢：自動套用" if auto_on else "原版查詢：手動選擇",
                    value=self._TOGGLE_AUTO,
                    description="點此切換為手動選擇" if auto_on else "點此切換為自動套用",
                    emoji="🔍",
                ),
                discord.SelectOption(
                    label="手動搜尋歌詞",
                    value=self._MANUAL_SEARCH,
                    description="輸入歌名或歌手手動搜尋",
                    emoji="🔎",
                ),
            ]

            if has_candidates:
                has_lyrics = bool(self._bot.lyric_lines or self._bot.plain_lyrics_text)
                options.append(discord.SelectOption(
                    label="更換歌詞來源" if has_lyrics else "選擇歌詞來源",
                    value=self._SELECT_LYRICS,
                    description=f"從 {len(self._bot.cover_candidates)} 筆候選中選擇",
                    emoji="🎤",
                ))

            select = discord.ui.Select(
                placeholder="歌詞設定",
                options=options,
                custom_id="panel_lyrics_mode",
                row=0,
            )
            select.callback = self._on_mode_select
            self.add_item(select)

        # ── Row 1：歌詞開關 ──
        toggle_btn = discord.ui.Button(
            label="歌詞：開" if lyrics_on else "歌詞：關",
            style=discord.ButtonStyle.green if lyrics_on else discord.ButtonStyle.grey,
            custom_id="panel_toggle_lyrics",
            emoji="🎶",
            row=1,
        )
        toggle_btn.callback = self._on_toggle_lyrics
        self.add_item(toggle_btn)

    async def _on_mode_select(self, interaction: discord.Interaction):
        if not await ensure_command_allowed(interaction):
            return

        value = interaction.data["values"][0]
        bot = self._bot

        if value == self._MANUAL_SEARCH:
            await interaction.response.send_modal(LyricsSearchModal(bot))
            return

        if value == self._SELECT_LYRICS:
            candidates = bot.cover_candidates
            if not candidates:
                await interaction.response.send_message("目前沒有候選歌詞。", ephemeral=True)
                return
            await interaction.response.send_message(
                content="請選擇要套用的歌詞來源：",
                view=CoverCandidateView(bot, candidates),
                ephemeral=True,
            )
            return

        if value == self._TOGGLE_AUTO:
            bot.lyrics_auto_select_enabled = not bot.lyrics_auto_select_enabled
            await interaction.response.defer()
            if bot.last_track_key and bot.get_lyrics_override(bot.last_track_key) is not None:
                logger.info("目前曲目已有手動歌詞記憶，切換自動模式不會覆蓋此曲目的記憶")
            if bot.last_track_key:
                bot.lyric_cache.pop(bot.last_track_key, None)
                bot.lyric_status_cache.pop(bot.last_track_key, None)
                if bot.lyric_fetch_task:
                    bot.lyric_fetch_task.cancel()
                bot.lyric_request_generation += 1
                bot.reset_lyrics("正在查詢歌詞...")
                state_snapshot = MediaState(
                    title=bot.cached_media_state.title,
                    artist=bot.cached_media_state.artist,
                    app_id=bot.cached_media_state.app_id,
                    position_seconds=bot.cached_media_state.position_seconds,
                    duration_seconds=bot.cached_media_state.duration_seconds,
                    playback_status=bot.cached_media_state.playback_status,
                )
                bot.lyric_fetch_task = asyncio.create_task(
                    bot.load_lyrics_for_track(
                        bot.last_track_key, state_snapshot, bot.lyric_request_generation
                    )
                )
            await bot.update_panel_if_needed(force=True)
            return

        bot.lyrics_mode = value
        await interaction.response.defer()
        await bot.update_panel_if_needed(force=True)

    async def _on_toggle_lyrics(self, interaction: discord.Interaction):
        if not await ensure_command_allowed(interaction):
            return

        self._bot.lyrics_display_enabled = not self._bot.lyrics_display_enabled
        await interaction.response.defer()
        await self._bot.update_panel_if_needed(force=True)


bot = MusicStatusBot()


@bot.event
async def on_ready():
    logger.info("Bot 已啟動：%s", bot.user)
    logger.info("媒體輸入裝置：%s", MEDIA_INPUT_NAME)
    if GUILD_ID:
        logger.info("單一伺服器同步模式已啟用：%s", GUILD_ID)
    if HAS_MEDIA_CONTROL:
        logger.info("媒體資訊功能已啟用")
    else:
        logger.info("媒體資訊功能停用，請安裝必要套件")
    logger.info("同步歌詞功能已啟用（LRCLIB）")
    logger.info("歌詞提前顯示秒數：%s", LYRIC_ADVANCE_SECONDS)
    logger.info("歌詞時間偏移秒數：%s", LYRICS_TIME_OFFSET_SECONDS)
    logger.info("歌詞自動套用信心門檻：%s", LYRICS_AUTO_ACCEPT_SCORE)
    logger.info("歌詞手動記憶檔：%s", LYRICS_OVERRIDE_FILE)
    logger.info("面板最短更新間隔秒數：%s", PANEL_UPDATE_INTERVAL_SECONDS)
    logger.info("音訊取樣率：%s", AUDIO_SAMPLERATE)
    logger.info("音訊聲道數：%s", AUDIO_CHANNELS)
    logger.info("音訊區塊大小：%s", AUDIO_BLOCKSIZE)
    logger.info("音訊佇列大小：%s", AUDIO_QUEUE_SIZE)
    logger.info("音訊緩衝等待秒數：%s", AUDIO_BUFFER_TIMEOUT_SECONDS)
    logger.info("媒體刷新秒數：%s", MEDIA_REFRESH_SECONDS)
    logger.info("狀態列刷新秒數：%s", PRESENCE_REFRESH_SECONDS)
    logger.info("媒體資訊 timeout 秒數：%s", MEDIA_STATE_TIMEOUT_SECONDS)
    logger.info("媒體資訊重試次數：%s", MEDIA_STATE_RETRY_COUNT)
    logger.info("媒體資訊重試等待秒數：%s", MEDIA_STATE_RETRY_DELAY_SECONDS)
    logger.info("歌詞快取上限：%s", MAX_LYRIC_CACHE)


@bot.event
async def on_voice_state_update(member, before, after):
    if bot.user is None or member.id != bot.user.id:
        return

    if before.channel != after.channel:
        logger.info("Bot 語音狀態變更：%s -> %s", before.channel, after.channel)
        await bot.update_panel_if_needed(force=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logger.exception("斜線指令錯誤: %s", error)
    await send_ephemeral(interaction, "指令執行失敗，請稍後再試，或查看主控台記錄。")


@bot.tree.command(name="join", description="加入你所在的語音頻道")
@app_commands.guild_only()
async def join(interaction: discord.Interaction):
    if not await ensure_command_allowed(interaction):
        return

    user_voice = getattr(interaction.user, "voice", None)
    if user_voice is None or user_voice.channel is None:
        await send_ephemeral(interaction, "你需要先加入一個語音頻道。")
        return

    await interaction.response.defer(thinking=True)

    async with bot.voice_lock:
        current_voice_clients = [vc for vc in bot.voice_clients if vc and vc.is_connected()]
        if current_voice_clients:
            connected_vc = current_voice_clients[0]
            if connected_vc.guild == interaction.guild:
                await interaction.followup.send("Bot 已經在語音頻道裡了，請先使用 /leave。", ephemeral=True)
            else:
                await interaction.followup.send(
                    f"Bot 已經在指定伺服器的語音頻道中：{connected_vc.guild.name} / {connected_vc.channel.name if connected_vc.channel else '未知頻道'}",
                    ephemeral=True,
                )
            return

        try:
            device_index = get_input_device_index(MEDIA_INPUT_NAME)
        except Exception:
            logger.exception("查詢音訊裝置失敗")
            await interaction.followup.send(
                f"查詢音訊裝置時發生錯誤，請確認 PortAudio 是否正常運作。\n裝置名稱：{MEDIA_INPUT_NAME}",
                ephemeral=True,
            )
            return

        if device_index is None:
            await interaction.followup.send(
                f"找不到輸入裝置：{MEDIA_INPUT_NAME}\n請檢查 .env 內的 MEDIA_INPUT_NAME 是否正確。",
                ephemeral=True,
            )
            return

        channel = user_voice.channel
        vc = None
        source = None

        try:
            vc = await channel.connect()
            source = MediaInputSource(device_index=device_index)
            vc.play(source)
        except Exception:
            logger.exception("加入語音頻道失敗")

            if source is not None:
                try:
                    source.cleanup()
                except Exception:
                    logger.exception("回滾音源失敗")

            if vc is not None:
                try:
                    await cleanup_voice_client(vc)
                    if vc.is_connected():
                        await vc.disconnect(force=True)
                except Exception:
                    logger.exception("回滾語音連線失敗")

            await interaction.followup.send("加入語音頻道失敗，請查看主控台記錄。", ephemeral=True)
            return

        await interaction.followup.send(f"已加入 {channel.name}。")
        try:
            await bot.update_panel_if_needed(force=True)
        except Exception:
            logger.exception("加入後更新面板失敗（不影響語音連線）")


@bot.tree.command(name="leave", description="離開語音頻道")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    if not await ensure_command_allowed(interaction):
        return

    await interaction.response.defer(thinking=True)

    async with bot.voice_lock:
        vc = interaction.guild.voice_client
        if vc is None:
            await interaction.followup.send("Bot 不在任何語音頻道。", ephemeral=True)
            return

        try:
            await cleanup_voice_client(vc)
            await vc.disconnect(force=True)
        except Exception:
            logger.exception("離開語音頻道失敗")
            await interaction.followup.send("離開語音頻道失敗，請查看主控台記錄。", ephemeral=True)
            return

        await interaction.followup.send("已離開語音頻道。")
        try:
            await bot.update_panel_if_needed(force=True)
        except Exception:
            logger.exception("離開後更新面板失敗（不影響已離開狀態）")


@bot.tree.command(name="status", description="查看目前連線狀態")
@app_commands.guild_only()
async def status(interaction: discord.Interaction):
    if not await ensure_command_allowed(interaction):
        return

    vc = interaction.guild.voice_client
    if vc and vc.channel:
        text = f"目前已連線到：{vc.guild.name} / {vc.channel.name}"
    else:
        text = "目前未連線到任何語音頻道。"

    text += "\n狀態面板：已啟用" if bot.panel_message else "\n狀態面板：未啟用"
    await send_ephemeral(interaction, text)


@bot.tree.command(name="panel", description="建立可自動更新的狀態面板")
@app_commands.guild_only()
async def panel(interaction: discord.Interaction):
    if not await ensure_command_allowed(interaction):
        return
    if interaction.channel is None:
        await send_ephemeral(interaction, "這個指令只能在伺服器文字頻道中使用。")
        return

    await interaction.response.defer(thinking=True)
    try:
        message = await bot.start_panel(interaction.channel, interaction.guild)
        await interaction.followup.send(f"狀態面板已建立：{message.jump_url}", ephemeral=True)
    except Exception:
        logger.exception("建立面板失敗")
        await interaction.followup.send("建立面板失敗，請查看主控台記錄。", ephemeral=True)


@bot.tree.command(name="play", description="要求播放器開始播放")
@app_commands.guild_only()
async def play_cmd(interaction: discord.Interaction):
    if not await ensure_command_allowed(interaction):
        return

    await interaction.response.defer(ephemeral=True)
    _, msg = await control_media("play")
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="pause", description="要求播放器暫停")
@app_commands.guild_only()
async def pause_cmd(interaction: discord.Interaction):
    if not await ensure_command_allowed(interaction):
        return

    await interaction.response.defer(ephemeral=True)
    _, msg = await control_media("pause")
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="next", description="要求播放器切到下一首")
@app_commands.guild_only()
async def next_cmd(interaction: discord.Interaction):
    if not await ensure_command_allowed(interaction):
        return

    await interaction.response.defer(ephemeral=True)
    _, msg = await control_media("next")
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="prev", description="要求播放器切到上一首")
@app_commands.guild_only()
async def prev_cmd(interaction: discord.Interaction):
    if not await ensure_command_allowed(interaction):
        return

    await interaction.response.defer(ephemeral=True)
    _, msg = await control_media("prev")
    await interaction.followup.send(msg, ephemeral=True)


async def main():
    configure_logging()

    if not TOKEN:
        raise RuntimeError("找不到 DISCORD_TOKEN，請在 .env 檔案中設定。")

    try:
        logger.info("準備啟動 bot.start()")
        await bot.start(TOKEN)
    finally:
        logger.info("開始執行 bot.close()")
        await bot.close()
        await asyncio.sleep(0.8)
        logger.info("程式結束")


if __name__ == "__main__":
    asyncio.run(main())
