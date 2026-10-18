import os
import sys
import ctypes
import subprocess
import tkinter as tk
import time
import json  # Added for fast packet-based FFprobe parsing
import math  # Diagnostic percentile interpolation

from tkinter import filedialog, messagebox, ttk
from datetime import datetime, timedelta, timezone

import ffmpeg
import piexif


# ============================================================
# Configuration
# ============================================================

DEFAULT_FPS = 2.0

# False = safest / lowest filesystem activity.
#
# Filename + EXIF are always synchronized to frame_time.
#
# True = after ALL JPEGs have been extracted and EXIF has been
# written, perform ONE final filesystem timestamp pass.
SET_WINDOWS_FILE_TIMESTAMPS = False

SET_WINDOWS_CREATED_TIME = True
SET_WINDOWS_MODIFIED_TIME = True
SET_WINDOWS_ACCESSED_TIME = False

JPEG_QUALITY = 2

# Windows CreateProcess has a command-line length limit of
# approximately 32767 characters.
#
# Use a conservative threshold for the FFmpeg filter expression.
FILTER_SCRIPT_THRESHOLD = 20000


# ============================================================
# Windows FILETIME support
# ============================================================

def datetime_to_filetime(dt):
    """
    Convert timezone-aware Python datetime to Windows FILETIME.
    """

    if dt.tzinfo is None:
        raise ValueError(
            "datetime must be timezone-aware."
        )

    dt_utc = dt.astimezone(timezone.utc)

    epoch = datetime(
        1601,
        1,
        1,
        tzinfo=timezone.utc
    )

    intervals = int(
        (dt_utc - epoch).total_seconds()
        * 10_000_000
    )

    return intervals


def set_windows_file_timestamps(
    filepath,
    created_time=None,
    modified_time=None,
    accessed_time=None
):
    """
    Set selected Windows filesystem timestamps.
    """

    if sys.platform != "win32":
        return

    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3

    kernel32 = ctypes.windll.kernel32

    handle = kernel32.CreateFileW(
        filepath,
        GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None
    )

    if handle == -1:
        raise ctypes.WinError()

    try:

        class FILETIME(ctypes.Structure):
            _fields_ = [
                (
                    "dwLowDateTime",
                    ctypes.c_uint32
                ),
                (
                    "dwHighDateTime",
                    ctypes.c_uint32
                ),
            ]

        def make_filetime(dt):

            if dt is None:
                return None

            value = datetime_to_filetime(dt)

            return FILETIME(
                value & 0xFFFFFFFF,
                (value >> 32) & 0xFFFFFFFF
            )

        creation = make_filetime(
            created_time
        )

        access = make_filetime(
            accessed_time
        )

        write = make_filetime(
            modified_time
        )

        result = kernel32.SetFileTime(
            handle,
            ctypes.byref(creation)
            if creation is not None
            else None,
            ctypes.byref(access)
            if access is not None
            else None,
            ctypes.byref(write)
            if write is not None
            else None
        )

        if not result:
            raise ctypes.WinError()

    finally:

        kernel32.CloseHandle(
            handle
        )


# ============================================================
# Video datetime parsing
# ============================================================

def parse_video_datetime(value):
    """
    Parse a video metadata datetime.

    Timezone information is preserved.

    No manual timezone correction is performed.
    """

    if not value:
        return None

    value = str(value).strip()

    if (
        len(value) >= 19
        and value[4] == ":"
        and value[7] == ":"
    ):

        value = (
            value[:4]
            + "-"
            + value[5:7]
            + "-"
            + value[8:]
        )

    if value.endswith("Z"):

        value = (
            value[:-1]
            + "+00:00"
        )

    try:

        dt = datetime.fromisoformat(
            value
        )

        if dt.tzinfo is None:

            dt = dt.astimezone()

        return dt

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

            dt = datetime.strptime(
                value,
                fmt
            )

            if dt.tzinfo is None:

                dt = dt.astimezone()

            return dt

        except ValueError:

            continue

    return None


def find_tag(tags, *names):
    """
    Find metadata tag using case-insensitive matching.
    """

    if not tags:
        return None

    normalized = {
        str(key).lower().strip(): value
        for key, value in tags.items()
    }

    for name in names:

        value = normalized.get(
            str(name).lower().strip()
        )

        if value not in (
            None,
            "",
            "0000:00:00 00:00:00"
        ):

            return value

    return None


# ============================================================
# Original video recording start
# ============================================================

def get_video_creation_time(video_path):
    """
    Determine the ORIGINAL VIDEO RECORDING START TIME.

    Priority:
        1. Apple/iPhone Creation Date
        2. Generic creation metadata

    Never uses filesystem creation time.
    """

    try:

        probe = ffmpeg.probe(
            video_path
        )

    except ffmpeg.Error as exc:

        stderr = ""

        if exc.stderr:

            try:

                stderr = exc.stderr.decode(
                    "utf-8",
                    errors="replace"
                )

            except Exception:

                stderr = str(
                    exc.stderr
                )

        raise RuntimeError(
            "FFprobe failed while reading "
            "video metadata:\n\n"
            + stderr
        )

    format_tags = (
        probe
        .get("format", {})
        .get("tags", {})
    )

    stream_tags = {}

    for stream in probe.get(
        "streams",
        []
    ):

        if stream.get(
            "codec_type"
        ) == "video":

            stream_tags.update(
                stream.get(
                    "tags",
                    {}
                )
            )

    all_tags = {}

    all_tags.update(
        format_tags
    )

    all_tags.update(
        stream_tags
    )

    creation_value = find_tag(
        all_tags,
        "com.apple.quicktime.creationdate",
        "Creation Date",
        "creation date",
        "creationdate"
    )

    if creation_value:

        recording_start = (
            parse_video_datetime(
                creation_value
            )
        )

        if recording_start is not None:

            return recording_start

    fallback_value = find_tag(
        all_tags,
        "creation_time",
        "date",
        "datetime"
    )

    if fallback_value:

        recording_start = (
            parse_video_datetime(
                fallback_value
            )
        )

        if recording_start is not None:

            return recording_start

    raise RuntimeError(
        "Could not determine the original video "
        "recording start time from metadata.\n\n"
        "The program will not substitute the Windows "
        "file creation time."
    )


# ============================================================
# Video information
# ============================================================

def get_video_info(video_path):

    try:

        probe = ffmpeg.probe(
            video_path
        )

    except ffmpeg.Error as exc:

        stderr = ""

        if exc.stderr:

            try:

                stderr = exc.stderr.decode(
                    "utf-8",
                    errors="replace"
                )

            except Exception:

                stderr = str(
                    exc.stderr
                )

        raise RuntimeError(
            "FFprobe failed:\n\n"
            + stderr
        )

    video_stream = None

    for stream in probe.get(
        "streams",
        []
    ):

        if stream.get(
            "codec_type"
        ) == "video":

            video_stream = stream
            break

    if video_stream is None:

        raise RuntimeError(
            "No video stream was found."
        )

    # --------------------------------------------------------
    # Duration
    # --------------------------------------------------------

    duration = None

    if video_stream.get(
        "duration"
    ):

        try:

            duration = float(
                video_stream["duration"]
            )

        except (
            ValueError,
            TypeError
        ):

            duration = None

    if duration is None:

        format_duration = (
            probe
            .get("format", {})
            .get("duration")
        )

        if format_duration:

            try:

                duration = float(
                    format_duration
                )

            except (
                ValueError,
                TypeError
            ):

                duration = None

    if duration is None:

        raise RuntimeError(
            "Could not determine video duration."
        )

    # --------------------------------------------------------
    # Source FPS
    # --------------------------------------------------------

    source_fps = None

    rate_text = video_stream.get(
        "r_frame_rate"
    )

    if (
        rate_text
        and "/" in rate_text
    ):

        numerator, denominator = (
            rate_text.split(
                "/",
                1
            )
        )

        try:

            numerator = float(
                numerator
            )

            denominator = float(
                denominator
            )

            if denominator != 0:

                source_fps = (
                    numerator
                    / denominator
                )

        except (
            ValueError,
            TypeError
        ):

            pass

    width = video_stream.get(
        "width"
    )

    height = video_stream.get(
        "height"
    )

    codec = video_stream.get(
        "codec_name"
    )

    start_time = video_stream.get(
        "start_time"
    )

    try:

        start_time = (
            float(start_time)
            if start_time is not None
            else 0.0
        )

    except (
        ValueError,
        TypeError
    ):

        start_time = 0.0

    # --------------------------------------------------------
    # Container / format start time
    #
    # Kept for diagnostics.
    #
    # It is NOT used to calculate frame timestamps.
    # --------------------------------------------------------

    format_start = (
        probe
        .get("format", {})
        .get("start_time")
    )

    try:

        format_start_time = (
            float(format_start)
            if format_start is not None
            else 0.0
        )

    except (
        ValueError,
        TypeError
    ):

        format_start_time = 0.0

    recording_start = (
        get_video_creation_time(
            video_path
        )
    )

    return {
        "duration": duration,
        "source_fps": source_fps,
        "width": width,
        "height": height,
        "codec": codec,
        "start_time": start_time,
        "format_start_time": format_start_time,
        "recording_start": recording_start,
    }


# ============================================================
# Source-frame PTS (OPTIMIZED PACKET READING)
# ============================================================

def get_source_frame_pts(video_path):
    """
    Read the original video frame PTS values using PACKETS.

    By reading packets instead of frames, we bypass H.264 decoding
    and read timestamps directly from the container index.
    This reduces processing time from minutes to milliseconds.

    Returns decoded source-frame order:
    [
        { "index": 0, "pts": 0.000000 },
        { "index": 1, "pts": 0.033333 },
        ...
    ]
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_packets",
        "-show_entries", "packet=pts_time,dts_time",
        "-of", "json",
        video_path
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace"
        )

        if result.returncode != 0:
            raise RuntimeError(
                "FFprobe failed while reading "
                "source frame PTS:\n\n"
                + result.stderr
            )

        probe = json.loads(result.stdout)

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Failed to parse FFprobe JSON output:\n\n{exc}"
        )
    except Exception as exc:
        raise RuntimeError(
            f"FFprobe execution failed:\n\n{exc}"
        )

    packets = probe.get("packets", [])

    if not packets:
        raise RuntimeError("FFprobe returned no video packets.")

    source_pts = []

    for pkt in packets:
        # Prefer PTS, fallback to DTS if PTS is missing
        pts_value = pkt.get("pts_time")
        if pts_value is None:
            pts_value = pkt.get("dts_time")

        if pts_value is None:
            continue

        try:
            pts = float(pts_value)
        except (ValueError, TypeError):
            continue

        source_pts.append({"pts": pts})

    if not source_pts:
        raise RuntimeError(
            "No usable source-frame PTS values "
            "were found."
        )

    # PATCH: weakened claim.
    #
    # Sort by PTS to obtain presentation order.
    #
    # For the supported iPhone H.264 / MOV workflow, this index
    # is INTENDED to correspond to FFmpeg's decoded frame order
    # used by select(n). This is an empirical property of the
    # tested material, NOT a universal FFmpeg guarantee:
    # streams with missing packet PTS (DTS fallback), multi-frame
    # packets, or duplicate PTS values can break the
    # correspondence.
    #
    # The extraction-count validation proves cardinality,
    # not index correspondence.
    source_pts.sort(key=lambda x: x["pts"])

    # Assign the final presentation-order index
    for index, item in enumerate(source_pts):
        item["index"] = index

    return source_pts


# ============================================================
# Select source frames against FIXED sampling grid
# ============================================================

def select_source_frames(
    source_pts,
    fps
):
    """
    Select actual source frames using a FIXED sampling grid.

    Target sequence:

        first_pts + 0/fps
        first_pts + 1/fps
        first_pts + 2/fps
        ...

    The previous selected frame is NEVER used to calculate the
    next target.

    For every target, the source frame whose PTS is nearest to
    that target is selected.
    """

    if fps <= 0:

        raise ValueError(
            "FPS must be greater than zero."
        )

    if not source_pts:

        raise ValueError(
            "source_pts must not be empty."
        )

    first_pts = (
        source_pts[0]["pts"]
    )

    interval = 1.0 / fps

    selected = []

    source_count = len(
        source_pts
    )

    source_position = 0

    target_number = 0

    while True:

        target_pts = (
            first_pts
            + target_number * interval
        )

        if target_pts > (
            source_pts[-1]["pts"]
            + interval / 2.0
        ):

            break

        while (
            source_position + 1
            < source_count
            and source_pts[
                source_position + 1
            ]["pts"] <= target_pts
        ):

            source_position += 1

        current = source_pts[
            source_position
        ]

        best = current

        if (
            source_position + 1
            < source_count
        ):

            following = source_pts[
                source_position + 1
            ]

            current_distance = abs(
                current["pts"]
                - target_pts
            )

            following_distance = abs(
                following["pts"]
                - target_pts
            )

            if following_distance < (
                current_distance
            ):

                best = following

                source_position += 1

        # ----------------------------------------------------
        # Never select the same source frame twice.
        # ----------------------------------------------------

        if (
            selected
            and best["index"]
            == selected[-1]["source_index"]
        ):

            target_number += 1
            continue

        selected.append(
            {
                "output_number": (
                    len(selected) + 1
                ),

                "source_index": (
                    best["index"]
                ),

                "target_pts": (
                    target_pts
                ),

                # AUTHORITATIVE timestamp PTS.
                #
                # This value comes directly from FFprobe.
                "actual_pts": (
                    best["pts"]
                ),
            }
        )

        target_number += 1

    if not selected:

        raise RuntimeError(
            "No source frames could be selected."
        )

    return selected


# ============================================================
# Build FFmpeg select expression
# ============================================================

def build_select_expression(
    selected_frames
):
    """
    Build a balanced binary-search expression for FFmpeg's
    select filter.

    The selected source frame indices are unchanged. Only the
    expression used by FFmpeg to test n is optimized.
    """

    indices = sorted(
        item["source_index"]
        for item in selected_frames
    )

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


# ============================================================
# Extract exact source frames
# ============================================================

def extract_selected_frames(
    video_path,
    temp_pattern,
    selected_frames
):
    """
    Extract exactly the source frame indices determined by
    select_source_frames().

    No showinfo is used.

    PTS is intentionally NOT obtained from FFmpeg showinfo.

    The authoritative PTS values are already stored in
    selected_frames from FFprobe.

    Returns:

        temp_files, extraction_seconds
    """

    if not selected_frames:

        raise RuntimeError(
            "No selected frames."
        )

    temp_folder = os.path.dirname(
        temp_pattern
    )

    os.makedirs(
        temp_folder,
        exist_ok=True
    )

    # --------------------------------------------------------
    # Build select expression
    # --------------------------------------------------------

    select_expression = (
        build_select_expression(
            selected_frames
        )
    )

    # Do NOT use showinfo.
    #
    # Also avoid unnecessary quote characters here.
    #
    # The subprocess call does not go through a shell, so the
    # FFmpeg filter expression can be passed directly.
    filter_expression = (
        "select="
        + select_expression
    )

    # --------------------------------------------------------
    # FFmpeg command
    # --------------------------------------------------------

    command = [
        "ffmpeg",
        "-y",

        # Explicitly select the same video stream that FFprobe
        # analyzed with select_streams="v:0".
        "-i",
        video_path,

        "-map",
        "0:v:0",
    ]

    filter_script_path = None

    # --------------------------------------------------------
    # Avoid Windows command-line length limit.
    # --------------------------------------------------------

    if len(filter_expression) > (
        FILTER_SCRIPT_THRESHOLD
    ):

        filter_script_path = os.path.join(
            temp_folder,
            "filter_script.txt"
        )

        with open(
            filter_script_path,
            "w",
            encoding="ascii",
            newline="\n"
        ) as handle:

            handle.write(
                filter_expression
            )

        command.extend(
            [
                "-filter_script:v",
                filter_script_path,
            ]
        )

    else:

        command.extend(
            [
                "-vf",
                filter_expression,
            ]
        )

    command.extend(
        [
            # Explicitly disable audio output because only the
            # selected video frames are required.
            "-an",

            # Do not duplicate/drop selected frames.
            "-fps_mode",
            "vfr",

            "-q:v",
            str(JPEG_QUALITY),

            temp_pattern,
        ]
    )

    # --------------------------------------------------------
    # Run FFmpeg
    # --------------------------------------------------------

    extraction_start = time.perf_counter()

    try:

        process = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

    except FileNotFoundError:

        raise RuntimeError(
            "FFmpeg executable was not found.\n\n"
            "Make sure ffmpeg.exe is installed and "
            "available in PATH."
        )

    extraction_seconds = (
        time.perf_counter()
        - extraction_start
    )

    stdout = process.stdout.decode(
        "utf-8",
        errors="replace"
    )

    stderr = process.stderr.decode(
        "utf-8",
        errors="replace"
    )

    # --------------------------------------------------------
    # Always preserve the complete FFmpeg diagnostic log.
    # --------------------------------------------------------

    output_root = os.path.dirname(
        temp_folder
    )

    log_path = os.path.join(
        output_root,
        "ffmpeg_last_run.log"
    )

    with open(
        log_path,
        "w",
        encoding="utf-8",
        errors="replace"
    ) as handle:

        handle.write(
            "COMMAND:\n"
        )

        handle.write(
            subprocess.list2cmdline(
                command
            )
        )

        handle.write(
            "\n\n"
        )

        handle.write(
            "RETURN CODE:\n"
        )

        handle.write(
            str(process.returncode)
        )

        handle.write(
            "\n\n"
        )

        handle.write(
            "STDOUT:\n"
        )

        handle.write(
            stdout
        )

        handle.write(
            "\n\n"
        )

        handle.write(
            "STDERR:\n"
        )

        handle.write(
            stderr
        )

        handle.write(
            "\n\n"
        )

        handle.write(
            "BENCHMARK:\n"
        )

        handle.write(
            "FFmpeg extraction time: "
            f"{extraction_seconds:.6f} seconds\n"
        )

    # --------------------------------------------------------
    # Remove filter script after FFmpeg has finished.
    # --------------------------------------------------------

    if filter_script_path is not None:

        try:

            os.remove(
                filter_script_path
            )

        except OSError:

            pass

    # --------------------------------------------------------
    # FFmpeg failure
    # --------------------------------------------------------

    if process.returncode != 0:

        diagnostic = stderr.strip()

        if len(diagnostic) > 5000:

            diagnostic = (
                diagnostic[-5000:]
            )

        raise RuntimeError(
            "FFmpeg frame extraction failed.\n\n"
            "FFmpeg return code: "
            f"{process.returncode}\n\n"
            "Diagnostic output:\n"
            f"{diagnostic}\n\n"
            "Complete FFmpeg log:\n"
            f"{log_path}"
        )

    # --------------------------------------------------------
    # Find generated JPEGs.
    #
    # Only files in this temporary folder are considered.
    # --------------------------------------------------------

    temp_files = []

    for filename in os.listdir(
        temp_folder
    ):

        if filename.lower().endswith(
            ".jpg"
        ):

            temp_files.append(
                os.path.join(
                    temp_folder,
                    filename
                )
            )

    temp_files.sort()

    # --------------------------------------------------------
    # Strict JPEG count validation.
    # --------------------------------------------------------

    expected = len(
        selected_frames
    )

    actual = len(
        temp_files
    )

    if actual != expected:

        raise RuntimeError(
            "Extracted JPEG count does not match "
            "the selected source-frame count.\n\n"
            f"Selected source frames: {expected}\n"
            f"JPEG files generated: {actual}\n\n"
            "This means FFmpeg did not produce exactly "
            "one JPEG for every selected source frame.\n\n"
            "Complete FFmpeg log:\n"
            f"{log_path}"
        )

    return (
        temp_files,
        extraction_seconds
    )


# ============================================================
# Frame timestamp
# ============================================================

def calculate_frame_timestamp(
    recording_start,
    frame_pts,
    first_frame_pts
):
    """
    Convert source-frame PTS into absolute recording time.

    Formula:

        recording_start
        +
        frame_pts - first_frame_pts

    This intentionally makes the first selected source frame
    exactly equal to recording_start.

    No FPS-derived timestamp is used.
    """

    offset_seconds = (
        float(frame_pts)
        - float(first_frame_pts)
    )

    return (
        recording_start
        + timedelta(
            seconds=offset_seconds
        )
    )


# ============================================================
# Expected frame count
# ============================================================

def calculate_expected_frame_count(
    duration,
    fps
):
    """
    Approximate number of grid positions.

    This value is for UI information only.

    The actual extraction count is determined from source-frame
    PTS and validated after FFmpeg extraction.
    """

    if duration <= 0:

        return 0

    return (
        int(duration * fps)
        + 1
    )


# ============================================================
# Filename
# ============================================================

def make_frame_filename(
    frame_time,
    frame_number
):
    """
    Filename timestamp uses millisecond precision.
    """

    timestamp = (
        frame_time.strftime(
            "%Y%m%d_%H%M%S_%f"
        )
    )[:-3]

    return (
        f"{timestamp}_"
        f"{frame_number:06d}.jpg"
    )


# ============================================================
# Diagnostics (low-cost, observational only)
# ============================================================

def summarize_jpeg_sizes(sizes):
    """
    Compute simple distribution statistics for the
    low-cost JPEG size diagnostic.

    Diagnostic only. These values are NEVER used for
    frame selection or gating.
    """

    if not sizes:
        return {}

    ordered = sorted(sizes)
    count = len(ordered)

    def percentile(p):

        if count == 1:
            return float(ordered[0])

        k = (count - 1) * p
        f = math.floor(k)
        c = math.ceil(k)

        if f == c:
            return float(ordered[int(k)])

        return (
            ordered[f] * (c - k)
            + ordered[c] * (k - f)
        )

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
    """
    Compute simple distribution statistics for the
    selection-error diagnostic:

        err_ms = (actual_pts - target_pts) * 1000

    Computed purely from in-memory selection data.
    No filesystem access, no decoding.

    Diagnostic only. These values are NEVER used for
    frame selection or gating. No thresholds are applied.
    """

    if not errors_ms:
        return {}

    ordered = sorted(errors_ms)
    count = len(ordered)

    abs_ordered = sorted(
        abs(e) for e in errors_ms
    )

    def percentile(values, p):

        if count == 1:
            return float(values[0])

        k = (count - 1) * p
        f = math.floor(k)
        c = math.ceil(k)

        if f == c:
            return float(values[int(k)])

        return (
            values[f] * (c - k)
            + values[c] * (k - f)
        )

    return {
        "count": count,
        "mean": sum(ordered) / count,
        "median": percentile(ordered, 0.5),
        "min": ordered[0],
        "max": ordered[-1],
        "max_abs": abs_ordered[-1],
        "p95_abs": percentile(abs_ordered, 0.95),
    }


# ============================================================
# EXIF
# ============================================================

def write_frame_exif(
    filepath,
    frame_time,
    video_path
):
    """
    Write EXIF timestamps corresponding exactly to frame_time.
    """

    date_time = (
        frame_time.strftime(
            "%Y:%m:%d %H:%M:%S"
        )
    )

    milliseconds = (
        frame_time.microsecond
        // 1000
    )

    subsec = (
        f"{milliseconds:03d}"
    )

    timezone_offset = (
        frame_time.strftime(
            "%z"
        )
    )

    if timezone_offset:

        timezone_offset = (
            timezone_offset[:3]
            + ":"
            + timezone_offset[3:]
        )

    try:

        exif_dict = piexif.load(
            filepath
        )

    except Exception:

        exif_dict = {
            "0th": {},
            "Exif": {},
            "GPS": {},
            "1st": {},
            "thumbnail": None,
        }

    exif_dict.setdefault(
        "0th",
        {}
    )

    exif_dict.setdefault(
        "Exif",
        {}
    )

    # --------------------------------------------------------
    # Standard EXIF timestamps
    # --------------------------------------------------------

    exif_dict["0th"][
        piexif.ImageIFD.DateTime
    ] = date_time.encode(
        "ascii"
    )

    exif_dict["Exif"][
        piexif.ExifIFD.DateTimeOriginal
    ] = date_time.encode(
        "ascii"
    )

    exif_dict["Exif"][
        piexif.ExifIFD.DateTimeDigitized
    ] = date_time.encode(
        "ascii"
    )

    # --------------------------------------------------------
    # Subseconds
    # --------------------------------------------------------

    exif_dict["Exif"][
        piexif.ExifIFD.SubSecTimeOriginal
    ] = subsec.encode(
        "ascii"
    )

    exif_dict["Exif"][
        piexif.ExifIFD.SubSecTimeDigitized
    ] = subsec.encode(
        "ascii"
    )

    # --------------------------------------------------------
    # Timezone
    # --------------------------------------------------------

    offset_original = getattr(
        piexif.ExifIFD,
        "OffsetTimeOriginal",
        None
    )

    if (
        offset_original is not None
        and timezone_offset
    ):

        exif_dict["Exif"][
            offset_original
        ] = timezone_offset.encode(
            "ascii"
        )

    offset_digitized = getattr(
        piexif.ExifIFD,
        "OffsetTimeDigitized",
        None
    )

    if (
        offset_digitized is not None
        and timezone_offset
    ):

        exif_dict["Exif"][
            offset_digitized
        ] = timezone_offset.encode(
            "ascii"
        )

    # --------------------------------------------------------
    # Software
    # --------------------------------------------------------

    exif_dict["0th"][
        piexif.ImageIFD.Software
    ] = (
        b"iPhone Video Frame Extractor"
    )

    # --------------------------------------------------------
    # Description
    # --------------------------------------------------------

    description = (
        "Extracted from: "
        + os.path.basename(
            video_path
        )
    )

    exif_dict["0th"][
        piexif.ImageIFD.ImageDescription
    ] = description.encode(
        "utf-8",
        errors="replace"
    )

    # --------------------------------------------------------
    # Write EXIF
    # --------------------------------------------------------

    exif_bytes = piexif.dump(
        exif_dict
    )

    piexif.insert(
        exif_bytes,
        filepath
    )


# ============================================================
# Final Windows timestamp batch
# ============================================================

def apply_windows_timestamps_batch(
    timestamp_records,
    progress_callback=None
):

    if not SET_WINDOWS_FILE_TIMESTAMPS:

        return

    if sys.platform != "win32":

        return

    total = len(
        timestamp_records
    )

    for index, (
        filepath,
        frame_time
    ) in enumerate(
        timestamp_records,
        start=1
    ):

        set_windows_file_timestamps(
            filepath,

            created_time=(
                frame_time
                if SET_WINDOWS_CREATED_TIME
                else None
            ),

            modified_time=(
                frame_time
                if SET_WINDOWS_MODIFIED_TIME
                else None
            ),

            accessed_time=(
                frame_time
                if SET_WINDOWS_ACCESSED_TIME
                else None
            )
        )

        if progress_callback:

            progress_callback(
                index,
                total,
                frame_time
            )


# ============================================================
# Temporary folder cleanup
# ============================================================

def cleanup_temp_folder(
    temp_folder
):
    """
    Best-effort cleanup.
    """

    if not os.path.isdir(
        temp_folder
    ):

        return

    try:

        for filename in os.listdir(
            temp_folder
        ):

            filepath = os.path.join(
                temp_folder,
                filename
            )

            if os.path.isfile(
                filepath
            ):

                try:

                    os.remove(
                        filepath
                    )

                except OSError:

                    pass

        try:

            os.rmdir(
                temp_folder
            )

        except OSError:

            pass

    except OSError:

        pass


# ============================================================
# Main extraction
# ============================================================

def extract_frames(
    video_path,
    output_folder,
    fps,
    progress_callback=None
):

    total_start = time.perf_counter()

    if fps <= 0:

        raise ValueError(
            "FPS must be greater than zero."
        )

    os.makedirs(
        output_folder,
        exist_ok=True
    )

    # --------------------------------------------------------
    # Video metadata
    # --------------------------------------------------------

    metadata_start = time.perf_counter()

    info = get_video_info(
        video_path
    )

    metadata_seconds = (
        time.perf_counter()
        - metadata_start
    )

    duration = info[
        "duration"
    ]

    recording_start = info[
        "recording_start"
    ]

    # --------------------------------------------------------
    # Read ACTUAL source-frame PTS.
    # --------------------------------------------------------

    pts_start = time.perf_counter()

    source_pts = (
        get_source_frame_pts(
            video_path
        )
    )

    pts_seconds = (
        time.perf_counter()
        - pts_start
    )

    # --------------------------------------------------------
    # Select frames against FIXED sampling grid.
    # --------------------------------------------------------

    selection_start = time.perf_counter()

    selected_frames = (
        select_source_frames(
            source_pts,
            fps
        )
    )

    selection_seconds = (
        time.perf_counter()
        - selection_start
    )

    if not selected_frames:

        raise RuntimeError(
            "No frames were selected."
        )

    # --------------------------------------------------------
    # PATCH: selection-error diagnostic records.
    #
    # Purely in-memory. Computed from values that already
    # exist in selected_frames.
    #
    # Diagnostic only. NOT used for frame selection.
    # --------------------------------------------------------

    selection_error_records = []

    for item in selected_frames:

        err_ms = (
            float(item["actual_pts"])
            - float(item["target_pts"])
        ) * 1000.0

        selection_error_records.append(
            (
                item["output_number"],
                float(item["target_pts"]),
                float(item["actual_pts"]),
                err_ms,
            )
        )

    selection_error_stats = summarize_selection_errors(
        [r[3] for r in selection_error_records]
    )

    # --------------------------------------------------------
    # Temporary folder
    # --------------------------------------------------------

    temp_folder = os.path.join(
        output_folder,
        "_temp_frames"
    )

    os.makedirs(
        temp_folder,
        exist_ok=True
    )

    cleanup_temp_folder(
        temp_folder
    )

    os.makedirs(
        temp_folder,
        exist_ok=True
    )

    temp_pattern = os.path.join(
        temp_folder,
        "frame_%08d.jpg"
    )

    try:

        # ----------------------------------------------------
        # Extract exactly the selected source frames.
        #
        # No showinfo.
        #
        # FFprobe is the authoritative source of PTS.
        # ----------------------------------------------------

        (
            temp_files,
            ffmpeg_seconds
        ) = extract_selected_frames(
            video_path,
            temp_pattern,
            selected_frames
        )

        # ----------------------------------------------------
        # First source-frame PTS.
        #
        # This is the PTS of the FIRST SELECTED source frame.
        # ----------------------------------------------------

        first_frame_pts = (
            selected_frames[0][
                "actual_pts"
            ]
        )

        timestamp_records = []

        # Low-cost JPEG size diagnostic records
        jpeg_records = []

        total = len(
            temp_files
        )

        # ----------------------------------------------------
        # Rename + EXIF
        # ----------------------------------------------------

        exif_start = time.perf_counter()

        for index in range(
            total
        ):

            frame_number = (
                index + 1
            )

            # ------------------------------------------------
            # AUTHORITATIVE source PTS.
            #
            # Directly from FFprobe.
            #
            # NOT:
            #
            #   frame_number / FPS
            #
            # NOT:
            #
            #   FFmpeg showinfo
            # ------------------------------------------------

            actual_frame_pts = (
                selected_frames[index][
                    "actual_pts"
                ]
            )

            # ------------------------------------------------
            # Calculate absolute recording timestamp.
            # ------------------------------------------------

            frame_time = (
                calculate_frame_timestamp(
                    recording_start,
                    actual_frame_pts,
                    first_frame_pts
                )
            )

            # ------------------------------------------------
            # Filename and EXIF use exactly the same
            # frame_time.
            # ------------------------------------------------

            output_filename = (
                make_frame_filename(
                    frame_time,
                    frame_number
                )
            )

            output_file = os.path.join(
                output_folder,
                output_filename
            )

            temp_file = (
                temp_files[index]
            )

            if os.path.exists(
                output_file
            ):

                os.remove(
                    output_file
                )

            os.replace(
                temp_file,
                output_file
            )

            # ------------------------------------------------
            # Low-cost diagnostic.
            #
            # Raw JPEG payload size, measured BEFORE EXIF
            # injection so the value reflects only the
            # encoded image content.
            #
            # Diagnostic only. NOT used for selection.
            # ------------------------------------------------

            jpeg_bytes = os.path.getsize(
                output_file
            )

            jpeg_records.append(
                (
                    frame_number,
                    output_filename,
                    jpeg_bytes,
                )
            )

            write_frame_exif(
                output_file,
                frame_time,
                video_path
            )

            if SET_WINDOWS_FILE_TIMESTAMPS:

                timestamp_records.append(
                    (
                        output_file,
                        frame_time
                    )
                )

            if progress_callback:

                progress_callback(
                    frame_number,
                    total,
                    frame_time,
                    "extracting"
                )

        exif_seconds = (
            time.perf_counter()
            - exif_start
        )

        # ----------------------------------------------------
        # JPEG size distribution statistics.
        # ----------------------------------------------------

        jpeg_stats = summarize_jpeg_sizes(
            [r[2] for r in jpeg_records]
        )

        # ----------------------------------------------------
        # Temporary folder should now be empty.
        # ----------------------------------------------------

        cleanup_temp_folder(
            temp_folder
        )

        # ----------------------------------------------------
        # Optional final Windows filesystem timestamps.
        # ----------------------------------------------------

        filesystem_start = time.perf_counter()

        if SET_WINDOWS_FILE_TIMESTAMPS:

            def filesystem_progress(
                current,
                total,
                frame_time
            ):

                if progress_callback:

                    progress_callback(
                        current,
                        total,
                        frame_time,
                        "filesystem"
                    )

            apply_windows_timestamps_batch(
                timestamp_records,
                filesystem_progress
            )

        filesystem_seconds = (
            time.perf_counter()
            - filesystem_start
        )

        # ----------------------------------------------------
        # Total benchmark
        # ----------------------------------------------------

        total_seconds = (
            time.perf_counter()
            - total_start
        )

        # ----------------------------------------------------
        # Persistent benchmark log
        #
        # This is deliberately separate from the FFmpeg command
        # diagnostics, which remain in ffmpeg_last_run.log.
        # ----------------------------------------------------

        benchmark_path = os.path.join(
            output_folder,
            "extraction_benchmark.log"
        )

        with open(
            benchmark_path,
            "w",
            encoding="utf-8",
            errors="replace"
        ) as handle:

            handle.write(
                "VIDEO FRAME EXTRACTION BENCHMARK\n"
            )

            handle.write(
                "================================\n\n"
            )

            handle.write(
                f"Video: {video_path}\n"
            )

            handle.write(
                f"Output folder: {output_folder}\n"
            )

            handle.write(
                f"User FPS: {fps:g}\n"
            )

            handle.write(
                f"Source PTS count: "
                f"{len(source_pts)}\n"
            )

            handle.write(
                f"Selected frame count: "
                f"{len(selected_frames)}\n"
            )

            handle.write(
                f"Extracted JPEG count: "
                f"{len(temp_files)}\n\n"
            )

            handle.write(
                "TIMINGS\n"
            )

            handle.write(
                "-------\n"
            )

            handle.write(
                f"Video metadata: "
                f"{metadata_seconds:.6f} s\n"
            )

            handle.write(
                f"FFprobe source-frame PTS: "
                f"{pts_seconds:.6f} s\n"
            )

            handle.write(
                f"Frame selection: "
                f"{selection_seconds:.6f} s\n"
            )

            handle.write(
                f"FFmpeg extraction: "
                f"{ffmpeg_seconds:.6f} s\n"
            )

            handle.write(
                f"Python EXIF + rename: "
                f"{exif_seconds:.6f} s\n"
            )

            handle.write(
                f"Windows filesystem timestamps: "
                f"{filesystem_seconds:.6f} s\n"
            )

            handle.write(
                f"TOTAL: "
                f"{total_seconds:.6f} s\n"
            )

            handle.write("\n")

            handle.write(
                "SELECTION ARCHITECTURE\n"
            )

            handle.write(
                "----------------------\n"
            )

            handle.write(
                "Sampling: fixed target PTS grid\n"
            )

            handle.write(
                "Source frame selection: nearest source PTS\n"
            )

            handle.write(
                "FFmpeg selection: Balanced BST expression tree\n"
            )

            handle.write(
                "FFmpeg showinfo: NOT USED\n"
            )

            handle.write(
                "FFprobe source PTS: AUTHORITATIVE FOR "
                "TIMESTAMP CALCULATION (Packet-based)\n"
            )

            handle.write(
                "Generic fps filter replacement: NOT USED\n"
            )

            handle.write(
                "ExifTool: NOT USED\n"
            )

            handle.write(
                "piexif: CURRENT IMPLEMENTATION\n"
            )

            handle.write(
                "JPEG size diagnostics: LOGGED "
                "(diagnostic only, NOT used for selection)\n"
            )

            handle.write(
                "Selection error diagnostics: LOGGED "
                "(diagnostic only, NOT used for selection)\n"
            )

            handle.write("\n")

            # ------------------------------------------------
            # SELECTION ERROR DIAGNOSTICS (summary).
            # ------------------------------------------------

            handle.write(
                "SELECTION ERROR DIAGNOSTICS\n"
            )

            handle.write(
                "---------------------------\n"
            )

            handle.write(
                "err_ms = (actual_pts - target_pts) * 1000\n"
            )

            handle.write(
                "Diagnostic only. NOT used for frame selection.\n"
            )

            handle.write(
                "Interior bound: half the LOCAL source interval\n"
            )

            handle.write(
                "(e.g. +/-16.7 ms for 30 fps stretches).\n"
            )

            handle.write(
                "Head/tail frames may legitimately exceed it\n"
            )

            handle.write(
                "(no earlier/later source frame exists).\n\n"
            )

            if selection_error_stats:

                handle.write(
                    f"Frames:        "
                    f"{selection_error_stats['count']}\n"
                )

                handle.write(
                    f"Mean:          "
                    f"{selection_error_stats['mean']:+.3f} ms\n"
                )

                handle.write(
                    f"Median:        "
                    f"{selection_error_stats['median']:+.3f} ms\n"
                )

                handle.write(
                    f"Min (signed):  "
                    f"{selection_error_stats['min']:+.3f} ms\n"
                )

                handle.write(
                    f"Max (signed):  "
                    f"{selection_error_stats['max']:+.3f} ms\n"
                )

                handle.write(
                    f"Max |err|:     "
                    f"{selection_error_stats['max_abs']:.3f} ms\n"
                )

                handle.write(
                    f"P95 |err|:     "
                    f"{selection_error_stats['p95_abs']:.3f} ms\n"
                )

            else:

                handle.write(
                    "No selection error records.\n"
                )

            handle.write("\n")

            # ------------------------------------------------
            # JPEG SIZE DIAGNOSTICS (summary).
            # ------------------------------------------------

            handle.write(
                "JPEG SIZE DIAGNOSTICS\n"
            )

            handle.write(
                "---------------------\n"
            )

            handle.write(
                "Raw JPEG payload measured before EXIF injection.\n"
            )

            handle.write(
                "Diagnostic only. NOT used for frame selection.\n\n"
            )

            if jpeg_stats:

                handle.write(
                    f"Frames:  {jpeg_stats['count']}\n"
                )

                handle.write(
                    f"Min:     {jpeg_stats['min']} bytes\n"
                )

                handle.write(
                    f"Max:     {jpeg_stats['max']} bytes\n"
                )

                handle.write(
                    f"Mean:    {jpeg_stats['mean']:.1f} bytes\n"
                )

                handle.write(
                    f"Median:  {jpeg_stats['median']:.1f} bytes\n"
                )

                handle.write(
                    f"P05:     {jpeg_stats['p05']:.1f} bytes\n"
                )

                handle.write(
                    f"P95:     {jpeg_stats['p95']:.1f} bytes\n"
                )

            else:

                handle.write(
                    "No JPEG size records.\n"
                )

            handle.write("\n")

            # ------------------------------------------------
            # Combined PER-FRAME DIAGNOSTICS table.
            # ------------------------------------------------

            handle.write(
                "PER-FRAME DIAGNOSTICS\n"
            )

            handle.write(
                "---------------------\n"
            )

            handle.write(
                "output  target_pts    actual_pts    "
                "err_ms     jpeg_bytes  filename\n"
            )

            for pos in range(
                len(jpeg_records)
            ):

                (
                    rec_number,
                    rec_filename,
                    rec_bytes
                ) = jpeg_records[pos]

                (
                    _err_number,
                    rec_target,
                    rec_actual,
                    rec_err
                ) = selection_error_records[pos]

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
            "count": total,

            "duration": duration,

            "recording_start": (
                recording_start
            ),

            "source_fps": (
                info["source_fps"]
            ),

            "width": (
                info["width"]
            ),

            "height": (
                info["height"]
            ),

            "codec": (
                info["codec"]
            ),

            "first_frame_pts": (
                first_frame_pts
            ),

            "selected_frames": (
                selected_frames
            ),

            # Benchmark information
            "benchmark": {
                "metadata_seconds": (
                    metadata_seconds
                ),
                "pts_seconds": (
                    pts_seconds
                ),
                "selection_seconds": (
                    selection_seconds
                ),
                "ffmpeg_seconds": (
                    ffmpeg_seconds
                ),
                "exif_seconds": (
                    exif_seconds
                ),
                "filesystem_seconds": (
                    filesystem_seconds
                ),
                "total_seconds": (
                    total_seconds
                ),
            },

            "benchmark_path": (
                benchmark_path
            ),

            # Low-cost JPEG size diagnostic
            "jpeg_diagnostics": {
                "stats": jpeg_stats,
                "records": jpeg_records,
            },

            # Selection-error diagnostic (in-memory)
            "selection_error_diagnostics": {
                "stats": selection_error_stats,
                "records": selection_error_records,
            },
        }

    except Exception:

        cleanup_temp_folder(
            temp_folder
        )

        raise


# ============================================================
# Tkinter application
# ============================================================

class VideoFrameExtractorApp:

    def __init__(self, root):

        self.root = root

        self.root.title(
            "iPhone Video Frame Extractor"
        )

        self.root.geometry(
            "760x560"
        )

        self.root.minsize(
            700,
            500
        )

        self.video_path = None
        self.video_info = None

        self.create_widgets()

    # --------------------------------------------------------
    # UI
    # --------------------------------------------------------

    def create_widgets(self):

        main = ttk.Frame(
            self.root,
            padding=15
        )

        main.pack(
            fill="both",
            expand=True
        )

        # ----------------------------------------------------
        # Source video
        # ----------------------------------------------------

        ttk.Label(
            main,
            text="Source video:"
        ).grid(
            row=0,
            column=0,
            sticky="w",
            pady=(0, 5)
        )

        self.video_var = (
            tk.StringVar()
        )

        video_entry = ttk.Entry(
            main,
            textvariable=self.video_var
        )

        video_entry.grid(
            row=1,
            column=0,
            sticky="ew",
            padx=(0, 8)
        )

        ttk.Button(
            main,
            text="Browse...",
            command=self.select_video
        ).grid(
            row=1,
            column=1,
            sticky="ew"
        )

        # ----------------------------------------------------
        # Output folder
        # ----------------------------------------------------

        ttk.Label(
            main,
            text="Output folder:"
        ).grid(
            row=2,
            column=0,
            sticky="w",
            pady=(15, 5)
        )

        self.output_var = (
            tk.StringVar()
        )

        output_entry = ttk.Entry(
            main,
            textvariable=self.output_var
        )

        output_entry.grid(
            row=3,
            column=0,
            sticky="ew",
            padx=(0, 8)
        )

        ttk.Button(
            main,
            text="Browse...",
            command=self.select_output
        ).grid(
            row=3,
            column=1,
            sticky="ew"
        )

        # ----------------------------------------------------
        # FPS
        # ----------------------------------------------------

        ttk.Label(
            main,
            text="Extraction FPS:"
        ).grid(
            row=4,
            column=0,
            sticky="w",
            pady=(15, 5)
        )

        self.fps_var = (
            tk.StringVar(
                value=str(
                    DEFAULT_FPS
                )
            )
        )

        fps_entry = ttk.Entry(
            main,
            textvariable=self.fps_var,
            width=15
        )

        fps_entry.grid(
            row=5,
            column=0,
            sticky="w"
        )

        # ----------------------------------------------------
        # Metadata
        # ----------------------------------------------------

        ttk.Label(
            main,
            text="Video metadata:"
        ).grid(
            row=6,
            column=0,
            sticky="w",
            pady=(15, 5)
        )

        self.metadata_text = tk.Text(
            main,
            height=12,
            width=80,
            wrap="word"
        )

        self.metadata_text.grid(
            row=7,
            column=0,
            columnspan=2,
            sticky="nsew"
        )

        scrollbar = ttk.Scrollbar(
            main,
            orient="vertical",
            command=self.metadata_text.yview
        )

        scrollbar.grid(
            row=7,
            column=2,
            sticky="ns"
        )

        self.metadata_text.configure(
            yscrollcommand=scrollbar.set
        )

        ttk.Button(
            main,
            text="Read Video Metadata",
            command=self.read_metadata
        ).grid(
            row=8,
            column=0,
            sticky="w",
            pady=(12, 5)
        )

        # ----------------------------------------------------
        # Progress
        # ----------------------------------------------------

        self.progress_var = (
            tk.DoubleVar(
                value=0
            )
        )

        self.progress = ttk.Progressbar(
            main,
            variable=self.progress_var,
            maximum=100
        )

        self.progress.grid(
            row=9,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(15, 5)
        )

        self.status_var = (
            tk.StringVar(
                value="Ready."
            )
        )

        ttk.Label(
            main,
            textvariable=self.status_var
        ).grid(
            row=10,
            column=0,
            columnspan=2,
            sticky="w"
        )

        # ----------------------------------------------------
        # Extract
        # ----------------------------------------------------

        self.extract_button = (
            ttk.Button(
                main,
                text="Extract Frames",
                command=self.start_extraction
            )
        )

        self.extract_button.grid(
            row=11,
            column=0,
            columnspan=2,
            pady=(15, 0)
        )

        main.columnconfigure(
            0,
            weight=1
        )

        main.rowconfigure(
            7,
            weight=1
        )

    # --------------------------------------------------------
    # Select video
    # --------------------------------------------------------

    def select_video(self):

        filepath = (
            filedialog.askopenfilename(
                title="Select video",
                filetypes=[
                    (
                        "Video files",
                        "*.mov *.mp4 *.m4v *.avi *.mkv"
                    ),
                    (
                        "All files",
                        "*.*"
                    ),
                ]
            )
        )

        if not filepath:

            return

        self.video_path = filepath

        self.video_var.set(
            filepath
        )

        base = os.path.splitext(
            os.path.basename(
                filepath
            )
        )[0]

        parent = os.path.dirname(
            filepath
        )

        output_folder = os.path.join(
            parent,
            base + "_frames"
        )

        self.output_var.set(
            output_folder
        )

        self.read_metadata()

    # --------------------------------------------------------
    # Select output
    # --------------------------------------------------------

    def select_output(self):

        folder = (
            filedialog.askdirectory(
                title="Select output folder"
            )
        )

        if folder:

            self.output_var.set(
                folder
            )

    # --------------------------------------------------------
    # Read metadata
    # --------------------------------------------------------

    def read_metadata(self):

        video_path = (
            self.video_var
            .get()
            .strip()
        )

        if not video_path:

            messagebox.showwarning(
                "No video",
                "Please select a video first."
            )

            return

        if not os.path.isfile(
            video_path
        ):

            messagebox.showerror(
                "File not found",
                "The selected video file does not exist."
            )

            return

        try:

            info = get_video_info(
                video_path
            )

            self.video_info = info

            recording_start = (
                info["recording_start"]
            )

            recording_start_text = (
                recording_start
                .strftime(
                    "%Y-%m-%d %H:%M:%S.%f"
                )
            )[:-3]

            duration = info[
                "duration"
            ]

            source_fps = info[
                "source_fps"
            ]

            width = info[
                "width"
            ]

            height = info[
                "height"
            ]

            codec = info[
                "codec"
            ]

            start_time = info[
                "start_time"
            ]

            format_start_time = info[
                "format_start_time"
            ]

            extraction_fps = float(
                self.fps_var.get()
            )

            if extraction_fps <= 0:

                raise ValueError(
                    "FPS must be greater than zero."
                )

            expected_count = (
                calculate_expected_frame_count(
                    duration,
                    extraction_fps
                )
            )

            interval = (
                1.0 / extraction_fps
            )

            metadata = []

            metadata.append(
                "Recording start: "
                + recording_start_text
            )

            metadata.append(
                "Timezone: "
                + (
                    recording_start
                    .strftime("%z")
                    or "unknown"
                )
            )

            metadata.append(
                f"Duration: {duration:.3f} s"
            )

            if source_fps:

                metadata.append(
                    f"Source FPS: "
                    f"{source_fps:.6f}"
                )

            metadata.append(
                f"Resolution: "
                f"{width} x {height}"
            )

            metadata.append(
                f"Codec: {codec}"
            )

            metadata.append(
                f"Video stream start_time: "
                f"{start_time:.6f} s"
            )

            metadata.append(
                f"Container start_time: "
                f"{format_start_time:.6f} s"
            )

            metadata.append("")

            metadata.append(
                f"Selected FPS: "
                f"{extraction_fps:g}"
            )

            metadata.append(
                f"Sampling interval: "
                f"{interval:.6f} s"
            )

            metadata.append(
                f"Approximate grid positions: "
                f"{expected_count}"
            )

            metadata.append("")

            metadata.append(
                "Timestamp architecture:"
            )

            metadata.append(
                "  iPhone Creation Date"
            )

            metadata.append(
                "        ↓"
            )

            metadata.append(
                "  original source-frame PTS"
            )

            metadata.append(
                "        ↓"
            )

            metadata.append(
                "  fixed sampling grid"
            )

            metadata.append(
                "        ↓"
            )

            metadata.append(
                "  nearest source frame"
            )

            metadata.append(
                "        ↓"
            )

            metadata.append(
                "  selected source-frame PTS"
            )

            metadata.append(
                "        ↓"
            )

            metadata.append(
                "  frame_time"
            )

            metadata.append(
                "     ↙     ↓     ↘"
            )

            metadata.append(
                " filename  EXIF  Windows timestamps"
            )

            metadata.append("")

            metadata.append(
                "FFmpeg showinfo:"
            )

            metadata.append(
                "  NOT used"
            )

            metadata.append(
                "FFprobe source PTS: authoritative for "
                "timestamp calculation (Packet-based)"
            )

            metadata.append("")

            metadata.append(
                "Frame-selection optimization:"
            )

            metadata.append(
                "  Current method: "
                "Balanced BST decision tree"
            )

            metadata.append(
                "  Status: Optimized for O(log S) evaluation"
            )

            metadata.append(
                "  Alternative fps filter: NOT used"
            )

            metadata.append("")

            metadata.append(
                "EXIF:"
            )

            metadata.append(
                "  Current implementation: piexif"
            )

            metadata.append(
                "  ExifTool: NOT used"
            )

            metadata.append("")

            metadata.append(
                "Diagnostics (observational only):"
            )

            metadata.append(
                "  JPEG size: LOGGED, NOT used for selection"
            )

            metadata.append(
                "  Selection error: LOGGED, NOT used for selection"
            )

            metadata.append("")

            metadata.append(
                "Windows filesystem timestamp mode: "
                + (
                    "FINAL BATCH PASS"
                    if SET_WINDOWS_FILE_TIMESTAMPS
                    else "DISABLED"
                )
            )

            self.metadata_text.delete(
                "1.0",
                "end"
            )

            self.metadata_text.insert(
                "1.0",
                "\n".join(
                    metadata
                )
            )

            self.status_var.set(
                "Metadata loaded."
            )

        except Exception as exc:

            messagebox.showerror(
                "Metadata error",
                str(exc)
            )

            self.status_var.set(
                "Metadata read failed."
            )

    # --------------------------------------------------------
    # Start extraction
    # --------------------------------------------------------

    def start_extraction(self):

        video_path = (
            self.video_var
            .get()
            .strip()
        )

        output_folder = (
            self.output_var
            .get()
            .strip()
        )

        if not video_path:

            messagebox.showwarning(
                "No video",
                "Please select a video."
            )

            return

        if not os.path.isfile(
            video_path
        ):

            messagebox.showerror(
                "File not found",
                "The selected video file does not exist."
            )

            return

        if not output_folder:

            messagebox.showwarning(
                "No output folder",
                "Please select an output folder."
            )

            return

        try:

            fps = float(
                self.fps_var.get()
            )

            if fps <= 0:

                raise ValueError

        except ValueError:

            messagebox.showerror(
                "Invalid FPS",
                "FPS must be greater than zero."
            )

            return

        try:

            info = get_video_info(
                video_path
            )

            self.video_info = info

        except Exception as exc:

            messagebox.showerror(
                "Video metadata error",
                str(exc)
            )

            return

        recording_start = (
            info["recording_start"]
        )

        expected_count = (
            calculate_expected_frame_count(
                info["duration"],
                fps
            )
        )

        interval = (
            1.0 / fps
        )

        recording_start_text = (
            recording_start
            .strftime(
                "%Y-%m-%d %H:%M:%S.%f"
            )
        )[:-3]

        if SET_WINDOWS_FILE_TIMESTAMPS:

            filesystem_mode = (
                "\n\nWindows filesystem timestamps "
                "will be updated only AFTER all "
                "frames have been extracted."
            )

        else:

            filesystem_mode = (
                "\n\nWindows filesystem timestamps "
                "will NOT be modified."
            )

        confirmation = (
            "Extraction settings:\n\n"

            f"FPS: {fps:g}\n"

            f"Sampling interval: "
            f"{interval:.6f} s\n"

            f"Approximate grid positions: "
            f"{expected_count}\n\n"

            "Recording start:\n"
            f"{recording_start_text}"

            f"{filesystem_mode}\n\n"

            "Timestamp source:\n"
            "original FFprobe source-frame PTS.\n\n"

            "The sampling grid is fixed and does not "
            "accumulate PTS rounding/drift.\n\n"

            "FFmpeg showinfo is not used for timestamp "
            "calculation.\n\n"

            "The FFmpeg frame-selection expression "
            "uses an optimized Balanced BST decision tree "
            "to reduce evaluation overhead.\n\n"

            "JPEG byte sizes will be logged as a "
            "low-cost diagnostic (not used for "
            "selection).\n\n"

            "Selection timing errors (actual PTS minus "
            "target PTS) will be logged as a diagnostic "
            "(not used for selection).\n\n"

            "A benchmark log will be written after "
            "extraction."
        )

        proceed = messagebox.askyesno(
            "Start extraction",
            confirmation
        )

        if not proceed:

            return

        self.extract_button.configure(
            state="disabled"
        )

        self.progress_var.set(
            0
        )

        self.status_var.set(
            "Reading source frame PTS..."
        )

        self.root.update_idletasks()

        try:

            def progress_callback(
                current,
                total,
                frame_time,
                stage
            ):

                if total <= 0:

                    percentage = 0

                else:

                    percentage = (
                        current
                        / total
                        * 100
                    )

                self.progress_var.set(
                    percentage
                )

                timestamp_text = (
                    frame_time
                    .strftime(
                        "%Y-%m-%d %H:%M:%S.%f"
                    )
                )[:-3]

                if stage == "filesystem":

                    status = (
                        "Finalizing Windows "
                        "timestamps: "
                        f"{current}/{total}   "
                        f"{timestamp_text}"
                    )

                else:

                    status = (
                        f"Frame "
                        f"{current}/{total}   "
                        f"{timestamp_text}"
                    )

                self.status_var.set(
                    status
                )

                self.root.update_idletasks()

            result = extract_frames(
                video_path,
                output_folder,
                fps,
                progress_callback
            )

            self.progress_var.set(
                100
            )

            self.status_var.set(
                "Extraction completed."
            )

            recording_start_text = (
                result["recording_start"]
                .strftime(
                    "%Y-%m-%d %H:%M:%S.%f"
                )
            )[:-3]

            benchmark = result[
                "benchmark"
            ]

            # ------------------------------------------------
            # JPEG size diagnostic summary line.
            # ------------------------------------------------

            jpeg_stats = (
                result["jpeg_diagnostics"]["stats"]
            )

            if jpeg_stats:

                jpeg_line = (
                    "JPEG size (diagnostic, pre-EXIF):\n"
                    f"  mean   {jpeg_stats['mean']/1024:.1f} KiB   "
                    f"median {jpeg_stats['median']/1024:.1f} KiB\n"
                    f"  p05    {jpeg_stats['p05']/1024:.1f} KiB   "
                    f"p95    {jpeg_stats['p95']/1024:.1f} KiB\n\n"
                )

            else:

                jpeg_line = ""

            # ------------------------------------------------
            # Selection error diagnostic summary line.
            # ------------------------------------------------

            sel_stats = (
                result["selection_error_diagnostics"]["stats"]
            )

            if sel_stats:

                sel_line = (
                    "Selection error (actual - target PTS):\n"
                    f"  median {sel_stats['median']:+.1f} ms   "
                    f"max |err| {sel_stats['max_abs']:.1f} ms\n\n"
                )

            else:

                sel_line = ""

            messagebox.showinfo(
                "Extraction complete",
                (
                    f"Frames extracted: "
                    f"{result['count']}\n\n"

                    f"Recording start:\n"
                    f"{recording_start_text}\n\n"

                    f"Output folder:\n"
                    f"{output_folder}\n\n"

                    "Benchmark:\n"
                    f"FFprobe PTS: "
                    f"{benchmark['pts_seconds']:.3f} s\n"
                    f"FFmpeg extraction: "
                    f"{benchmark['ffmpeg_seconds']:.3f} s\n"
                    f"EXIF + rename: "
                    f"{benchmark['exif_seconds']:.3f} s\n"
                    f"Total: "
                    f"{benchmark['total_seconds']:.3f} s\n\n"

                    f"{sel_line}"

                    f"{jpeg_line}"

                    "FFmpeg diagnostic log:\n"
                    f"{os.path.join(output_folder, 'ffmpeg_last_run.log')}\n\n"

                    "Benchmark log:\n"
                    f"{result['benchmark_path']}"
                )
            )

        except Exception as exc:

            self.status_var.set(
                "Extraction failed."
            )

            messagebox.showerror(
                "Extraction error",
                str(exc)
            )

        finally:

            self.extract_button.configure(
                state="normal"
            )


# ============================================================
# Main
# ============================================================

def main():

    root = tk.Tk()

    VideoFrameExtractorApp(
        root
    )

    root.mainloop()


if __name__ == "__main__":

    main()