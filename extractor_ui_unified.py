"""
Unified timestamped video frame extractor.

Two extraction methods, selectable from the UI:

    * PTS grid  (source-frame accurate)
        - FFprobe packet PTS is authoritative
        - fixed sampling grid + nearest source frame
        - balanced BST select expression inside FFmpeg
        - logs: extraction_benchmark.log, ffmpeg_last_run.log

    * FPS grid  (regular output sampling)
        - FFmpeg's fps filter builds the output timeline
        - per-frame timestamp = recording_start + index/fps
        - log: fps_extraction_log.log

Both modes write filename, EXIF, and (optionally) Windows
filesystem timestamps from the SAME calculated frame_time.
They also generate a manifest (CSV + JSON) containing metadata
for each extracted frame, which is useful for downstream SFM pipelines.

ffmpeg-python is no longer required: every FFmpeg/FFprobe call goes
through subprocess using the executable that was validated at startup.

FFmpeg availability is checked at startup. If ffmpeg / ffprobe
are not found on PATH, the user can point the application at
the folder that contains them; the choice is persisted under
%APPDATA%\\TimestampedVideoFrameExtractor\\settings.json.
"""

import os
import re
import sys
import ctypes
import subprocess
import time
import json
import math
import shutil
import tempfile
import uuid
import hashlib
import csv
import tkinter as tk

from tkinter import filedialog, messagebox, ttk
from datetime import datetime, timedelta, timezone

import piexif


# ============================================================
# PyInstaller --windowed fix
# ============================================================

def _patch_subprocess_for_frozen_windows():
    if sys.platform != "win32":
        return
    if not getattr(sys, "frozen", False):
        return

    CREATE_NO_WINDOW = 0x08000000
    _original_popen_init = subprocess.Popen.__init__

    def _patched_popen_init(self, *args, **kwargs):
        if kwargs.get("creationflags", 0) == 0:
            kwargs["creationflags"] = CREATE_NO_WINDOW
        return _original_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _patched_popen_init


_patch_subprocess_for_frozen_windows()


# ============================================================
# Configuration
# ============================================================

DEFAULT_FPS = 2.0
DEFAULT_METHOD = "pts"

PTS_BENCHMARK_LOG_NAME = "extraction_benchmark.log"
PTS_FFMPEG_LOG_NAME = "ffmpeg_last_run.log"
FPS_LOG_NAME = "fps_extraction_log.log"

MANIFEST_CSV_NAME = "frame_manifest.csv"
MANIFEST_JSON_NAME = "frame_manifest.json"
RUN_METADATA_NAME = "extraction_run_metadata.json"

SET_WINDOWS_FILE_TIMESTAMPS = False
SET_WINDOWS_CREATED_TIME = True
SET_WINDOWS_MODIFIED_TIME = True
SET_WINDOWS_ACCESSED_TIME = False

JPEG_QUALITY = 2
FILTER_SCRIPT_THRESHOLD = 20000

MAX_EXTRACTION_FPS = 1000.0
PTS_VERIFY_TOLERANCE_S = 0.0001
OUTPUT_FRAME_PATTERN = re.compile(
    r"^\d{8}_\d{6}_\d{3}_\d{6,}\.jpg$", re.IGNORECASE)

SETTINGS_APP_NAME = "TimestampedVideoFrameExtractor"
SETTINGS_FILENAME = "settings.json"


# ============================================================
# Settings persistence
# ============================================================

def get_settings_path():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        folder = os.path.join(base, SETTINGS_APP_NAME)
    else:
        folder = os.path.join(
            os.path.expanduser("~"),
            ".config",
            "timestamped-video-frame-extractor",
        )
    return os.path.join(folder, SETTINGS_FILENAME)


def load_settings():
    path = get_settings_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def save_settings(settings):
    path = get_settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except OSError:
        pass


# ============================================================
# Input validation
# ============================================================

def validate_fps(value):
    """Return a finite float in (0, MAX_EXTRACTION_FPS] or raise ValueError.

    float() happily accepts 'inf' and 'nan', and both pass a plain
    `fps <= 0` test; they made select_source_frames() loop forever.
    """
    try:
        fps = float(value)
    except (TypeError, ValueError):
        raise ValueError("FPS must be a number.")
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("FPS must be a finite number greater than zero.")
    if fps > MAX_EXTRACTION_FPS:
        raise ValueError(f"FPS must not exceed {MAX_EXTRACTION_FPS:g}.")
    return fps


# ============================================================
# FFmpeg detection & PATH management
# ============================================================

_ORIGINAL_PATH = os.environ.get("PATH", "")

# Executables used for EVERY subprocess call. configure_ffmpeg() sets
# them from the detection result, so the binary that was validated is
# the binary that runs (no second, independent PATH lookup).
_FFMPEG_EXE = "ffmpeg"
_FFPROBE_EXE = "ffprobe"
_VFR_ARGS = ["-fps_mode", "vfr"]


def set_custom_ffmpeg_folder(folder):
    if folder:
        os.environ["PATH"] = folder + os.pathsep + _ORIGINAL_PATH
    else:
        os.environ["PATH"] = _ORIGINAL_PATH


def _resolve_exe(name, folder):
    if folder is None:
        return name
    suffix = ".exe" if sys.platform == "win32" else ""
    if suffix:
        candidate = os.path.join(folder, name + suffix)
        if os.path.isfile(candidate):
            return candidate
    candidate = os.path.join(folder, name)
    if os.path.isfile(candidate):
        return candidate
    return os.path.join(folder, name + suffix)


def _decode_bytes(data):
    return data.decode("utf-8", errors="replace") if data else ""


def _run_version_command(exe):
    try:
        result = subprocess.run(
            [exe, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except FileNotFoundError:
        return None, "not found"
    except subprocess.TimeoutExpired:
        return None, "timed out"
    except Exception as exc:
        return None, str(exc)

    if result.returncode != 0:
        return None, f"exit code {result.returncode}"

    output = result.stdout or ""
    first_line = output.splitlines()[0] if output else ""
    match = re.search(r"version\s+(\S+)", first_line)
    version = match.group(1) if match else (first_line.strip() or "unknown")
    return version, None


def _supports_fps_mode(ffmpeg_exe):
    """True / False / None (could not be determined).

    -fps_mode is needed by PTS mode. Probing the capability is more
    reliable than parsing version strings (git builds, vendor suffixes).
    """
    command = [
        ffmpeg_exe, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "nullsrc=s=16x16:r=1:d=1",
        "-frames:v", "1", "-fps_mode", "vfr", "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20)
    except Exception:
        return None
    if result.returncode == 0:
        return True
    error_text = _decode_bytes(result.stderr).lower()
    if "unrecognized option" in error_text and "fps_mode" in error_text:
        return False
    return None


def detect_ffmpeg(custom_folder=None):
    if custom_folder:
        ffmpeg_exe = _resolve_exe("ffmpeg", custom_folder)
        ffprobe_exe = _resolve_exe("ffprobe", custom_folder)
        source = "custom"
    else:
        ffmpeg_exe = shutil.which("ffmpeg") or "ffmpeg"
        ffprobe_exe = shutil.which("ffprobe") or "ffprobe"
        source = "path"

    ffmpeg_version, ffmpeg_error = _run_version_command(ffmpeg_exe)
    ffprobe_version, ffprobe_error = _run_version_command(ffprobe_exe)

    fps_mode_supported = None
    if ffmpeg_version is not None:
        fps_mode_supported = _supports_fps_mode(ffmpeg_exe)

    return {
        "ok": ffmpeg_version is not None and ffprobe_version is not None,
        "source": source,
        "custom_folder": custom_folder,
        "ffmpeg_exe": ffmpeg_exe,
        "ffmpeg_version": ffmpeg_version,
        "ffmpeg_error": ffmpeg_error,
        "ffprobe_exe": ffprobe_exe,
        "ffprobe_version": ffprobe_version,
        "ffprobe_error": ffprobe_error,
        "fps_mode_supported": fps_mode_supported,
    }


def configure_ffmpeg(info):
    """Make every later subprocess call use the validated executables."""
    global _FFMPEG_EXE, _FFPROBE_EXE, _VFR_ARGS
    if not info or not info.get("ok"):
        return
    _FFMPEG_EXE = info["ffmpeg_exe"]
    _FFPROBE_EXE = info["ffprobe_exe"]
    if info.get("fps_mode_supported") is False:
        _VFR_ARGS = ["-vsync", "vfr"]      # FFmpeg < 5.1
    else:
        _VFR_ARGS = ["-fps_mode", "vfr"]


def run_ffprobe_json(video_path, extra_args, what):
    command = ([_FFPROBE_EXE, "-v", "error"] + list(extra_args)
               + ["-of", "json", video_path])
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError(
            f"FFprobe executable was not found: {_FFPROBE_EXE}")
    if result.returncode != 0:
        raise RuntimeError(
            f"FFprobe failed while reading {what}:\n\n"
            + _decode_bytes(result.stderr))
    try:
        return json.loads(_decode_bytes(result.stdout))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Failed to parse FFprobe JSON output ({what}):\n\n{exc}")


# ============================================================
# Windows FILETIME support (shared)
# ============================================================

def datetime_to_filetime(dt):
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware.")
    dt_utc = dt.astimezone(timezone.utc)
    epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
    return int((dt_utc - epoch).total_seconds() * 10_000_000)


def set_windows_file_timestamps(filepath,
                                created_time=None,
                                modified_time=None,
                                accessed_time=None):
    if sys.platform != "win32":
        return
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    kernel32 = ctypes.windll.kernel32
    from ctypes import wintypes
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.SetFileTime.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
    ]
    kernel32.SetFileTime.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateFileW(
        filepath,
        GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError()
    try:
        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                        ("dwHighDateTime", ctypes.c_uint32)]

        def make_filetime(dt):
            if dt is None:
                return None
            v = datetime_to_filetime(dt)
            return FILETIME(v & 0xFFFFFFFF, (v >> 32) & 0xFFFFFFFF)

        creation = make_filetime(created_time)
        access = make_filetime(accessed_time)
        write = make_filetime(modified_time)

        ok = kernel32.SetFileTime(
            handle,
            ctypes.byref(creation) if creation is not None else None,
            ctypes.byref(access) if access is not None else None,
            ctypes.byref(write) if write is not None else None,
        )
        if not ok:
            raise ctypes.WinError()
    finally:
        kernel32.CloseHandle(handle)


# ============================================================
# Video datetime parsing (shared)
# ============================================================

def parse_video_datetime(value, meta=None):
    """Parse a metadata datetime string.

    If the string carries no timezone, the machine's local timezone is
    assumed. When `meta` (a dict) is given, meta["tz_assumed"] is set so
    the caller can report that assumption instead of hiding it.
    """
    if not value:
        return None

    def localize(dt):
        if dt.tzinfo is None:
            if meta is not None:
                meta["tz_assumed"] = True
            dt = dt.astimezone()
        return dt

    value = str(value).strip()
    if (len(value) >= 19 and value[4] == ":" and value[7] == ":"):
        value = value[:4] + "-" + value[5:7] + "-" + value[8:]
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return localize(datetime.fromisoformat(value))
    except ValueError:
        pass
    formats = [
        "%Y:%m:%d %H:%M:%S%z",
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
    ]
    for fmt in formats:
        try:
            return localize(datetime.strptime(value, fmt))
        except ValueError:
            continue
    return None


def find_tag(tags, *names):
    if not tags:
        return None
    normalized = {str(k).lower().strip(): v for k, v in tags.items()}
    for name in names:
        v = normalized.get(str(name).lower().strip())
        if v not in (None, "", "0000:00:00 00:00:00"):
            return v
    return None


def find_tag_named(tags, *names):
    """Like find_tag(), but also returns which tag name matched."""
    if not tags:
        return None, None
    normalized = {str(k).lower().strip(): v for k, v in tags.items()}
    for name in names:
        v = normalized.get(str(name).lower().strip())
        if v not in (None, "", "0000:00:00 00:00:00"):
            return name, v
    return None, None


# ============================================================
# Original video recording start (shared)
# ============================================================

_PRIMARY_START_TAGS = (
    "com.apple.quicktime.creationdate",
    "Creation Date",
    "creation date",
    "creationdate",
)
_FALLBACK_START_TAGS = ("creation_time", "date", "datetime")


def find_recording_start(probe):
    """Return a dict describing the recording start and where it came from."""
    format_tags = probe.get("format", {}).get("tags", {})
    stream_tags = {}
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video":
            stream_tags.update(stream.get("tags", {}))

    all_tags = {}
    all_tags.update(format_tags)
    all_tags.update(stream_tags)

    for names, is_fallback in ((_PRIMARY_START_TAGS, False),
                               (_FALLBACK_START_TAGS, True)):
        tag_name, value = find_tag_named(all_tags, *names)
        if not value:
            continue
        meta = {}
        parsed = parse_video_datetime(value, meta)
        if parsed is None:
            continue
        return {
            "datetime": parsed,
            "tag": tag_name,
            "raw": str(value),
            "is_fallback": is_fallback,
            "tz_assumed": bool(meta.get("tz_assumed")),
            "whole_seconds": "." not in str(value),
        }

    raise RuntimeError(
        "Could not determine the original video recording start "
        "time from metadata.\n\nThe program will not substitute "
        "the Windows file creation time."
    )


def get_video_creation_time(video_path):
    probe = run_ffprobe_json(
        video_path, ["-show_format", "-show_streams"], "video metadata")
    return find_recording_start(probe)["datetime"]


def describe_recording_start(info):
    """Human-readable lines about the timestamp base, incl. warnings."""
    rs = info.get("recording_start_info") or {}
    kind = "fallback tag" if rs.get("is_fallback") else "primary tag"
    lines = [
        f"Recording start source: {rs.get('tag')} ({kind}), "
        f"raw value: {rs.get('raw')}"
    ]
    if rs.get("is_fallback"):
        lines.append(
            "  WARNING: fallback tags are usually UTC and, on non-Apple "
            "devices, may mark the end of recording or the muxing time "
            "rather than the start.")
    if rs.get("tz_assumed"):
        lines.append(
            "  WARNING: the tag has no timezone; this computer's current "
            "timezone was assumed.")
    if rs.get("whole_seconds"):
        lines.append(
            "  NOTE: the tag has whole-second precision; the millisecond "
            "digits in filenames are relative to it, not absolute.")
    return lines


def exif_offset_tags_available():
    try:
        return all(hasattr(piexif.ExifIFD, name)
                   for name in ("OffsetTimeOriginal", "OffsetTimeDigitized"))
    except Exception:
        return False


# ============================================================
# Video information (shared)
# ============================================================

def get_video_info(video_path):
    probe = run_ffprobe_json(
        video_path, ["-show_format", "-show_streams"], "video metadata")

    # First real video stream. Attached pictures (cover art) are skipped;
    # the ordinal among video streams is what -select_streams v:N and
    # -map 0:v:N expect, and is used consistently by both methods.
    video_stream = None
    video_ordinal = 0
    for stream in probe.get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        if (stream.get("disposition") or {}).get("attached_pic") == 1:
            video_ordinal += 1
            continue
        video_stream = stream
        break
    if video_stream is None:
        raise RuntimeError("No video stream was found.")

    duration = None
    if video_stream.get("duration"):
        try:
            duration = float(video_stream["duration"])
        except (ValueError, TypeError):
            duration = None
    if duration is None:
        format_duration = probe.get("format", {}).get("duration")
        if format_duration:
            try:
                duration = float(format_duration)
            except (ValueError, TypeError):
                duration = None
    if duration is None:
        raise RuntimeError("Could not determine video duration.")

    source_fps = None
    rate_text = video_stream.get("r_frame_rate")
    if rate_text and "/" in rate_text:
        num, den = rate_text.split("/", 1)
        try:
            num, den = float(num), float(den)
            if den != 0:
                source_fps = num / den
        except (ValueError, TypeError):
            pass

    average_fps = None
    avg_rate_text = video_stream.get("avg_frame_rate")
    if avg_rate_text and "/" in avg_rate_text:
        num, den = avg_rate_text.split("/", 1)
        try:
            num, den = float(num), float(den)
            if den != 0:
                average_fps = num / den
        except (ValueError, TypeError):
            pass

    average_fps = None
    avg_rate_text = video_stream.get("avg_frame_rate")
    if avg_rate_text and "/" in avg_rate_text:
        num, den = avg_rate_text.split("/", 1)
        try:
            num, den = float(num), float(den)
            if den != 0:
                average_fps = num / den
        except (ValueError, TypeError):
            pass

    time_base = None
    tb_text = video_stream.get("time_base")
    if tb_text and "/" in tb_text:
        tb_num, tb_den = tb_text.split("/", 1)
        try:
            tb_num, tb_den = float(tb_num), float(tb_den)
            if tb_den != 0 and tb_num > 0:
                time_base = tb_num / tb_den
        except (ValueError, TypeError):
            pass

    width = video_stream.get("width")
    height = video_stream.get("height")
    codec = video_stream.get("codec_name")

    start_time = video_stream.get("start_time")
    try:
        start_time = float(start_time) if start_time is not None else 0.0
    except (ValueError, TypeError):
        start_time = 0.0

    format_start = probe.get("format", {}).get("start_time")
    try:
        format_start_time = (
            float(format_start) if format_start is not None else 0.0
        )
    except (ValueError, TypeError):
        format_start_time = 0.0

    recording_start_info = find_recording_start(probe)

    return {
        "duration": duration,
        "source_fps": source_fps,
        "avg_frame_rate": average_fps,
        "width": width,
        "height": height,
        "codec": codec,
        "start_time": start_time,
        "format_start_time": format_start_time,
        "recording_start": recording_start_info["datetime"],
        "recording_start_info": recording_start_info,
        "video_ordinal": video_ordinal,
        "time_base": time_base,
    }


# ============================================================
# Shared: filename / EXIF / Windows batch / temp cleanup
# ============================================================

def make_frame_filename(frame_time, frame_number):
    timestamp = frame_time.strftime("%Y%m%d_%H%M%S_%f")[:-3]
    return f"{timestamp}_{frame_number:06d}.jpg"


def write_frame_exif(filepath, frame_time, video_path, method="pts"):
    date_time = frame_time.strftime("%Y:%m:%d %H:%M:%S")
    milliseconds = frame_time.microsecond // 1000
    subsec = f"{milliseconds:03d}"
    tz = frame_time.strftime("%z")
    if tz:
        tz = tz[:3] + ":" + tz[3:]

    try:
        exif_dict = piexif.load(filepath)
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {},
                     "1st": {}, "thumbnail": None}
    exif_dict.setdefault("0th", {})
    exif_dict.setdefault("Exif", {})

    exif_dict["0th"][piexif.ImageIFD.DateTime] = date_time.encode("ascii")
    exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = \
        date_time.encode("ascii")
    exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = \
        date_time.encode("ascii")
    exif_dict["Exif"][piexif.ExifIFD.SubSecTimeOriginal] = \
        subsec.encode("ascii")
    exif_dict["Exif"][piexif.ExifIFD.SubSecTimeDigitized] = \
        subsec.encode("ascii")

    off_orig = getattr(piexif.ExifIFD, "OffsetTimeOriginal", None)
    if off_orig is not None and tz:
        exif_dict["Exif"][off_orig] = tz.encode("ascii")
    off_dig = getattr(piexif.ExifIFD, "OffsetTimeDigitized", None)
    if off_dig is not None and tz:
        exif_dict["Exif"][off_dig] = tz.encode("ascii")

    software = (
        b"Timestamped Video Frame Extractor (PTS grid)"
        if method == "pts"
        else b"Timestamped Video Frame Extractor (FPS grid)"
    )
    exif_dict["0th"][piexif.ImageIFD.Software] = software

    description = "Extracted from: " + os.path.basename(video_path)
    exif_dict["0th"][piexif.ImageIFD.ImageDescription] = \
        description.encode("utf-8", errors="replace")

    piexif.insert(piexif.dump(exif_dict), filepath)


def write_frame_manifest(output_folder, records):
    """Atomically replace CSV and JSON manifests after complete serialization."""
    if not records:
        return None, None

    csv_path = os.path.join(output_folder, MANIFEST_CSV_NAME)
    json_path = os.path.join(output_folder, MANIFEST_JSON_NAME)
    token = uuid.uuid4().hex
    csv_tmp = csv_path + "." + token + ".tmp"
    json_tmp = json_path + "." + token + ".tmp"
    fields = list(dict.fromkeys(
        key for record in records for key in record.keys()
    ))
    try:
        with open(csv_tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="raise")
            writer.writeheader()
            writer.writerows(records)
            f.flush()
            os.fsync(f.fileno())
        with open(json_tmp, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        # Each replacement is atomic; a filesystem cannot atomically replace
        # two independent files, so run metadata marks the committed result.
        os.replace(csv_tmp, csv_path)
        os.replace(json_tmp, json_path)
    finally:
        for path in (csv_tmp, json_tmp):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
    return csv_path, json_path


def to_utc_iso(timestamp):
    """Return an ISO-8601 UTC timestamp with an explicit +00:00 offset."""
    return timestamp.astimezone(timezone.utc).isoformat()


def sha256_file(filepath, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with open(filepath, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_run_metadata(output_folder, metadata):
    """Write run-level metadata atomically; status should be complete only at commit."""
    path = os.path.join(output_folder, RUN_METADATA_NAME)
    tmp = path + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, indent=2, ensure_ascii=False, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
    return path


def apply_windows_timestamps_batch(timestamp_records, progress_callback=None):
    if not SET_WINDOWS_FILE_TIMESTAMPS:
        return
    if sys.platform != "win32":
        return
    total = len(timestamp_records)
    for index, (filepath, frame_time) in enumerate(timestamp_records, start=1):
        set_windows_file_timestamps(
            filepath,
            created_time=frame_time if SET_WINDOWS_CREATED_TIME else None,
            modified_time=frame_time if SET_WINDOWS_MODIFIED_TIME else None,
            accessed_time=frame_time if SET_WINDOWS_ACCESSED_TIME else None,
        )
        if progress_callback:
            progress_callback(index, total, frame_time)


def cleanup_temp_folder(temp_folder):
    """Remove a temporary extraction directory; surface cleanup failures."""
    if not os.path.exists(temp_folder):
        return
    shutil.rmtree(temp_folder)


def calculate_expected_frame_count(duration, fps):
    if duration <= 0:
        return 0
    return int(duration * fps) + 1


# ============================================================
# PTS-mode helpers
# ============================================================

def _to_float_or_none(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None          # includes the literal string "N/A"
    return number if math.isfinite(number) else None


def get_source_frame_pts(video_path, stream_ordinal=0, stats=None):
    """Presentation timestamps of the frames the decoder will OUTPUT.

    Packets flagged 'D' (discard) are needed to keep decoder state but
    never produce an output frame (typical after stream-copy trimming:
    pre-roll frames with negative PTS). Counting them shifted every
    frame index used by the select filter.
    """
    probe = run_ffprobe_json(
        video_path,
        ["-select_streams", f"v:{stream_ordinal}",
         "-show_packets",
         "-show_entries", "packet=pts_time,dts_time,flags"],
        "source frame PTS",
    )

    packets = probe.get("packets", [])
    if not packets:
        raise RuntimeError("FFprobe returned no video packets.")

    source_pts = []
    discarded = 0
    unusable = 0
    for pkt in packets:
        if "D" in (pkt.get("flags") or ""):
            discarded += 1
            continue
        # DTS is decode time and must not be substituted for presentation time.
        pts = _to_float_or_none(pkt.get("pts_time"))
        if pts is None:
            unusable += 1
            continue
        source_pts.append({"pts": pts})

    if stats is not None:
        stats["packets_total"] = len(packets)
        stats["packets_discarded"] = discarded
        stats["packets_unusable"] = unusable

    if unusable:
        # Skipping them silently would shift every later frame index.
        raise RuntimeError(
            f"{unusable} video packet(s) have no usable presentation PTS. "
            "Frame indices cannot be trusted for this file, so "
            "PTS-grid extraction was stopped.\n\n"
            "Use the FPS grid method, or remux the video first.")
    if not source_pts:
        raise RuntimeError("No usable source-frame PTS values were found.")

    source_pts.sort(key=lambda x: x["pts"])
    for index, item in enumerate(source_pts):
        item["index"] = index
    return source_pts


def select_source_frames(source_pts, fps, stats=None):
    fps = validate_fps(fps)
    if not source_pts:
        raise ValueError("source_pts must not be empty.")

    first_pts = source_pts[0]["pts"]
    interval = 1.0 / fps
    selected = []
    skipped_duplicates = 0
    source_count = len(source_pts)
    source_position = 0
    target_number = 0

    while True:
        target_pts = first_pts + target_number * interval
        if target_pts > (source_pts[-1]["pts"] + interval / 2.0):
            break
        while (source_position + 1 < source_count
               and source_pts[source_position + 1]["pts"] <= target_pts):
            source_position += 1
        current = source_pts[source_position]
        best = current
        if source_position + 1 < source_count:
            following = source_pts[source_position + 1]
            if (abs(following["pts"] - target_pts)
                    < abs(current["pts"] - target_pts)):
                best = following
                source_position += 1
        if (selected and best["index"]
                == selected[-1]["source_index"]):
            skipped_duplicates += 1
            target_number += 1
            continue
        selected.append({
            "output_number": len(selected) + 1,
            "source_index": best["index"],
            "target_pts": target_pts,
            "actual_pts": best["pts"],
        })
        target_number += 1

    if stats is not None:
        stats["skipped_duplicates"] = skipped_duplicates
        stats["grid_positions"] = target_number

    if not selected:
        raise RuntimeError("No source frames could be selected.")
    return selected


def build_select_expression(selected_frames):
    indices = sorted(item["source_index"] for item in selected_frames)

    def build_bst(values):
        if not values:
            return "0"
        if len(values) == 1:
            return f"eq(n\\,{values[0]})"
        mid = len(values) // 2
        split_val = values[mid]
        left_expr = build_bst(values[:mid])
        right_expr = build_bst(values[mid + 1:])
        return (
            f"if(eq(n\\,{split_val})\\,"
            f"1\\,"
            f"if(lt(n\\,{split_val})\\,"
            f"{left_expr}\\,"
            f"{right_expr}))"
        )

    return build_bst(indices)


def calculate_frame_timestamp(recording_start, frame_pts, first_frame_pts):
    offset_seconds = float(frame_pts) - float(first_frame_pts)
    return recording_start + timedelta(seconds=offset_seconds)


def summarize_jpeg_sizes(sizes):
    if not sizes:
        return {}
    ordered = sorted(sizes)
    count = len(ordered)

    def percentile(p):
        if count == 1:
            return float(ordered[0])
        k = (count - 1) * p
        f, c = math.floor(k), math.ceil(k)
        if f == c:
            return float(ordered[int(k)])
        return ordered[f] * (c - k) + ordered[c] * (k - f)

    return {
        "count": count,
        "min": ordered[0],
        "max": ordered[-1],
        "mean": sum(ordered) / count,
        "median": percentile(0.5),
        "p05": percentile(0.05),
        "p95": percentile(0.95),
    }


def summarize_selection_errors(errors_ms):
    if not errors_ms:
        return {}
    ordered = sorted(errors_ms)
    count = len(ordered)
    abs_ordered = sorted(abs(e) for e in errors_ms)

    def percentile(values, p):
        if count == 1:
            return float(values[0])
        k = (count - 1) * p
        f, c = math.floor(k), math.ceil(k)
        if f == c:
            return float(values[int(k)])
        return values[f] * (c - k) + values[c] * (k - f)

    return {
        "count": count,
        "mean": sum(ordered) / count,
        "median": percentile(ordered, 0.5),
        "min": ordered[0],
        "max": ordered[-1],
        "max_abs": abs_ordered[-1],
        "p95_abs": percentile(abs_ordered, 0.95),
    }


_SHOWINFO_RE = re.compile(r"\bpts:\s*(-?\d+)\s+pts_time:\s*(\S+)")


def parse_showinfo_pts(stderr_text, time_base=None):
    """PTS values (seconds) of the frames that passed the select filter.

    showinfo prints pts_time with only ~6 significant digits, so the
    integer pts is multiplied by the stream time base when known.
    """
    values = []
    for line in stderr_text.splitlines():
        if "showinfo" not in line:
            continue
        match = _SHOWINFO_RE.search(line)
        if not match:
            continue
        if time_base:
            values.append(int(match.group(1)) * time_base)
        else:
            try:
                values.append(float(match.group(2)))
            except ValueError:
                continue
    return values


def extract_selected_frames_pts(video_path, temp_pattern, selected_frames,
                                stream_ordinal=0, time_base=None):
    if not selected_frames:
        raise RuntimeError("No selected frames.")

    temp_folder = os.path.dirname(temp_pattern)
    os.makedirs(temp_folder, exist_ok=True)

    select_expression = build_select_expression(selected_frames)
    # showinfo is used ONLY to verify, afterwards, that FFmpeg passed
    # exactly the frames that were intended. It is not a timestamp source.
    filter_expression = "select=" + select_expression + ",showinfo"

    # -copyts: keep raw container timestamps so the verification compares
    # like with like (without it FFmpeg rebases by the file start_time).
    command = [
        _FFMPEG_EXE, "-y",
        "-copyts",
        "-i", video_path,
        "-map", f"0:v:{stream_ordinal}",
    ]

    filter_script_path = None
    if len(filter_expression) > FILTER_SCRIPT_THRESHOLD:
        filter_script_path = os.path.join(temp_folder, "filter_script.txt")
        with open(filter_script_path, "w", encoding="ascii",
                  newline="\n") as handle:
            handle.write(filter_expression)
        command.extend(["-filter_script:v", filter_script_path])
    else:
        command.extend(["-vf", filter_expression])

    command.extend(["-an"] + list(_VFR_ARGS)
                   + ["-q:v", str(JPEG_QUALITY), temp_pattern])

    extraction_start = time.perf_counter()
    try:
        process = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError(
            "FFmpeg executable was not found.\n\n"
            "Make sure ffmpeg.exe is installed and available in PATH, "
            "or use the 'Locate FFmpeg folder...' button to point the "
            "application at the folder containing ffmpeg.exe and "
            "ffprobe.exe."
        )
    extraction_seconds = time.perf_counter() - extraction_start

    stdout = process.stdout.decode("utf-8", errors="replace")
    stderr = process.stderr.decode("utf-8", errors="replace")

    output_root = os.path.dirname(temp_folder)
    log_path = os.path.join(output_root, PTS_FFMPEG_LOG_NAME)
    with open(log_path, "w", encoding="utf-8", errors="replace") as handle:
        handle.write("COMMAND:\n")
        handle.write(subprocess.list2cmdline(command))
        handle.write("\n\nRETURN CODE:\n")
        handle.write(str(process.returncode))
        handle.write("\n\nSTDOUT:\n")
        handle.write(stdout)
        handle.write("\n\nSTDERR:\n")
        handle.write(stderr)
        handle.write("\n\nBENCHMARK:\n")
        handle.write(f"FFmpeg extraction time: {extraction_seconds:.6f} s\n")

    if filter_script_path is not None:
        try:
            os.remove(filter_script_path)
        except OSError:
            pass

    if process.returncode != 0:
        diagnostic = stderr.strip()
        if len(diagnostic) > 5000:
            diagnostic = diagnostic[-5000:]
        raise RuntimeError(
            "FFmpeg frame extraction failed.\n\n"
            f"FFmpeg return code: {process.returncode}\n\n"
            f"Diagnostic output:\n{diagnostic}\n\n"
            f"Complete FFmpeg log:\n{log_path}"
        )

    temp_files = sorted(
        os.path.join(temp_folder, f)
        for f in os.listdir(temp_folder)
        if f.lower().endswith(".jpg")
    )
    expected = len(selected_frames)
    actual = len(temp_files)
    if actual != expected:
        raise RuntimeError(
            "Extracted JPEG count does not match the selected "
            "source-frame count.\n\n"
            f"Selected source frames: {expected}\n"
            f"JPEG files generated: {actual}\n\n"
            f"Complete FFmpeg log:\n{log_path}"
        )

    # ---- verification: did FFmpeg pass the frames we intended? ----
    shown = parse_showinfo_pts(stderr, time_base)
    expected_pts = [item["actual_pts"] for item in selected_frames]
    problem = None
    max_error = 0.0
    mismatches = []
    if len(shown) != expected:
        problem = (f"FFmpeg's showinfo reported {len(shown)} selected "
                   f"frames; {expected} were expected.")
    else:
        for number, (want, got) in enumerate(zip(expected_pts, shown),
                                             start=1):
            error = abs(got - want)
            tolerance = (PTS_VERIFY_TOLERANCE_S if time_base
                         else max(PTS_VERIFY_TOLERANCE_S,
                                  6e-6 * abs(want)))
            max_error = max(max_error, error)
            if error > tolerance:
                mismatches.append((number, want, got))
        if mismatches:
            lines = [
                f"  output {n:06d}: expected PTS {w:.6f} s, "
                f"FFmpeg selected PTS {g:.6f} s"
                for (n, w, g) in mismatches[:5]
            ]
            problem = (
                f"{len(mismatches)} of {expected} frames differ from the "
                "intended PTS:\n" + "\n".join(lines))

    with open(log_path, "a", encoding="utf-8", errors="replace") as handle:
        handle.write("\nPTS VERIFICATION (showinfo vs. ffprobe selection):\n")
        if problem is None:
            handle.write(f"PASSED: {expected} frames, "
                         f"max |delta| = {max_error * 1000.0:.4f} ms\n")
        else:
            handle.write("FAILED: " + problem + "\n")

    if problem is not None:
        raise RuntimeError(
            "PTS verification failed: the frames FFmpeg selected are not "
            "the frames chosen from the ffprobe timestamps, so the "
            "filenames/EXIF would be wrong. No output was written.\n\n"
            + problem + "\n\n"
            "Typical cause: a file whose decoder drops frames that ffprobe "
            "still lists (stream-copy trimmed, damaged or open-GOP "
            "streams). The FPS grid method can still be used.\n\n"
            f"Complete FFmpeg log:\n{log_path}")

    verification = {"checked": expected,
                    "max_abs_error_ms": max_error * 1000.0}
    return temp_files, extraction_seconds, log_path, verification


# ============================================================
# Existing-output detection (shared)
# ============================================================

def find_existing_frame_files(output_folder):
    """Frames written by an earlier run (same filename scheme)."""
    if not os.path.isdir(output_folder):
        return []
    found = []
    for name in os.listdir(output_folder):
        path = os.path.join(output_folder, name)
        if OUTPUT_FRAME_PATTERN.match(name) and os.path.isfile(path):
            found.append(path)
    return sorted(found)


def delete_files(paths):
    failed = []
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            failed.append(path)
    return failed


# ============================================================
# PTS-mode extraction (wrapper)
# ============================================================

def extract_frames_pts(video_path, output_folder, fps,
                       progress_callback=None):
    total_start = time.perf_counter()
    fps = validate_fps(fps)
    os.makedirs(output_folder, exist_ok=True)

    metadata_start = time.perf_counter()
    info = get_video_info(video_path)
    metadata_seconds = time.perf_counter() - metadata_start

    duration = info["duration"]
    recording_start = info["recording_start"]

    pts_start = time.perf_counter()
    packet_stats = {}
    source_pts = get_source_frame_pts(
        video_path, info["video_ordinal"], packet_stats)
    pts_seconds = time.perf_counter() - pts_start

    selection_start = time.perf_counter()
    selection_stats = {}
    selected_frames = select_source_frames(
        source_pts, fps, selection_stats)
    selection_seconds = time.perf_counter() - selection_start

    if not selected_frames:
        raise RuntimeError("No frames were selected.")

    selection_error_records = []
    for item in selected_frames:
        err_ms = (float(item["actual_pts"])
                  - float(item["target_pts"])) * 1000.0
        selection_error_records.append((
            item["output_number"],
            float(item["target_pts"]),
            float(item["actual_pts"]),
            err_ms,
        ))
    selection_error_stats = summarize_selection_errors(
        [r[3] for r in selection_error_records])

    temp_folder = tempfile.mkdtemp(prefix="_temp_frames_", dir=output_folder)
    temp_pattern = os.path.join(temp_folder, "frame_%08d.jpg")

    try:
        (temp_files, ffmpeg_seconds, _ffmpeg_log,
         verification) = extract_selected_frames_pts(
            video_path, temp_pattern, selected_frames,
            stream_ordinal=info["video_ordinal"],
            time_base=info["time_base"])

        first_frame_pts = selected_frames[0]["actual_pts"]
        timestamp_records = []
        jpeg_records = []
        manifest_records = []
        total = len(temp_files)

        exif_start = time.perf_counter()
        for index in range(total):
            frame_number = index + 1
            actual_frame_pts = selected_frames[index]["actual_pts"]
            frame_time = calculate_frame_timestamp(
                recording_start, actual_frame_pts, first_frame_pts)

            output_filename = make_frame_filename(frame_time, frame_number)
            output_file = os.path.join(output_folder, output_filename)
            temp_file = temp_files[index]

            if os.path.exists(output_file):
                raise FileExistsError(
                    f"Refusing to overwrite existing frame: {output_file}. "
                    "Choose a clean output folder or delete prior frames.")
            os.replace(temp_file, output_file)

            jpeg_bytes = os.path.getsize(output_file)
            jpeg_records.append((frame_number, output_filename, jpeg_bytes))

            manifest_records.append({
                "image_id": frame_number,
                "file_name": output_filename,
                "timestamp_utc": to_utc_iso(frame_time),
                "timestamp_unix": frame_time.timestamp(),
                "width": info["width"],
                "height": info["height"],
                "file_size_bytes": jpeg_bytes,
                "frame_type": "pts_native",
                "sampling_method": "pts_grid",
                "requested_fps": fps,
                "source_frame_index": selected_frames[index].get("index"),
                "actual_pts_seconds": actual_frame_pts,
                "target_grid_pts_seconds": selected_frames[index].get("target_pts"),
                "source_video": os.path.basename(video_path),
                "source_video_path": os.path.abspath(video_path),
                "file_sha256": sha256_file(output_file)
            })

            write_frame_exif(output_file, frame_time, video_path, "pts")
            manifest_records[-1]["file_size_bytes"] = os.path.getsize(output_file)
            manifest_records[-1]["file_sha256"] = sha256_file(output_file)

            if SET_WINDOWS_FILE_TIMESTAMPS:
                timestamp_records.append((output_file, frame_time))

            if progress_callback:
                progress_callback(frame_number, total, frame_time,
                                  "extracting")
        exif_seconds = time.perf_counter() - exif_start

        write_frame_manifest(output_folder, manifest_records)
        write_run_metadata(output_folder, {
            "schema_version": 2,
            "run_id": uuid.uuid4().hex,
            "status": "complete",
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "sampling_method": "pts_grid",
            "requested_fps": fps,
            "source_video": os.path.basename(video_path),
            "source_video_path": os.path.abspath(video_path),
            "source_video_size_bytes": os.path.getsize(video_path),
            "source_video_sha256": sha256_file(video_path),
            "recording_start_iso8601": recording_start.isoformat(),
            "recording_start_utc": to_utc_iso(recording_start),
            "duration_seconds": info["duration"],
            "source_fps_nominal": info["source_fps"],
            "source_fps_average": info.get("avg_frame_rate"),
            "width": info["width"],
            "height": info["height"],
            "codec": info["codec"],
            "ffmpeg_executable": _FFMPEG_EXE,
            "ffprobe_executable": _FFPROBE_EXE,
            "frame_count": len(manifest_records),
        })

        jpeg_stats = summarize_jpeg_sizes([r[2] for r in jpeg_records])

        cleanup_temp_folder(temp_folder)

        filesystem_start = time.perf_counter()
        if SET_WINDOWS_FILE_TIMESTAMPS:
            def fs_progress(cur, tot, ft):
                if progress_callback:
                    progress_callback(cur, tot, ft, "filesystem")
            apply_windows_timestamps_batch(timestamp_records, fs_progress)
        filesystem_seconds = time.perf_counter() - filesystem_start

        total_seconds = time.perf_counter() - total_start

        benchmark_path = os.path.join(
            output_folder, PTS_BENCHMARK_LOG_NAME)
        with open(benchmark_path, "w", encoding="utf-8",
                  errors="replace") as handle:
            handle.write("VIDEO FRAME EXTRACTION BENCHMARK\n")
            handle.write("================================\n\n")
            handle.write(f"Video: {video_path}\n")
            handle.write(f"Output folder: {output_folder}\n")
            handle.write(f"Method: PTS grid\n")
            handle.write(f"User FPS: {fps:g}\n")
            handle.write(f"Source PTS count: {len(source_pts)}\n")
            handle.write(f"Selected frame count: {len(selected_frames)}\n")
            handle.write(f"Extracted JPEG count: {len(temp_files)}\n\n")

            handle.write("SOURCE / TIMESTAMP CHECKS\n"
                         "-------------------------\n")
            for line in describe_recording_start(info):
                handle.write(line + "\n")
            handle.write(
                f"Packets listed by ffprobe: "
                f"{packet_stats.get('packets_total')}\n")
            handle.write(
                f"Packets excluded (discard flag, no output frame): "
                f"{packet_stats.get('packets_discarded')}\n")
            handle.write(
                f"Grid positions skipped as duplicates: "
                f"{selection_stats.get('skipped_duplicates')}\n")
            handle.write(
                f"FFmpeg frame-PTS verification: PASSED "
                f"({verification['checked']} frames, max |delta| "
                f"{verification['max_abs_error_ms']:.4f} ms)\n")
            handle.write(
                "EXIF OffsetTime tags: "
                + ("written" if exif_offset_tags_available()
                   else "NOT written (installed piexif has no "
                        "OffsetTime definitions)")
                + "\n\n")

            handle.write("TIMINGS\n-------\n")
            handle.write(f"Video metadata: {metadata_seconds:.6f} s\n")
            handle.write(f"FFprobe source-frame PTS: {pts_seconds:.6f} s\n")
            handle.write(f"Frame selection: {selection_seconds:.6f} s\n")
            handle.write(f"FFmpeg extraction: {ffmpeg_seconds:.6f} s\n")
            handle.write(f"Python EXIF + rename: {exif_seconds:.6f} s\n")
            handle.write(
                f"Windows filesystem timestamps: {filesystem_seconds:.6f} s\n")
            handle.write(f"TOTAL: {total_seconds:.6f} s\n\n")

            handle.write("SELECTION ARCHITECTURE\n----------------------\n")
            handle.write("Sampling: fixed target PTS grid\n")
            handle.write("Source frame selection: nearest source PTS\n")
            handle.write("FFmpeg selection: Balanced BST expression tree\n")
            handle.write("FFmpeg showinfo: used for post-extraction verification only; not for timestamp calculation\n")
            handle.write("FFprobe source PTS: AUTHORITATIVE FOR "
                         "TIMESTAMP CALCULATION (Packet-based)\n")
            handle.write("Generic fps filter replacement: NOT USED\n")
            handle.write("ExifTool: NOT USED\n")
            handle.write("piexif: CURRENT IMPLEMENTATION\n")
            handle.write("JPEG size diagnostics: LOGGED "
                         "(diagnostic only, NOT used for selection)\n")
            handle.write("Selection error diagnostics: LOGGED "
                         "(diagnostic only, NOT used for selection)\n\n")

            handle.write("SELECTION ERROR DIAGNOSTICS\n---------------------------\n")
            handle.write("err_ms = (actual_pts - target_pts) * 1000\n")
            handle.write("Diagnostic only. NOT used for frame selection.\n")
            handle.write(
                "Interior bound: half the LOCAL source interval\n")
            handle.write("(e.g. +/-16.7 ms for 30 fps stretches).\n")
            handle.write(
                "Head/tail frames may legitimately exceed it\n")
            handle.write("(no earlier/later source frame exists).\n\n")
            if selection_error_stats:
                handle.write(f"Frames:        {selection_error_stats['count']}\n")
                handle.write(
                    f"Mean:          {selection_error_stats['mean']:+.3f} ms\n")
                handle.write(
                    f"Median:        "
                    f"{selection_error_stats['median']:+.3f} ms\n")
                handle.write(
                    f"Min (signed):  {selection_error_stats['min']:+.3f} ms\n")
                handle.write(
                    f"Max (signed):  {selection_error_stats['max']:+.3f} ms\n")
                handle.write(
                    f"Max |err|:     "
                    f"{selection_error_stats['max_abs']:.3f} ms\n")
                handle.write(
                    f"P95 |err|:     "
                    f"{selection_error_stats['p95_abs']:.3f} ms\n")
            else:
                handle.write("No selection error records.\n")
            handle.write("\n")

            handle.write("JPEG SIZE DIAGNOSTICS\n---------------------\n")
            handle.write(
                "Raw JPEG payload measured before EXIF injection.\n")
            handle.write(
                "Diagnostic only. NOT used for frame selection.\n\n")
            if jpeg_stats:
                handle.write(f"Frames:  {jpeg_stats['count']}\n")
                handle.write(f"Min:     {jpeg_stats['min']} bytes\n")
                handle.write(f"Max:     {jpeg_stats['max']} bytes\n")
                handle.write(f"Mean:    {jpeg_stats['mean']:.1f} bytes\n")
                handle.write(f"Median:  {jpeg_stats['median']:.1f} bytes\n")
                handle.write(f"P05:     {jpeg_stats['p05']:.1f} bytes\n")
                handle.write(f"P95:     {jpeg_stats['p95']:.1f} bytes\n")
            else:
                handle.write("No JPEG size records.\n")
            handle.write("\n")

            handle.write("PER-FRAME DIAGNOSTICS\n---------------------\n")
            handle.write("output  target_pts    actual_pts    "
                         "err_ms     jpeg_bytes  filename\n")
            for pos in range(len(jpeg_records)):
                (rec_number, rec_filename, rec_bytes) = jpeg_records[pos]
                (_n, rec_target, rec_actual, rec_err) = \
                    selection_error_records[pos]
                handle.write(
                    f"{rec_number:06d}  "
                    f"{rec_target:12.6f}  "
                    f"{rec_actual:12.6f}  "
                    f"{rec_err:+9.3f}  "
                    f"{rec_bytes:>10}  "
                    f"{rec_filename}\n"
                )
            handle.write("\n")

        return {
            "method": "pts",
            "count": total,
            "duration": duration,
            "recording_start": recording_start,
            "source_fps": info["source_fps"],
            "width": info["width"],
            "height": info["height"],
            "codec": info["codec"],
            "first_frame_pts": first_frame_pts,
            "verification": verification,
            "skipped_duplicates": selection_stats.get("skipped_duplicates", 0),
            "packets_discarded": packet_stats.get("packets_discarded", 0),
            "selected_frames": selected_frames,
            "benchmark": {
                "metadata_seconds": metadata_seconds,
                "pts_seconds": pts_seconds,
                "selection_seconds": selection_seconds,
                "ffmpeg_seconds": ffmpeg_seconds,
                "exif_seconds": exif_seconds,
                "filesystem_seconds": filesystem_seconds,
                "total_seconds": total_seconds,
            },
            "benchmark_path": benchmark_path,
            "jpeg_diagnostics": {
                "stats": jpeg_stats,
                "records": jpeg_records,
            },
            "selection_error_diagnostics": {
                "stats": selection_error_stats,
                "records": selection_error_records,
            },
        }
    except Exception:
        cleanup_temp_folder(temp_folder)
        raise


# ============================================================
# FPS-mode extraction
# ============================================================

def calculate_sampling_time(recording_start, frame_number, fps):
    if fps <= 0:
        raise ValueError("FPS must be greater than zero.")
    return recording_start + timedelta(seconds=(frame_number - 1) / fps)


def extract_frames_fps(video_path, output_folder, fps,
                       progress_callback=None):
    total_start = time.perf_counter()
    fps = validate_fps(fps)
    os.makedirs(output_folder, exist_ok=True)
    write_run_metadata(output_folder, {
        "schema_version": 2,
        "run_id": uuid.uuid4().hex,
        "status": "running",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "sampling_method": "fps_grid",
        "source_video": os.path.basename(video_path),
        "source_video_path": os.path.abspath(video_path),
        "note": "Not complete until status changes to complete."
    })

    log_lines = []

    def append_log(*items):
        log_lines.extend(items)

    def flush_log():
        log_path = os.path.join(output_folder, FPS_LOG_NAME)
        with open(log_path, "w", encoding="utf-8",
                  errors="replace") as handle:
            handle.write("\n".join(log_lines) + "\n")
        return log_path

    info = get_video_info(video_path)
    duration = info["duration"]
    recording_start = info["recording_start"]
    expected_count = calculate_expected_frame_count(duration, fps)
    source_fps = info["source_fps"]

    append_log("FPS-GRID EXTRACTION LOG")
    append_log("=======================")
    append_log("")
    append_log("Timestamp semantics: output sampling time")
    append_log("(frame index / FPS), NOT source-frame PTS.")
    append_log("")
    append_log(f"Video: {video_path}")
    append_log(f"Output folder: {output_folder}")
    append_log(f"Selected FPS: {fps:g}")
    append_log(f"Frame interval: {1.0 / fps:.6f} s")
    append_log(f"JPEG quality: {JPEG_QUALITY}")
    append_log("")
    append_log("Recording start: "
               + recording_start.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
    append_log("Timezone: "
               + (recording_start.strftime("%z") or "unknown"))
    for line in describe_recording_start(info):
        append_log(line)
    append_log(f"Duration: {duration:.3f} s")
    append_log("Source FPS (nominal r_frame_rate): "
               + (f"{source_fps:.6f}" if source_fps else "unknown"))
    avg_fps = info.get("avg_frame_rate")
    append_log("Source FPS (average avg_frame_rate): "
               + (f"{avg_fps:.6f}" if avg_fps else "unknown"))
    append_log(f"Resolution: {info['width']} x {info['height']}")
    append_log(f"Codec: {info['codec']}")
    append_log("EXIF OffsetTime tags: "
               + ("written" if exif_offset_tags_available()
                  else "NOT written (installed piexif has no "
                       "OffsetTime definitions)"))
    rate_warning = bool(source_fps and fps > source_fps * 1.001)
    if rate_warning:
        append_log("")
        append_log("WARNING: requested FPS exceeds the source frame rate.")
        append_log("FFmpeg's fps filter DUPLICATES frames to fill the grid;")
        append_log("duplicates are written as separate timestamped files.")
        append_log("Use the PTS grid method to avoid duplicates.")
    append_log("")

    temp_folder = tempfile.mkdtemp(prefix="_temp_frames_", dir=output_folder)
    temp_pattern = os.path.join(temp_folder, "frame_%08d.jpg")

    # Explicit stream selection: the same video stream that
    # get_video_info() inspected (FFmpeg's default would pick the "best"
    # video stream, which can differ in multi-stream files).
    command = [
        _FFMPEG_EXE, "-y",
        "-i", video_path,
        "-map", f"0:v:{info['video_ordinal']}",
        "-vf", f"fps=fps={fps:.10g}",
        "-an",
        "-q:v", str(JPEG_QUALITY),
        temp_pattern,
    ]
    append_log("FFMPEG COMMAND:")
    append_log(subprocess.list2cmdline(command))
    append_log("")

    failure_logged = False
    try:
        ffmpeg_start = time.perf_counter()
        try:
            process = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            raise RuntimeError(
                "FFmpeg executable was not found.\n\n"
                "Use the 'Locate FFmpeg folder...' button to point the "
                "application at the folder containing ffmpeg.exe and "
                "ffprobe.exe.")
        ffmpeg_seconds = time.perf_counter() - ffmpeg_start
        ffmpeg_stderr = _decode_bytes(process.stderr)

        if process.returncode != 0:
            append_log("RESULT: FAILED")
            append_log(f"FFmpeg time: {ffmpeg_seconds:.6f} s")
            append_log(f"FFmpeg return code: {process.returncode}")
            append_log("")
            append_log("FFMPEG STDERR:")
            append_log(ffmpeg_stderr)
            failure_logged = True
            flush_log()
            raise RuntimeError(
                "FFmpeg extraction failed:\n\n" + ffmpeg_stderr)

        temp_files = sorted(
            os.path.join(temp_folder, f)
            for f in os.listdir(temp_folder)
            if f.lower().endswith(".jpg")
        )
        produced_count = len(temp_files)
        if not temp_files:
            append_log("RESULT: FAILED")
            append_log(f"FFmpeg time: {ffmpeg_seconds:.6f} s")
            append_log("")
            append_log("FFmpeg produced no JPEG frames.")
            append_log("")
            append_log("FFMPEG STDERR:")
            append_log(ffmpeg_stderr)
            failure_logged = True
            flush_log()
            raise RuntimeError("FFmpeg did not produce any JPEG frames.")

        usable_count = produced_count
        timestamp_records = []
        per_frame_records = []
        manifest_records = []

        exif_start = time.perf_counter()
        for index in range(usable_count):
            frame_number = index + 1
            sampling_time = calculate_sampling_time(
                recording_start, frame_number, fps)

            output_filename = make_frame_filename(
                sampling_time, frame_number)
            output_file = os.path.join(output_folder, output_filename)
            temp_file = temp_files[index]

            if os.path.exists(output_file):
                raise FileExistsError(
                    f"Refusing to overwrite existing frame: {output_file}. "
                    "Choose a clean output folder or delete prior frames.")
            os.replace(temp_file, output_file)

            file_size_bytes = os.path.getsize(output_file)

            per_frame_records.append((
                frame_number,
                sampling_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                output_filename,
            ))

            manifest_records.append({
                "image_id": frame_number,
                "file_name": output_filename,
                "timestamp_utc": to_utc_iso(sampling_time),
                "timestamp_unix": sampling_time.timestamp(),
                "width": info["width"],
                "height": info["height"],
                "file_size_bytes": file_size_bytes,
                "frame_type": "fps_sampled",
                "sampling_method": "fps_grid",
                "requested_fps": fps,
                "sampling_index": index,
                "nominal_sampling_time_seconds": index / fps,
                "source_video": os.path.basename(video_path),
                "source_video_path": os.path.abspath(video_path),
                "file_sha256": sha256_file(output_file)
            })

            write_frame_exif(output_file, sampling_time, video_path, "fps")
            manifest_records[-1]["file_size_bytes"] = os.path.getsize(output_file)
            manifest_records[-1]["file_sha256"] = sha256_file(output_file)

            if SET_WINDOWS_FILE_TIMESTAMPS:
                timestamp_records.append((output_file, sampling_time))

            if progress_callback:
                progress_callback(frame_number, usable_count,
                                  sampling_time, "extracting")
        exif_seconds = time.perf_counter() - exif_start

        write_frame_manifest(output_folder, manifest_records)
        write_run_metadata(output_folder, {
            "schema_version": 2,
            "run_id": uuid.uuid4().hex,
            "status": "complete",
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "sampling_method": "fps_grid",
            "requested_fps": fps,
            "expected_frame_count_estimate": expected_count,
            "ffmpeg_produced_frame_count": produced_count,
            "source_video": os.path.basename(video_path),
            "source_video_path": os.path.abspath(video_path),
            "source_video_size_bytes": os.path.getsize(video_path),
            "source_video_sha256": sha256_file(video_path),
            "recording_start_iso8601": recording_start.isoformat(),
            "recording_start_utc": to_utc_iso(recording_start),
            "duration_seconds": duration,
            "source_fps_nominal": info.get("source_fps"),
            "source_fps_average": info.get("avg_frame_rate"),
            "width": info["width"],
            "height": info["height"],
            "codec": info["codec"],
            "ffmpeg_executable": _FFMPEG_EXE,
            "ffprobe_executable": _FFPROBE_EXE,
            "frame_count": len(manifest_records),
        })

        filesystem_start = time.perf_counter()
        if SET_WINDOWS_FILE_TIMESTAMPS:
            def fs_progress(cur, tot, st):
                if progress_callback:
                    progress_callback(cur, tot, st, "filesystem")
            apply_windows_timestamps_batch(timestamp_records, fs_progress)
        filesystem_seconds = time.perf_counter() - filesystem_start
        total_seconds = time.perf_counter() - total_start

        append_log("RESULT: SUCCESS")
        append_log("")
        append_log("COUNTS:")
        append_log(f"  Estimated output frames: {expected_count}")
        append_log(f"  FFmpeg produced:        {produced_count}")
        append_log(f"  Used:                   {usable_count}")
        if produced_count != expected_count:
            append_log("")
            append_log("  NOTE: produced != expected.")
            append_log("  The expected count is an output-grid estimate.")
            append_log("  FFmpeg's actual output count is authoritative for")
            append_log("  the generated files. No source-PTS correction is "
                       "applied.")
            append_log("  All FFmpeg-produced frames are retained.")
        append_log("")
        append_log("TIMINGS:")
        append_log(f"  FFmpeg extraction:   {ffmpeg_seconds:.6f} s")
        append_log(f"  Rename + EXIF:       {exif_seconds:.6f} s")
        append_log(f"  Windows timestamps:  {filesystem_seconds:.6f} s")
        append_log(f"  TOTAL:               {total_seconds:.6f} s")
        append_log("")
        append_log("Windows filesystem timestamp mode: "
                   + ("FINAL BATCH PASS"
                      if SET_WINDOWS_FILE_TIMESTAMPS else "DISABLED"))
        append_log("")
        append_log("FFMPEG STDERR:")
        append_log(ffmpeg_stderr)
        append_log("")
        append_log("PER-FRAME OUTPUT MAP:")
        append_log("frame  sampling_time          filename")
        for (rec_number, rec_time, rec_filename) in per_frame_records:
            append_log(f"{rec_number:06d}  {rec_time}  {rec_filename}")
        append_log("")
        append_log("Timestamp semantics: output sampling time,")
        append_log("not source-frame PTS.")

        log_path = flush_log()
    except Exception as exc:
        if not failure_logged:
            append_log("RESULT: FAILED")
            append_log(f"{type(exc).__name__}: {exc}")
            try:
                write_run_metadata(output_folder, {
                    "schema_version": 2,
                    "run_id": uuid.uuid4().hex,
                    "status": "failed",
                    "failed_at_utc": datetime.now(timezone.utc).isoformat(),
                    "sampling_method": "fps_grid",
                    "source_video": os.path.basename(video_path),
                    "source_video_path": os.path.abspath(video_path),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "note": "Output may be partial; do not treat as a complete dataset."
                })
            except OSError:
                pass
            try:
                flush_log()
            except OSError:
                pass
        raise
    finally:
        cleanup_temp_folder(temp_folder)

    return {
        "method": "fps",
        "count": usable_count,
        "expected_count": expected_count,
        "produced_count": produced_count,
        "usable_count": usable_count,
        "duration": duration,
        "recording_start": recording_start,
        "source_fps": info["source_fps"],
        "width": info["width"],
        "height": info["height"],
        "codec": info["codec"],
        "log_path": log_path,
        "rate_warning": rate_warning,
        "benchmark": {
            "ffmpeg_seconds": ffmpeg_seconds,
            "exif_seconds": exif_seconds,
            "filesystem_seconds": filesystem_seconds,
            "total_seconds": total_seconds,
        },
    }


# ============================================================
# Dispatcher
# ============================================================

def extract_frames(video_path, output_folder, fps, method="pts",
                   progress_callback=None):
    if method == "pts":
        return extract_frames_pts(
            video_path, output_folder, fps, progress_callback)
    if method == "fps":
        return extract_frames_fps(
            video_path, output_folder, fps, progress_callback)
    raise ValueError(f"Unknown extraction method: {method!r}")


# ============================================================
# Tkinter application
# ============================================================

class VideoFrameExtractorApp:
    def __init__(self, root):
        self.root = root
        self.root.title(
            "Timestamped Video Frame Extractor (PTS / FPS grid)")
        self.root.geometry("760x680")
        self.root.minsize(700, 620)

        self.video_path = None
        self.video_info = None
        self.ffmpeg_info = None

        self.create_widgets()
        self._initialize_ffmpeg()

    # --------------------------------------------------------
    # UI
    # --------------------------------------------------------

    def create_widgets(self):
        main = ttk.Frame(self.root, padding=15)
        main.pack(fill="both", expand=True)

        # Styles for the FFmpeg status label.
        style = ttk.Style()
        try:
            style.configure("FFmpegOK.TLabel", foreground="#006600")
            style.configure("FFmpegFail.TLabel", foreground="#aa0000")
            style.configure("FFmpegCheck.TLabel", foreground="#666666")
        except Exception:
            pass

        # --- Source video ----------------------------------
        ttk.Label(main, text="Source video:").grid(
            row=0, column=0, sticky="w", pady=(0, 5))
        self.video_var = tk.StringVar()
        ttk.Entry(main, textvariable=self.video_var).grid(
            row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(main, text="Browse...",
                   command=self.select_video).grid(
            row=1, column=1, sticky="ew")

        # --- Output folder ---------------------------------
        ttk.Label(main, text="Output folder:").grid(
            row=2, column=0, sticky="w", pady=(15, 5))
        self.output_var = tk.StringVar()
        ttk.Entry(main, textvariable=self.output_var).grid(
            row=3, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(main, text="Browse...",
                   command=self.select_output).grid(
            row=3, column=1, sticky="ew")

        # --- FFmpeg status ---------------------------------
        ffmpeg_frame = ttk.Frame(main)
        ffmpeg_frame.grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=(15, 0))
        ffmpeg_frame.columnconfigure(0, weight=1)

        self.ffmpeg_status_var = tk.StringVar(
            value="Checking FFmpeg...")
        self.ffmpeg_status_label = ttk.Label(
            ffmpeg_frame,
            textvariable=self.ffmpeg_status_var,
            style="FFmpegCheck.TLabel",
            wraplength=550,
            justify="left",
        )
        self.ffmpeg_status_label.grid(row=0, column=0, sticky="nw")

        self.ffmpeg_locate_button = ttk.Button(
            ffmpeg_frame,
            text="Locate FFmpeg folder...",
            command=self.locate_ffmpeg_folder,
        )
        self.ffmpeg_locate_button.grid(
            row=0, column=1, sticky="ne", padx=(8, 0))

        self.ffmpeg_reset_button = ttk.Button(
            ffmpeg_frame,
            text="Reset to PATH",
            command=self.reset_ffmpeg_to_path,
            state="disabled",
        )
        self.ffmpeg_reset_button.grid(
            row=0, column=2, sticky="ne", padx=(8, 0))

        # --- FPS -------------------------------------------
        ttk.Label(main, text="Extraction FPS:").grid(
            row=5, column=0, sticky="w", pady=(15, 5))
        self.fps_var = tk.StringVar(value=str(DEFAULT_FPS))
        ttk.Entry(main, textvariable=self.fps_var, width=15).grid(
            row=6, column=0, sticky="w")

        # --- Timestamp method ------------------------------
        ttk.Label(main, text="Timestamp method:").grid(
            row=7, column=0, sticky="w", pady=(15, 5))
        self.method_var = tk.StringVar(value=DEFAULT_METHOD)
        method_frame = ttk.Frame(main)
        method_frame.grid(row=8, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(
            method_frame,
            text="PTS grid (source-frame accurate)",
            variable=self.method_var, value="pts",
            command=self._on_method_change,
        ).pack(side="left", padx=(0, 15))
        ttk.Radiobutton(
            method_frame,
            text="FPS grid (output sampling)",
            variable=self.method_var, value="fps",
            command=self._on_method_change,
        ).pack(side="left")

        # --- Metadata preview ------------------------------
        ttk.Label(main, text="Video metadata:").grid(
            row=9, column=0, sticky="w", pady=(15, 5))
        self.metadata_text = tk.Text(main, height=14, width=80,
                                     wrap="word")
        self.metadata_text.grid(
            row=10, column=0, columnspan=2, sticky="nsew")
        scrollbar = ttk.Scrollbar(main, orient="vertical",
                                  command=self.metadata_text.yview)
        scrollbar.grid(row=10, column=2, sticky="ns")
        self.metadata_text.configure(yscrollcommand=scrollbar.set)

        self.read_metadata_button = ttk.Button(
            main, text="Read Video Metadata",
            command=self.read_metadata)
        self.read_metadata_button.grid(
            row=11, column=0, sticky="w", pady=(12, 5))

        # --- Progress + status -----------------------------
        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(main, variable=self.progress_var,
                        maximum=100).grid(
            row=12, column=0, columnspan=2, sticky="ew",
            pady=(15, 5))

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(main, textvariable=self.status_var).grid(
            row=13, column=0, columnspan=2, sticky="w")

        # --- Extract ---------------------------------------
        self.extract_button = ttk.Button(
            main, text="Extract Frames",
            command=self.start_extraction)
        self.extract_button.grid(
            row=14, column=0, columnspan=2, pady=(15, 0))

        main.columnconfigure(0, weight=1)
        main.rowconfigure(10, weight=1)

    # --------------------------------------------------------
    # FFmpeg init / status
    # --------------------------------------------------------

    def _initialize_ffmpeg(self):
        settings = load_settings()
        saved_folder = settings.get("ffmpeg_folder")

        if saved_folder and os.path.isdir(saved_folder):
            saved_info = detect_ffmpeg(custom_folder=saved_folder)
            if saved_info["ok"]:
                set_custom_ffmpeg_folder(saved_folder)
                self._apply_ffmpeg_info(saved_info)
                return
            settings.pop("ffmpeg_folder", None)
            save_settings(settings)

        info = detect_ffmpeg()
        self._apply_ffmpeg_info(info)

    def _apply_ffmpeg_info(self, info):
        self.ffmpeg_info = info

        if info["ok"]:
            configure_ffmpeg(info)
            source_label = "Custom folder" if info["source"] == "custom" else "PATH"
            exe_path = info['ffmpeg_exe']
            compat_note = ""
            if info.get("fps_mode_supported") is False:
                compat_note = (
                    "\nNote: this FFmpeg lacks -fps_mode (needs 5.1+); "
                    "using the older -vsync fallback.")
            self.ffmpeg_status_var.set(
                f"FFmpeg {info['ffmpeg_version']}\n{source_label}: {exe_path}"
                f"{compat_note}"
            )
            try:
                self.ffmpeg_status_label.configure(style="FFmpegOK.TLabel")
            except Exception:
                pass

            if info["source"] == "custom":
                self.ffmpeg_locate_button.configure(
                    text="Change FFmpeg location...")
                self.ffmpeg_reset_button.configure(state="normal")
            else:
                self.ffmpeg_locate_button.configure(
                    text="Override FFmpeg location...")
                self.ffmpeg_reset_button.configure(state="disabled")
        else:
            details = []
            if info["ffmpeg_error"]:
                details.append(f"ffmpeg: {info['ffmpeg_error']}")
            if info["ffprobe_error"]:
                details.append(f"ffprobe: {info['ffprobe_error']}")
            detail = "; ".join(details) if details else "not found"
            self.ffmpeg_status_var.set(
                "FFmpeg not available — click 'Locate FFmpeg folder...'\n"
                f"to select the folder containing ffmpeg.exe and ffprobe.exe ({detail})"
            )
            try:
                self.ffmpeg_status_label.configure(style="FFmpegFail.TLabel")
            except Exception:
                pass
            self.ffmpeg_locate_button.configure(
                text="Locate FFmpeg folder...")
            self.ffmpeg_reset_button.configure(state="disabled")

        self._refresh_action_button_state()

    def _refresh_action_button_state(self):
        ffmpeg_ok = bool(self.ffmpeg_info and self.ffmpeg_info.get("ok"))
        state = "normal" if ffmpeg_ok else "disabled"
        self.extract_button.configure(state=state)
        self.read_metadata_button.configure(state=state)

    def locate_ffmpeg_folder(self):
        folder = filedialog.askdirectory(
            title="Select the folder containing ffmpeg.exe and ffprobe.exe"
        )
        if not folder:
            return

        info = detect_ffmpeg(custom_folder=folder)
        if not info["ok"]:
            details = []
            if info["ffmpeg_error"]:
                details.append(f"ffmpeg: {info['ffmpeg_error']}")
            if info["ffprobe_error"]:
                details.append(f"ffprobe: {info['ffprobe_error']}")
            messagebox.showerror(
                "FFmpeg not usable",
                "The selected folder does not contain a working "
                "ffmpeg.exe and ffprobe.exe.\n\n"
                + "\n".join(details)
            )
            return

        set_custom_ffmpeg_folder(folder)
        settings = load_settings()
        settings["ffmpeg_folder"] = folder
        save_settings(settings)

        self._apply_ffmpeg_info(info)

        messagebox.showinfo(
            "FFmpeg configured",
            f"FFmpeg {info['ffmpeg_version']}\n"
            f"FFprobe {info['ffprobe_version']}\n\n"
            f"Folder:\n{folder}"
        )

    def reset_ffmpeg_to_path(self):
        set_custom_ffmpeg_folder(None)
        settings = load_settings()
        settings.pop("ffmpeg_folder", None)
        save_settings(settings)

        info = detect_ffmpeg()
        self._apply_ffmpeg_info(info)

        if info["ok"]:
            messagebox.showinfo(
                "FFmpeg",
                f"Now using FFmpeg from PATH.\n\n"
                f"FFmpeg {info['ffmpeg_version']}\n"
                f"Path: {info['ffmpeg_exe']}"
            )
        else:
            messagebox.showwarning(
                "FFmpeg not found in PATH",
                'FFmpeg was not found in PATH.\n\n'
                'Click "Locate FFmpeg folder..." to select the folder '
                "containing ffmpeg.exe and ffprobe.exe."
            )

    # --------------------------------------------------------
    # Select video / output
    # --------------------------------------------------------

    def select_video(self):
        filepath = filedialog.askopenfilename(
            title="Select video",
            filetypes=[
                ("Video files", "*.mov *.mp4 *.m4v *.avi *.mkv"),
                ("All files", "*.*"),
            ])
        if not filepath:
            return
        self.video_path = filepath
        self.video_var.set(filepath)
        base = os.path.splitext(os.path.basename(filepath))[0]
        parent = os.path.dirname(filepath)
        self.output_var.set(os.path.join(parent, base + "_frames"))
        self.read_metadata()

    def select_output(self):
        folder = filedialog.askdirectory(title="Select output folder")
        if folder:
            self.output_var.set(folder)

    def _on_method_change(self):
        if self.video_var.get().strip():
            self.read_metadata()

    # --------------------------------------------------------
    # Read metadata
    # --------------------------------------------------------

    def read_metadata(self):
        video_path = self.video_var.get().strip()
        if not video_path:
            messagebox.showwarning(
                "No video", "Please select a video first.")
            return
        if not os.path.isfile(video_path):
            messagebox.showerror(
                "File not found",
                "The selected video file does not exist.")
            return

        try:
            info = get_video_info(video_path)
            self.video_info = info

            recording_start = info["recording_start"]
            recording_start_text = recording_start.strftime(
                "%Y-%m-%d %H:%M:%S.%f")[:-3]
            duration = info["duration"]
            source_fps = info["source_fps"]
            width = info["width"]
            height = info["height"]
            codec = info["codec"]
            start_time = info["start_time"]
            format_start_time = info["format_start_time"]

            extraction_fps = validate_fps(self.fps_var.get())
            expected_count = calculate_expected_frame_count(
                duration, extraction_fps)
            interval = 1.0 / extraction_fps

            method = self.method_var.get()

            metadata = []
            metadata.append("Recording start: " + recording_start_text)
            metadata.append("Timezone: "
                            + (recording_start.strftime("%z")
                               or "unknown"))
            metadata.append(f"Duration: {duration:.3f} s")
            if source_fps:
                metadata.append(f"Source FPS: {source_fps:.6f}")
            metadata.append(f"Resolution: {width} x {height}")
            metadata.append(f"Codec: {codec}")
            metadata.append(f"Video stream start_time: {start_time:.6f} s")
            metadata.append("")

            for line in describe_recording_start(info):
                metadata.append(line)
            if source_fps and extraction_fps > source_fps * 1.001:
                metadata.append(
                    "WARNING: selected FPS exceeds the source frame rate "
                    "(PTS grid skips duplicates; FPS grid duplicates frames).")
            metadata.append("")

            metadata.append(f"Selected FPS: {extraction_fps:g}")
            metadata.append(f"Sampling interval: {interval:.6f} s")
            metadata.append(
                f"Approximate grid positions: {expected_count}")
            metadata.append("")
            metadata.append(
                "Method: "
                + ("PTS grid (source-frame accurate)"
                   if method == "pts"
                   else "FPS grid (output sampling)"))
            metadata.append("")

            if method == "pts":
                metadata.append(
                    f"Container start_time: {format_start_time:.6f} s")
                metadata.append("")
                metadata.append("Timestamp architecture (PTS grid):")
                metadata.append("  iPhone Creation Date")
                metadata.append("        ↓")
                metadata.append("  original source-frame PTS")
                metadata.append("        ↓")
                metadata.append("  fixed sampling grid")
                metadata.append("        ↓")
                metadata.append("  nearest source frame")
                metadata.append("        ↓")
                metadata.append("  selected source-frame PTS")
                metadata.append("        ↓")
                metadata.append("  frame_time")
                metadata.append("     ↙     ↓     ↘")
                metadata.append(
                    " filename  EXIF  Windows timestamps")
                metadata.append("")
                metadata.append("FFmpeg showinfo: used for verification only, not timestamp calculation")
                metadata.append(
                    "FFprobe source PTS: authoritative for "
                    "timestamp calculation (Packet-based)")
                metadata.append("")
                metadata.append("Frame-selection optimization:")
                metadata.append(
                    "  Current method: Balanced BST decision tree")
                metadata.append(
                    "  Status: Optimized for O(log S) evaluation")
                metadata.append("  Alternative fps filter: NOT used")
                metadata.append("")
                metadata.append("EXIF:")
                metadata.append("  Current implementation: piexif")
                metadata.append("  ExifTool: NOT used")
                metadata.append("")
                metadata.append("Diagnostics (observational only):")
                metadata.append(
                    "  JPEG size: LOGGED, NOT used for selection")
                metadata.append(
                    "  Selection error: LOGGED, NOT used for selection")
                metadata.append("")
                metadata.append(
                    "Logs: "
                    f"{PTS_BENCHMARK_LOG_NAME}, "
                    f"{PTS_FFMPEG_LOG_NAME}")
            else:
                metadata.append(
                    "Timestamp architecture (FPS grid):")
                metadata.append("  iPhone Creation Date")
                metadata.append("        ↓")
                metadata.append("  recording_start")
                metadata.append("        ↓")
                metadata.append(
                    "  output frame index / selected FPS")
                metadata.append("        ↓")
                metadata.append("  sampling_time")
                metadata.append("     ↙      ↓      ↘")
                metadata.append(
                    " filename  EXIF  Windows timestamps")
                metadata.append("")
                metadata.append("Timestamp semantics:")
                metadata.append(
                    "  Output sampling time,")
                metadata.append(
                    "  not source-frame PTS")
                metadata.append("")
                metadata.append(
                    "For source-frame-accurate PTS timestamps,")
                metadata.append("use PTS grid method.")
                metadata.append("")
                metadata.append(
                    f"Log: {FPS_LOG_NAME} in output folder")

            metadata.append("")
            metadata.append("FFmpeg status:")
            if self.ffmpeg_info and self.ffmpeg_info.get("ok"):
                src = ("custom folder"
                       if self.ffmpeg_info["source"] == "custom"
                       else "PATH")
                metadata.append(
                    f"  FFmpeg:  {self.ffmpeg_info['ffmpeg_version']}")
                metadata.append(
                    f"  FFprobe: {self.ffmpeg_info['ffprobe_version']}")
                metadata.append(f"  Source:  {src}")
                metadata.append(f"  Path:    {self.ffmpeg_info['ffmpeg_exe']}")
            else:
                metadata.append("  FFmpeg not available")

            metadata.append("")
            metadata.append(
                "Windows filesystem timestamp mode: "
                + ("FINAL BATCH PASS"
                   if SET_WINDOWS_FILE_TIMESTAMPS else "DISABLED"))

            self.metadata_text.delete("1.0", "end")
            self.metadata_text.insert("1.0", "\n".join(metadata))
            self.status_var.set("Metadata loaded.")

        except Exception as exc:
            messagebox.showerror("Metadata error", str(exc))
            self.status_var.set("Metadata read failed.")

    # --------------------------------------------------------
    # Start extraction
    # --------------------------------------------------------

    def start_extraction(self):
        if not (self.ffmpeg_info and self.ffmpeg_info.get("ok")):
            messagebox.showerror(
                "FFmpeg not available",
                "FFmpeg and ffprobe are required but were not found.\n\n"
                'Click "Locate FFmpeg folder..." to select the folder '
                "containing ffmpeg.exe and ffprobe.exe."
            )
            return

        video_path = self.video_var.get().strip()
        output_folder = self.output_var.get().strip()

        if not video_path:
            messagebox.showwarning(
                "No video", "Please select a video.")
            return
        if not os.path.isfile(video_path):
            messagebox.showerror(
                "File not found",
                "The selected video file does not exist.")
            return
        if not output_folder:
            messagebox.showwarning(
                "No output folder",
                "Please select an output folder.")
            return

        try:
            fps = validate_fps(self.fps_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid FPS", str(exc))
            return

        method = self.method_var.get()
        if method not in ("pts", "fps"):
            messagebox.showerror(
                "Invalid method",
                "Please select a timestamp method.")
            return

        try:
            info = get_video_info(video_path)
            self.video_info = info
        except Exception as exc:
            messagebox.showerror("Video metadata error", str(exc))
            return

        recording_start = info["recording_start"]
        expected_count = calculate_expected_frame_count(
            info["duration"], fps)
        interval = 1.0 / fps
        recording_start_text = recording_start.strftime(
            "%Y-%m-%d %H:%M:%S.%f")[:-3]

        if SET_WINDOWS_FILE_TIMESTAMPS:
            filesystem_mode = (
                "\n\nWindows filesystem timestamps will be updated "
                "only AFTER all frames have been extracted.")
        else:
            filesystem_mode = (
                "\n\nWindows filesystem timestamps will NOT be "
                "modified.")

        if method == "pts":
            method_block = (
                "Method: PTS grid (source-frame accurate)\n"
                "Timestamp source:\n"
                "original FFprobe source-frame PTS.\n\n"
                "The sampling grid is fixed and does not "
                "accumulate PTS rounding/drift.\n\n"
                "FFmpeg showinfo is not used for timestamp "
                "calculation.\n\n"
                "The FFmpeg frame-selection expression uses an "
                "optimized Balanced BST decision tree to reduce "
                "evaluation overhead.\n\n"
                "JPEG byte sizes and selection timing errors "
                "will be logged as diagnostics (not used for "
                "selection).\n\n"
                f"A benchmark log ({PTS_BENCHMARK_LOG_NAME}) and "
                f"an FFmpeg log ({PTS_FFMPEG_LOG_NAME}) will be "
                "written after extraction."
            )
        else:
            method_block = (
                "Method: FPS grid (output sampling)\n"
                "Timestamp semantics:\n"
                "Output sampling time (frame index / FPS),\n"
                "not guaranteed source-frame PTS.\n\n"
                "Filename and EXIF will use the same "
                "sampling timestamp.\n\n"
                "FFmpeg's fps filter is used.\n\n"
                f"A run log ({FPS_LOG_NAME}) will be written to "
                "the output folder."
            )

        warning_lines = [
            line.strip() for line in describe_recording_start(info)
            if line.strip().startswith(("WARNING", "NOTE"))
        ]
        if info["source_fps"] and fps > info["source_fps"] * 1.001:
            warning_lines.append(
                "WARNING: FPS exceeds the source frame rate "
                + ("(PTS grid will skip duplicate grid positions)."
                   if method == "pts"
                   else "(FPS grid will write duplicated frames)."))
        warning_block = (
            "\n\n" + "\n".join(warning_lines)) if warning_lines else ""

        confirmation = (
            "Extraction settings:\n\n"
            f"FPS: {fps:g}\n"
            f"Sampling interval: {interval:.6f} s\n"
            f"Approximate grid positions: {expected_count}\n\n"
            "Recording start:\n"
            f"{recording_start_text}"
            f"{filesystem_mode}\n\n"
            f"{method_block}"
            f"{warning_block}"
        )

        if not messagebox.askyesno("Start extraction", confirmation):
            return

        existing_frames = find_existing_frame_files(output_folder)
        if existing_frames:
            answer = messagebox.askyesnocancel(
                "Existing frames found",
                f"The output folder already contains "
                f"{len(existing_frames)} frame JPEG(s) from an earlier "
                "run.\n\nLeaving them would mix old and new frames "
                "(different FPS or method give different filenames).\n\n"
                "Yes = delete existing frame JPEGs first\n"
                "No = keep existing files; any filename collision aborts\n"
                "Cancel = abort")
            if answer is None:
                return
            if answer:
                failed = delete_files(existing_frames)
                if failed:
                    messagebox.showwarning(
                        "Some files not deleted",
                        f"{len(failed)} file(s) could not be deleted "
                        "(in use or read-only). They remain in the "
                        "output folder.")

        self.extract_button.configure(state="disabled")
        self.read_metadata_button.configure(state="disabled")
        self.progress_var.set(0)
        self.status_var.set(
            "Reading source frame PTS..."
            if method == "pts" else "Extracting...")
        self.root.update_idletasks()

        try:
            def progress_callback(current, total, frame_time, stage):
                if total <= 0:
                    percentage = 0
                else:
                    percentage = current / total * 100
                self.progress_var.set(percentage)
                ts = frame_time.strftime(
                    "%Y-%m-%d %H:%M:%S.%f")[:-3]
                if stage == "filesystem":
                    status = (f"Finalizing Windows timestamps: "
                              f"{current}/{total}   {ts}")
                else:
                    status = f"Frame {current}/{total}   {ts}"
                self.status_var.set(status)
                self.root.update_idletasks()

            result = extract_frames(
                video_path, output_folder, fps,
                method=method, progress_callback=progress_callback)

            self.progress_var.set(100)
            self.status_var.set("Extraction completed.")

            recording_start_text = result["recording_start"].strftime(
                "%Y-%m-%d %H:%M:%S.%f")[:-3]
            benchmark = result["benchmark"]

            if result["method"] == "pts":
                jpeg_stats = result["jpeg_diagnostics"]["stats"]
                if jpeg_stats:
                    jpeg_line = (
                        "JPEG size (diagnostic, pre-EXIF):\n"
                        f"  mean   {jpeg_stats['mean']/1024:.1f} KiB   "
                        f"median {jpeg_stats['median']/1024:.1f} KiB\n"
                        f"  p05    {jpeg_stats['p05']/1024:.1f} KiB   "
                        f"p95    {jpeg_stats['p95']/1024:.1f} KiB\n\n")
                else:
                    jpeg_line = ""

                sel_stats = result["selection_error_diagnostics"]["stats"]
                if sel_stats:
                    sel_line = (
                        "Selection error (actual - target PTS):\n"
                        f"  median {sel_stats['median']:+.1f} ms   "
                        f"max |err| {sel_stats['max_abs']:.1f} ms\n\n")
                else:
                    sel_line = ""

                verification = result["verification"]
                check_line = (
                    "FFmpeg PTS verification: passed "
                    f"({verification['checked']} frames, max |delta| "
                    f"{verification['max_abs_error_ms']:.3f} ms)\n")
                if result["skipped_duplicates"]:
                    check_line += (
                        f"Grid positions skipped as duplicates: "
                        f"{result['skipped_duplicates']}\n")
                check_line += "\n"

                messagebox.showinfo(
                    "Extraction complete",
                    (
                        f"Method: PTS grid\n"
                        f"Frames extracted: {result['count']}\n\n"
                        f"Recording start:\n{recording_start_text}\n\n"
                        f"Output folder:\n{output_folder}\n\n"
                        "Benchmark:\n"
                        f"FFprobe PTS: "
                        f"{benchmark['pts_seconds']:.3f} s\n"
                        f"FFmpeg extraction: "
                        f"{benchmark['ffmpeg_seconds']:.3f} s\n"
                        f"EXIF + rename: "
                        f"{benchmark['exif_seconds']:.3f} s\n"
                        f"Total: "
                        f"{benchmark['total_seconds']:.3f} s\n\n"
                        f"{sel_line}{check_line}{jpeg_line}"
                        "FFmpeg diagnostic log:\n"
                        f"{os.path.join(output_folder, PTS_FFMPEG_LOG_NAME)}\n\n"
                        "Benchmark log:\n"
                        f"{result['benchmark_path']}\n\n"
                        f"Manifests written:\n"
                        f"{os.path.join(output_folder, MANIFEST_CSV_NAME)}\n"
                        f"{os.path.join(output_folder, MANIFEST_JSON_NAME)}"
                    ))
            else:
                messagebox.showinfo(
                    "Extraction complete",
                    (
                        f"Method: FPS grid\n"
                        f"Used frames: {result['usable_count']}\n\n"
                        f"Expected output frames: "
                        f"{result['expected_count']}\n"
                        f"FFmpeg produced:        "
                        f"{result['produced_count']}\n"
                        f"Used:                   "
                        f"{result['usable_count']}\n\n"
                        "Timestamp semantics:\n"
                        "  Output sampling time,\n"
                        "  not source-frame PTS\n\n"
                        f"Recording start:\n{recording_start_text}\n\n"
                        f"Output folder:\n{output_folder}\n\n"
                        f"Run log:\n{result['log_path']}\n\n"
                        f"Manifests written:\n"
                        f"{os.path.join(output_folder, MANIFEST_CSV_NAME)}\n"
                        f"{os.path.join(output_folder, MANIFEST_JSON_NAME)}"
                    ))

        except Exception as exc:
            self.status_var.set("Extraction failed.")
            messagebox.showerror("Extraction error", str(exc))
        finally:
            self._refresh_action_button_state()


# ============================================================
# Main
# ============================================================

def main():
    root = tk.Tk()
    VideoFrameExtractorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
