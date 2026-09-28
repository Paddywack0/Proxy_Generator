"""
make_proxies.py  -  Step 1: core DNxHR LB proxy maker for DaVinci Resolve

HOW TO RUN (from a Command Prompt):
    python make_proxies.py "F:\\Athena\\RESTOS_26\\Test Media"

WHAT IT DOES:
    1. Looks through the folder (and its subfolders) for video files.
    2. For each clip, makes a DNxHR LB .mov proxy in a "Proxy" subfolder
       next to the original, with the same filename, so Resolve links it
       automatically.
    3. Copies the start timecode (from the file itself, or from Sony's .XML
       sidecar), keeps the frame rate and every audio channel, and shrinks
       anything bigger than 1920x1080.
    4. Checks each finished proxy is the same length as the original.
    5. Skips clips that already have a proxy, so it's safe to re-run.
    6. Writes a log file (proxy_log.txt) in the folder you gave it.
"""

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

# ----------------------------------------------------------------------
# SETTINGS - change these if you need to
# ----------------------------------------------------------------------
FFMPEG = "ffmpeg"      # or a full path, e.g. r"C:\ffmpeg\bin\ffmpeg.exe"
FFPROBE = "ffprobe"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mxf", ".mts", ".m4v", ".avi", ".mkv"}
PROXY_FOLDER_NAME = "Proxy"
MAX_WIDTH, MAX_HEIGHT = 1920, 1080
LENGTH_TOLERANCE_SECONDS = 0.1   # how close proxy and original lengths must be

# On Windows, stop ffmpeg/ffprobe popping up a black console window each time
# (matters when this is run from the drag-and-drop window).
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


# ----------------------------------------------------------------------
# Small helper: write a line to the screen and to the log file
# ----------------------------------------------------------------------
LOG_FILE = None
LOG_HOOK = None   # the window sets this so log lines also appear on screen

def log(message):
    print(message)
    if LOG_HOOK:
        LOG_HOOK(message)
    if LOG_FILE:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}\n")


# ----------------------------------------------------------------------
# Step A: ask ffprobe what's inside a video file
# ----------------------------------------------------------------------
def probe(path):
    """Returns ffprobe's description of the file as a Python dictionary."""
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, creationflags=NO_WINDOW)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe couldn't read the file: {result.stderr.strip()}")
    return json.loads(result.stdout)


# ----------------------------------------------------------------------
# Step B: find the clip's start timecode
# ----------------------------------------------------------------------
def timecode_from_file(info):
    """Many cameras (GoPro, Canon, most .mov files) store timecode as a tag."""
    for stream in info.get("streams", []):
        tc = stream.get("tags", {}).get("timecode")
        if tc:
            return tc
    return info.get("format", {}).get("tags", {}).get("timecode")


def timecode_from_sony_xml(video_path):
    """
    Sony stores timecode in a sidecar file, e.g. C0001M01.XML next to C0001.MP4.
    We look for either  NAME M01.XML  or  NAME.XML  (any capitalisation).
    Inside, <LtcChange value="FFSSMMHH"> holds frames, seconds, minutes, hours
    as pairs of digits (a format called BCD), with a few extra flag bits
    that we mask off.
    """
    folder, name = video_path.parent, video_path.stem.lower()
    wanted = {f"{name}m01.xml", f"{name}.xml"}
    xml_file = next((p for p in folder.iterdir() if p.name.lower() in wanted), None)
    if xml_file is None:
        return None

    try:
        root = ET.parse(xml_file).getroot()
    except ET.ParseError:
        return None

    for element in root.iter():
        if element.tag.endswith("LtcChange") and element.get("frameCount") == "0":
            value = element.get("value", "")
            if len(value) != 8:
                return None

            def bcd(two_hex_digits, mask):
                n = int(two_hex_digits, 16) & mask
                return (n >> 4) * 10 + (n & 0x0F)

            ff = bcd(value[0:2], 0x3F)
            ss = bcd(value[2:4], 0x7F)
            mm = bcd(value[4:6], 0x7F)
            hh = bcd(value[6:8], 0x3F)
            return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"
    return None


# ----------------------------------------------------------------------
# Step C: make one proxy
# ----------------------------------------------------------------------
def make_proxy(source, on_progress=None, stop_now=None):
    """
    on_progress: optional function(fraction_done, fps, speed) for the window.
    stop_now:    optional threading.Event - if it gets set, ffmpeg is halted,
                 the partial file removed, and "stopped" returned.
    """
    proxy_dir = source.parent / PROXY_FOLDER_NAME
    final_path = proxy_dir / (source.stem + ".mov")
    # We write to a temporary name first. If the job is interrupted,
    # the half-finished file never looks like a real proxy.
    temp_path = proxy_dir / (source.stem + ".partial.mov")

    if final_path.exists():
        log(f"SKIP   {source.name} (proxy already exists)")
        return "skipped"

    info = probe(source)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        log(f"SKIP   {source.name} (no video stream)")
        return "skipped"
    audio_count = sum(1 for s in info["streams"] if s["codec_type"] == "audio")

    # Where does the timecode come from?
    tc = timecode_from_file(info)
    tc_source = "file"
    if not tc:
        tc = timecode_from_sony_xml(source)
        tc_source = "Sony XML"
    if not tc:
        tc_source = "none found - proxy starts at 00:00:00:00"

    log(f"START  {source.name}  {video['width']}x{video['height']}  "
        f"{video.get('r_frame_rate')} fps  {audio_count} audio stream(s)  "
        f"TC {tc or '-'} ({tc_source})")

    proxy_dir.mkdir(exist_ok=True)

    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-stats", "-y",
           "-i", str(source),
           "-map", "0:v:0",          # the main picture
           "-map", "0:a?",           # every audio stream (the ? = fine if none)
           "-c:v", "dnxhd", "-profile:v", "dnxhr_lb", "-pix_fmt", "yuv422p",
           "-c:a", "pcm_s24le"]

    # Only shrink if it's bigger than 1920x1080; keep its shape.
    if int(video["width"]) > MAX_WIDTH or int(video["height"]) > MAX_HEIGHT:
        cmd += ["-vf", f"scale={MAX_WIDTH}:{MAX_HEIGHT}:"
                       "force_original_aspect_ratio=decrease:force_divisible_by=2"]

    if tc:
        cmd += ["-timecode", tc]

    cmd.append(str(temp_path))
    source_len = float(info["format"]["duration"])

    started = datetime.now()
    if on_progress is None and stop_now is None:
        # Command-line mode: ffmpeg prints its own progress line.
        returncode = subprocess.run(cmd).returncode
    else:
        returncode = run_with_progress(cmd, source_len, on_progress, stop_now)
        if returncode == "stopped":
            temp_path.unlink(missing_ok=True)
            log(f"STOPPED {source.name} (partial file removed)")
            return "stopped"
    seconds_taken = (datetime.now() - started).total_seconds()

    if returncode != 0:
        temp_path.unlink(missing_ok=True)
        log(f"FAILED {source.name} (ffmpeg error)")
        return "failed"

    # Check: is the proxy the same length as the original?
    proxy_len = float(probe(temp_path)["format"]["duration"])
    if abs(source_len - proxy_len) > LENGTH_TOLERANCE_SECONDS:
        log(f"FAILED {source.name} (length mismatch: original {source_len:.2f}s, "
            f"proxy {proxy_len:.2f}s) - partial file kept for inspection")
        return "failed"

    temp_path.rename(final_path)
    speed = source_len / seconds_taken if seconds_taken else 0
    log(f"DONE   {source.name}  in {seconds_taken:.0f}s ({speed:.2f}x realtime)")
    return "done"


def run_with_progress(cmd, source_len, on_progress, stop_now):
    """
    Runs ffmpeg with '-progress pipe:1', which makes it print simple
    'key=value' lines (e.g. out_time_us=1234567) as it works. We read
    those to work out how far through the clip it is.
    """
    cmd = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
    cmd = [c for c in cmd if c != "-stats"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, creationflags=NO_WINDOW)
    fps = speed = 0.0
    for line in proc.stdout:
        if stop_now is not None and stop_now.is_set():
            proc.terminate()
            proc.wait()
            return "stopped"
        key, _, value = line.strip().partition("=")
        try:
            if key == "fps":
                fps = float(value)
            elif key == "speed":
                speed = float(value.rstrip("x"))
            elif key == "out_time_us" and on_progress and source_len:
                fraction = min(int(value) / 1_000_000 / source_len, 1.0)
                on_progress(fraction, fps, speed)
        except ValueError:
            pass   # ffmpeg sometimes reports "N/A" at the start
    proc.wait()
    if proc.returncode != 0:
        log(f"       ffmpeg said: {proc.stderr.read().strip()[:300]}")
    return proc.returncode


def is_mac_junk(path):
    """macOS leaves hidden '._' files (e.g. ._C0001.MP4) on non-Mac drives."""
    return path.name.startswith("._")


def find_clips(path):
    """A single clip, or every video file inside a folder (skipping Proxy folders
    and Mac '._' files)."""
    path = Path(path)
    if path.is_file():
        return [path] if (path.suffix.lower() in VIDEO_EXTENSIONS
                          and not is_mac_junk(path)) else []
    return sorted(
        p for p in path.rglob("*")
        if p.suffix.lower() in VIDEO_EXTENSIONS
        and not is_mac_junk(p)
        and PROXY_FOLDER_NAME.lower() not in (part.lower() for part in p.parts)
    )


# ----------------------------------------------------------------------
# Main: find all the clips and process them one at a time
# ----------------------------------------------------------------------
def main():
    global LOG_FILE
    if len(sys.argv) != 2:
        print('Usage: python make_proxies.py "path\\to\\shoot folder"')
        sys.exit(1)

    top = Path(sys.argv[1])
    if not top.is_dir():
        print(f"Not a folder: {top}")
        sys.exit(1)

    LOG_FILE = top / "proxy_log.txt"

    clips = find_clips(top)
    log(f"Found {len(clips)} clip(s) in {top}")

    totals = {"done": 0, "skipped": 0, "failed": 0}
    for number, clip in enumerate(clips, start=1):
        print(f"\n[{number}/{len(clips)}]")
        try:
            totals[make_proxy(clip)] += 1
        except Exception as error:   # keep going even if one clip is odd
            log(f"FAILED {clip.name} ({error})")
            totals["failed"] += 1

    log(f"Finished: {totals['done']} made, {totals['skipped']} skipped, "
        f"{totals['failed']} failed")


if __name__ == "__main__":
    main()
