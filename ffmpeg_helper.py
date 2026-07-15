import logging
import os
import platform
import shutil
import subprocess
from typing import List

logger = logging.getLogger(__name__)


def get_ffmpeg_path() -> str:
    try:
        import static_ffmpeg

        static_ffmpeg.add_paths()
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path:
            return ffmpeg_path
    except ImportError:
        pass

    project_dir = os.path.dirname(os.path.abspath(__file__))
    system = platform.system().lower()
    if system == "windows":
        candidates = [
            os.path.join(project_dir, "ffmpeg", "ffmpeg-master-latest-win64-gpl", "bin", "ffmpeg.exe"),
            os.path.join(project_dir, "ffmpeg", "bin", "ffmpeg.exe"),
            os.path.join(project_dir, "ffmpeg", "ffmpeg.exe"),
        ]
        exe = "ffmpeg.exe"
    else:
        candidates = [
            os.path.join(project_dir, "ffmpeg", "ffmpeg"),
            os.path.join(project_dir, "ffmpeg", "bin", "ffmpeg"),
        ]
        exe = "ffmpeg"

    for path in candidates:
        if os.path.exists(path):
            return path

    ffmpeg_path = shutil.which(exe)
    if ffmpeg_path:
        return ffmpeg_path

    raise FileNotFoundError("FFmpeg not found")


def get_ffprobe_path() -> str:
    ffmpeg_path = get_ffmpeg_path()
    ffprobe_path = ffmpeg_path.replace("ffmpeg", "ffprobe")
    return ffprobe_path if os.path.exists(ffprobe_path) else (shutil.which("ffprobe") or ffprobe_path)


def get_file_size(file_path: str) -> int:
    return os.path.getsize(file_path) if os.path.exists(file_path) else 0


def _merge_segments(segment_files: List[str], output_path: str, args: List[str], delete_segments: bool) -> bool:
    if not segment_files:
        return False

    ffmpeg = get_ffmpeg_path()
    concat_file = output_path + ".txt"
    with open(concat_file, "w", encoding="utf-8") as handle:
        for segment in segment_files:
            handle.write(f"file '{segment.replace(chr(92), '/')}'\n")

    try:
        cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", concat_file] + args + [output_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error("FFmpeg merge error: %s", result.stderr)
            return False
        if delete_segments:
            for path in segment_files:
                if os.path.exists(path):
                    os.remove(path)
        return True
    finally:
        if os.path.exists(concat_file):
            os.remove(concat_file)


def merge_audio_segments(segment_files: List[str], output_path: str, delete_segments: bool = True) -> bool:
    return _merge_segments(segment_files, output_path, ["-c:a", "aac", "-b:a", "128k"], delete_segments)


def merge_video_segments(segment_files: List[str], output_path: str, delete_segments: bool = True) -> bool:
    return _merge_segments(
        segment_files,
        output_path,
        ["-c:v", "libx264", "-preset", "fast", "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"],
        delete_segments,
    )


def extract_audio(video_path: str, audio_path: str) -> bool:
    ffmpeg = get_ffmpeg_path()
    result = subprocess.run(
        [ffmpeg, "-y", "-i", video_path, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", audio_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("FFmpeg audio extract error: %s", result.stderr)
        return False
    return True
