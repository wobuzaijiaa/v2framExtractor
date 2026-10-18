"""
FPS-grid video frame extractor (simple tool).

Scope:
    Generates JPEG images on a regular, user-selected output-FPS
    timeline using FFmpeg's fps filter.

Timestamp semantics:
    Each output image is labelled with an OUTPUT SAMPLING TIME:

        sampling_time = recording_start + (frame_index / fps)

    This is NOT guaranteed to equal the source frame's original PTS.
    For source-frame-accurate PTS timestamps, use the PTS-based
    extractor (baseline).

Logging:
    Every run writes a persistent audit log:

        <output_folder>/fps_extraction_log.log

    The log records parameters, counts (expected/produced/used),
    timings, the FFmpeg command and stderr, and the per-frame
    sampling-time map. It contains no source-PTS analysis: this
    tool deliberately does not claim source-frame accuracy.
"""

import os
import sys
import ctypes
import subprocess
import time
import tkinter as tk

from tkinter import filedialog, messagebox, ttk
from datetime import datetime, timedelta, timezone

import ffmpeg
import piexif


# ============================================================
# Configuration
# ============================================================

DEFAULT_FPS = 2.0

# Persistent per-run audit log written into the output folder.
FPS_LOG_NAME = "fps_extraction_log.log"

# IMPORTANT:
#
# False = safest / lowest filesystem activity.
# Filename + EXIF are still synchronized to sampling_time.
#
# True = after ALL JPEGs have been extracted and EXIF has been
# written, perform ONE final filesystem timestamp pass.
#
# This is deliberately NOT done during frame extraction.
SET_WINDOWS_FILE_TIMESTAMPS = False

# If filesystem timestamps are enabled, control which Windows
# timestamps are modified.
#
# Date accessed is deliberately disabled because it provides
# no useful information for this application.
SET_WINDOWS_CREATED_TIME = True
SET_WINDOWS_MODIFIED_TIME = True
SET_WINDOWS_ACCESSED_TIME = False

JPEG_QUALITY = 2


# ============================================================
# Windows FILETIME support
# ============================================================

def datetime_to_filetime(dt):
    """
    Convert timezone-aware Python datetime to Windows FILETIME.

    FILETIME:
        100-nanosecond intervals since 1601-01-01 UTC.
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

    This function is NOT called during frame extraction.

    It is only used by the optional final batch pass.
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
        kernel32.CloseHandle(handle)


# ============================================================
# Video datetime parsing
# ============================================================

def parse_video_datetime(value):
    """
    Parse a video metadata datetime.

    Examples:

        2026:07:23 11:44:40+02:00
        2026-07-23 11:44:40+02:00
        2026-07-23T11:44:40+02:00
        2026-07-23T09:44:40Z

    If timezone information is present, it is preserved.

    NO manual +1 / +2 hour correction is performed.
    """

    if not value:
        return None

    value = str(value).strip()

    # Convert:
    #
    # 2026:07:23 11:44:40
    #
    # into:
    #
    # 2026-07-23 11:44:40
    #
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
        value = value[:-1] + "+00:00"

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
        2. Generic creation_time/date metadata

    The function NEVER uses:
        - JPEG creation time
        - JPEG modification time
        - Windows file creation time
        - Python execution time
        - FFmpeg extraction time
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

    # --------------------------------------------------------
    # Apple / iPhone creation date.
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Generic fallback.
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Other information
    # --------------------------------------------------------

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
        "recording_start": recording_start,
    }


# ============================================================
# Sampling timestamp
# ============================================================

def calculate_sampling_time(
    recording_start,
    frame_number,
    fps
):
    """
    Calculate the OUTPUT SAMPLING TIME:

        recording_start
        +
        (frame_number - 1) / fps

    Frame 1:
        recording_start

    Frame 2:
        recording_start + 1/fps

    This is a regular output-grid timestamp.

    It is NOT guaranteed to equal the source frame's
    original PTS. For source-frame-accurate timestamps,
    use the PTS-based extractor.
    """

    if fps <= 0:

        raise ValueError(
            "FPS must be greater than zero."
        )

    frame_index = (
        frame_number - 1
    )

    offset_seconds = (
        frame_index / fps
    )

    return (
        recording_start
        + timedelta(
            seconds=offset_seconds
        )
    )


def calculate_expected_frame_count(
    duration,
    fps
):
    """
    Expected number of output-grid positions.

    Informational only. The actual produced count is
    reported separately after extraction.
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
    sampling_time,
    frame_number
):
    """
    Filename is generated from exactly the same
    sampling_time used for EXIF.
    """

    timestamp = (
        sampling_time
        .strftime(
            "%Y%m%d_%H%M%S_%f"
        )
    )[:-3]

    return (
        f"{timestamp}_"
        f"{frame_number:06d}.jpg"
    )


# ============================================================
# EXIF
# ============================================================

def write_frame_exif(
    filepath,
    sampling_time,
    video_path
):
    """
    Write EXIF timestamp from sampling_time.

    sampling_time is an OUTPUT SAMPLING TIME:

        recording_start + frame_index / fps

    It is NOT the source frame's original PTS, and it is
    NOT derived from the JPEG's creation time.
    """

    date_time = (
        sampling_time
        .strftime(
            "%Y:%m:%d %H:%M:%S"
        )
    )

    milliseconds = (
        sampling_time.microsecond
        // 1000
    )

    subsec = (
        f"{milliseconds:03d}"
    )

    timezone_offset = (
        sampling_time.strftime(
            "%z"
        )
    )

    if timezone_offset:

        timezone_offset = (
            timezone_offset[:3]
            + ":"
            + timezone_offset[3:]
        )

    # --------------------------------------------------------
    # Existing EXIF.
    # --------------------------------------------------------

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
    # Standard EXIF timestamp.
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
    # Milliseconds.
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
    # Timezone.
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
    # Software.
    # --------------------------------------------------------

    exif_dict["0th"][
        piexif.ImageIFD.Software
    ] = (
        b"iPhone Video Frame Extractor"
    )

    # --------------------------------------------------------
    # Source video.
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
    # Write.
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
    """
    Apply Windows filesystem timestamps ONLY AFTER all frame
    extraction and EXIF processing is complete.

    timestamp_records:

        [
            (filepath, sampling_time),
            ...
        ]

    This avoids repeatedly modifying filesystem metadata while
    FFmpeg is still generating files.
    """

    if not SET_WINDOWS_FILE_TIMESTAMPS:
        return

    if sys.platform != "win32":
        return

    total = len(
        timestamp_records
    )

    for index, (
        filepath,
        sampling_time
    ) in enumerate(
        timestamp_records,
        start=1
    ):

        set_windows_file_timestamps(
            filepath,
            created_time=(
                sampling_time
                if SET_WINDOWS_CREATED_TIME
                else None
            ),
            modified_time=(
                sampling_time
                if SET_WINDOWS_MODIFIED_TIME
                else None
            ),
            accessed_time=(
                sampling_time
                if SET_WINDOWS_ACCESSED_TIME
                else None
            )
        )

        if progress_callback:

            progress_callback(
                index,
                total,
                sampling_time
            )


# ============================================================
# Extraction
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
    # Run log machinery.
    #
    # The log is written on BOTH success and failure, so a
    # failed run still leaves a complete audit trail.
    # --------------------------------------------------------

    log_lines = []

    def append_log(*items):
        log_lines.extend(items)

    def flush_log():
        log_path = os.path.join(
            output_folder,
            FPS_LOG_NAME
        )
        with open(
            log_path,
            "w",
            encoding="utf-8",
            errors="replace"
        ) as handle:
            handle.write(
                "\n".join(log_lines) + "\n"
            )
        return log_path

    # --------------------------------------------------------
    # Video metadata.
    # --------------------------------------------------------

    info = get_video_info(
        video_path
    )

    duration = info[
        "duration"
    ]

    recording_start = info[
        "recording_start"
    ]

    expected_count = (
        calculate_expected_frame_count(
            duration,
            fps
        )
    )

    # --------------------------------------------------------
    # Log header.
    # --------------------------------------------------------

    append_log(
        "FPS-GRID EXTRACTION LOG"
    )
    append_log(
        "======================="
    )
    append_log("")
    append_log(
        "Timestamp semantics: output sampling time"
    )
    append_log(
        "(frame index / FPS), NOT source-frame PTS."
    )
    append_log("")
    append_log(f"Video: {video_path}")
    append_log(f"Output folder: {output_folder}")
    append_log(f"Selected FPS: {fps:g}")
    append_log(
        f"Frame interval: {1.0 / fps:.6f} s"
    )
    append_log(f"JPEG quality: {JPEG_QUALITY}")
    append_log("")
    append_log(
        "Recording start: "
        + recording_start.strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:-3]
    )
    append_log(
        "Timezone: "
        + (
            recording_start.strftime("%z")
            or "unknown"
        )
    )
    append_log(f"Duration: {duration:.3f} s")
    append_log(
        "Source FPS (nominal): "
        + (
            f"{info['source_fps']:.6f}"
            if info["source_fps"]
            else "unknown"
        )
    )
    append_log(
        f"Resolution: {info['width']} x {info['height']}"
    )
    append_log(f"Codec: {info['codec']}")
    append_log("")

    # --------------------------------------------------------
    # Temporary folder.
    # --------------------------------------------------------

    temp_folder = os.path.join(
        output_folder,
        "_temp_frames"
    )

    os.makedirs(
        temp_folder,
        exist_ok=True
    )

    # Clean old temporary files.
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

    temp_pattern = os.path.join(
        temp_folder,
        "frame_%08d.jpg"
    )

    # --------------------------------------------------------
    # FFmpeg extraction.
    #
    # PATCH: no explicit ss=0; seeking to zero is the default
    # and stating it only clutters the logged command.
    # --------------------------------------------------------

    stream = (
        ffmpeg
        .input(
            video_path
        )
        .filter(
            "fps",
            fps=fps
        )
        .output(
            temp_pattern,
            **{
                "q:v": JPEG_QUALITY
            }
        )
        .overwrite_output()
    )

    try:
        command_args = stream.get_args()
    except Exception:
        command_args = None

    append_log("FFMPEG COMMAND:")

    if command_args is not None:
        append_log(
            subprocess.list2cmdline(
                ["ffmpeg"] + list(command_args)
            )
        )
    else:
        append_log("(command unavailable)")

    append_log("")

    ffmpeg_start = time.perf_counter()

    try:

        _stdout, stderr_bytes = stream.run(
            capture_stdout=True,
            capture_stderr=True
        )

        ffmpeg_stderr = (
            stderr_bytes.decode(
                "utf-8",
                errors="replace"
            )
            if stderr_bytes
            else ""
        )

    except ffmpeg.Error as exc:

        ffmpeg_seconds = (
            time.perf_counter()
            - ffmpeg_start
        )

        ffmpeg_stderr = ""

        if exc.stderr:

            try:
                ffmpeg_stderr = (
                    exc.stderr.decode(
                        "utf-8",
                        errors="replace"
                    )
                )

            except Exception:
                ffmpeg_stderr = str(
                    exc.stderr
                )

        append_log("RESULT: FAILED")
        append_log(
            f"FFmpeg time: {ffmpeg_seconds:.6f} s"
        )
        append_log("")
        append_log("FFMPEG STDERR:")
        append_log(ffmpeg_stderr)

        flush_log()

        raise RuntimeError(
            "FFmpeg extraction failed:\n\n"
            + ffmpeg_stderr
        )

    ffmpeg_seconds = (
        time.perf_counter()
        - ffmpeg_start
    )

    # --------------------------------------------------------
    # Temporary JPEG list.
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

    produced_count = len(
        temp_files
    )

    if not temp_files:

        append_log("RESULT: FAILED")
        append_log(
            f"FFmpeg time: {ffmpeg_seconds:.6f} s"
        )
        append_log("")
        append_log(
            "FFmpeg produced no JPEG frames."
        )
        append_log("")
        append_log("FFMPEG STDERR:")
        append_log(ffmpeg_stderr)

        flush_log()

        raise RuntimeError(
            "FFmpeg did not produce any JPEG frames."
        )

    usable_count = min(
        produced_count,
        expected_count
    )

    # --------------------------------------------------------
    # Records for optional final Windows timestamp pass.
    #
    # Nothing touches Windows filesystem timestamps here.
    # --------------------------------------------------------

    timestamp_records = []

    # Per-frame output map for the log.
    per_frame_records = []

    # --------------------------------------------------------
    # Rename + EXIF.
    # --------------------------------------------------------

    exif_start = time.perf_counter()

    for index in range(
        usable_count
    ):

        frame_number = (
            index + 1
        )

        # ----------------------------------------------------
        # SINGLE AUTHORITATIVE SAMPLING TIMESTAMP
        # ----------------------------------------------------

        sampling_time = (
            calculate_sampling_time(
                recording_start,
                frame_number,
                fps
            )
        )

        # ----------------------------------------------------
        # Filename.
        # ----------------------------------------------------

        output_filename = (
            make_frame_filename(
                sampling_time,
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

        # ----------------------------------------------------
        # Move/rename.
        # ----------------------------------------------------

        os.replace(
            temp_file,
            output_file
        )

        # ----------------------------------------------------
        # EXIF uses EXACTLY the same sampling_time.
        # ----------------------------------------------------

        write_frame_exif(
            output_file,
            sampling_time,
            video_path
        )

        # ----------------------------------------------------
        # Log record.
        # ----------------------------------------------------

        per_frame_records.append(
            (
                frame_number,
                sampling_time.strftime(
                    "%Y-%m-%d %H:%M:%S.%f"
                )[:-3],
                output_filename,
            )
        )

        # ----------------------------------------------------
        # Store for optional final Windows pass.
        # ----------------------------------------------------

        if SET_WINDOWS_FILE_TIMESTAMPS:

            timestamp_records.append(
                (
                    output_file,
                    sampling_time
                )
            )

        # ----------------------------------------------------
        # Normal extraction progress.
        # ----------------------------------------------------

        if progress_callback:

            progress_callback(
                frame_number,
                usable_count,
                sampling_time,
                "extracting"
            )

    exif_seconds = (
        time.perf_counter()
        - exif_start
    )

    # --------------------------------------------------------
    # Clean temporary directory.
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # OPTIONAL FINAL WINDOWS TIMESTAMP PASS
    #
    # This happens AFTER all JPEG extraction and EXIF writes.
    # --------------------------------------------------------

    filesystem_start = time.perf_counter()

    if SET_WINDOWS_FILE_TIMESTAMPS:

        def filesystem_progress(
            current,
            total,
            sampling_time
        ):

            if progress_callback:

                progress_callback(
                    current,
                    total,
                    sampling_time,
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

    total_seconds = (
        time.perf_counter()
        - total_start
    )

    # --------------------------------------------------------
    # Log body (success path).
    # --------------------------------------------------------

    append_log("RESULT: SUCCESS")
    append_log("")
    append_log("COUNTS:")
    append_log(
        f"  Expected output frames: {expected_count}"
    )
    append_log(
        f"  FFmpeg produced:        {produced_count}"
    )
    append_log(
        f"  Used:                   {usable_count}"
    )

    if produced_count != expected_count:

        append_log("")
        append_log(
            "  NOTE: produced != expected."
        )
        append_log(
            "  The expected count is an output-grid estimate."
        )
        append_log(
            "  FFmpeg's actual output count is authoritative for"
        )
        append_log(
            "  the generated files. No source-PTS correction is applied."
        )

    append_log("")
    append_log("TIMINGS:")
    append_log(
        f"  FFmpeg extraction:   {ffmpeg_seconds:.6f} s"
    )
    append_log(
        f"  Rename + EXIF:       {exif_seconds:.6f} s"
    )
    append_log(
        f"  Windows timestamps:  {filesystem_seconds:.6f} s"
    )
    append_log(
        f"  TOTAL:               {total_seconds:.6f} s"
    )
    append_log("")
    append_log(
        "Windows filesystem timestamp mode: "
        + (
            "FINAL BATCH PASS"
            if SET_WINDOWS_FILE_TIMESTAMPS
            else "DISABLED"
        )
    )
    append_log("")
    append_log("FFMPEG STDERR:")
    append_log(ffmpeg_stderr)
    append_log("")
    append_log("PER-FRAME OUTPUT MAP:")
    append_log(
        "frame  sampling_time          filename"
    )

    for (
        rec_number,
        rec_time,
        rec_filename
    ) in per_frame_records:

        append_log(
            f"{rec_number:06d}  {rec_time}  {rec_filename}"
        )

    append_log("")
    append_log(
        "Timestamp semantics: output sampling time,"
    )
    append_log(
        "not source-frame PTS."
    )

    log_path = flush_log()

    return {
        "count": usable_count,
        "expected_count": expected_count,
        "produced_count": produced_count,
        "usable_count": usable_count,
        "duration": duration,
        "recording_start": recording_start,
        "source_fps": info[
            "source_fps"
        ],
        "width": info[
            "width"
        ],
        "height": info[
            "height"
        ],
        "codec": info[
            "codec"
        ],
        "log_path": log_path,
        "benchmark": {
            "ffmpeg_seconds": ffmpeg_seconds,
            "exif_seconds": exif_seconds,
            "filesystem_seconds": filesystem_seconds,
            "total_seconds": total_seconds,
        },
    }


# ============================================================
# Tkinter application
# ============================================================

class VideoFrameExtractorApp:

    def __init__(self, root):

        self.root = root

        self.root.title(
            "iPhone Video Frame Extractor (FPS grid)"
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

        # ----------------------------------------------------
        # Metadata button
        # ----------------------------------------------------

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

            extraction_fps = float(
                self.fps_var.get()
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
                f"Video start_time: "
                f"{start_time:.6f} s"
            )

            metadata.append("")

            metadata.append(
                f"Selected FPS: "
                f"{extraction_fps:g}"
            )

            metadata.append(
                f"Frame interval: "
                f"{interval:.6f} s"
            )

            metadata.append(
                f"Expected output frames: "
                f"{expected_count}"
            )

            metadata.append("")

            metadata.append(
                "Timestamp source:"
            )

            metadata.append(
                "  iPhone Creation Date"
            )

            metadata.append(
                "        ↓"
            )

            metadata.append(
                "  recording_start"
            )

            metadata.append(
                "        ↓"
            )

            metadata.append(
                "  output frame index / selected FPS"
            )

            metadata.append(
                "        ↓"
            )

            metadata.append(
                "  sampling_time"
            )

            metadata.append(
                "     ↙      ↓      ↘"
            )

            metadata.append(
                " filename  EXIF  Windows timestamps"
            )

            metadata.append("")

            metadata.append(
                "Timestamp semantics:"
            )

            metadata.append(
                "  Output sampling time,"
            )

            metadata.append(
                "  not source-frame PTS"
            )

            metadata.append("")

            metadata.append(
                "Scope:"
            )

            metadata.append(
                "  Regular output-FPS timeline extraction."
            )

            metadata.append(
                "  For source-frame-accurate PTS timestamps,"
            )

            metadata.append(
                "  use the PTS-based extractor."
            )

            metadata.append("")

            metadata.append(
                "Run log:"
            )

            metadata.append(
                f"  {FPS_LOG_NAME} in output folder"
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
                "\n".join(metadata)
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

        # ----------------------------------------------------
        # Read video metadata.
        # ----------------------------------------------------

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
            f"Frame interval: "
            f"{interval:.6f} s\n"
            f"Expected output frames: "
            f"{expected_count}\n\n"
            "Recording start:\n"
            f"{recording_start_text}"
            f"{filesystem_mode}\n\n"
            "Timestamp semantics:\n"
            "Output sampling time "
            "(frame index / FPS),\n"
            "not guaranteed source-frame PTS.\n\n"
            "Filename and EXIF will use the same "
            "sampling timestamp.\n\n"
            f"A run log will be written to:\n"
            f"{os.path.join(output_folder, FPS_LOG_NAME)}"
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
            "Extracting..."
        )

        self.root.update_idletasks()

        try:

            def progress_callback(
                current,
                total,
                sampling_time,
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
                    sampling_time
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

            messagebox.showinfo(
                "Extraction complete",
                (
                    f"Used frames: "
                    f"{result['usable_count']}\n\n"

                    f"Expected output frames: "
                    f"{result['expected_count']}\n"
                    f"FFmpeg produced:        "
                    f"{result['produced_count']}\n"
                    f"Used:                   "
                    f"{result['usable_count']}\n\n"

                    "Timestamp semantics:\n"
                    "  Output sampling time,\n"
                    "  not source-frame PTS\n\n"

                    f"Recording start:\n"
                    f"{recording_start_text}\n\n"

                    f"Output folder:\n"
                    f"{output_folder}\n\n"

                    f"Run log:\n"
                    f"{result['log_path']}"
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