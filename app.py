#!/usr/bin/env python3
"""
Pittsburgh HOV Lane Status Checker
Monitors 511PA traffic camera at Bedford Ave to determine if the HOV lane is open.
Only the Bedford Ave camera is used for AI detection.
MM 5.5 camera is shown for visual reference only.
"""

import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, render_template, Response
import requests
from PIL import Image, ImageEnhance
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

# Force unbuffered output for logging
sys.stdout = io.TextIOWrapper(
    open(sys.stdout.fileno(), 'wb', 0), write_through=True
)

app = Flask(__name__)

# Pittsburgh Eastern time (handles EST/EDT automatically)
EASTERN = ZoneInfo("America/New_York")


def now_eastern():
    """Return current time in Eastern (Pittsburgh) timezone."""
    return datetime.now(EASTERN)

# Camera configuration
CAMERAS = {
    "bedford": {
        "name": "I-579 HOV @ Bedford Ave",
        "image_id": 5967,
        "image_url": "https://www.511pa.com/map/Cctv/5967",
        "video_image_id": 5967,
        "description": "HOV exit/entrance camera"
    },
    "mm55": {
        "name": "I-279 @ MM 5.5 (HOV ON-RAMP)",
        "image_id": 5454,
        "image_url": "https://www.511pa.com/map/Cctv/5454",
        "video_image_id": 5454,
        "description": "HOV exit lane detection"
    },
    "mm12": {
        "name": "I-279 @ MM 1.2 (HOV)",
        "image_id": 5337,
        "image_url": "https://www.511pa.com/map/Cctv/5337",
        "video_image_id": 5337,
        "description": "HOV roadway camera"
    },
    "mm14": {
        "name": "I-279 @ MM 1.4 (HOV)",
        "image_id": 5451,
        "image_url": "https://www.511pa.com/map/Cctv/5451",
        "video_image_id": 5451,
        "description": "HOV roadway camera"
    },
}

# Azure OpenAI configuration
AZURE_OPENAI_ENDPOINT = os.environ.get(
    "AZURE_OPENAI_ENDPOINT", ""
)
AZURE_OPENAI_DEPLOYMENT = os.environ.get(
    "AZURE_OPENAI_DEPLOYMENT", "gpt-4o"
)

# Initialize Azure OpenAI client
_aoai_client = None


def get_openai_client():
    """Lazy-init Azure OpenAI client with Managed Identity."""
    global _aoai_client
    if _aoai_client is None:
        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential,
            "https://cognitiveservices.azure.com/.default",
        )
        _aoai_client = AzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            azure_ad_token_provider=token_provider,
            api_version="2024-12-01-preview",
        )
    return _aoai_client


def call_vision_model(prompt, image_b64):
    """Send an image + prompt to Azure OpenAI GPT-4o-mini
    and return the raw text response."""
    client = get_openai_client()
    response = client.chat.completions.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}",
                            "detail": "low",
                        },
                    },
                ],
            }
        ],
        temperature=0.1,
        max_tokens=300,
    )
    return response.choices[0].message.content


def call_vision_model_multi(prompt, images_b64):
    """Send multiple images + prompt to Azure OpenAI GPT-4o.
    images_b64 is a list of base64-encoded JPEG strings."""
    client = get_openai_client()
    content = [{"type": "text", "text": prompt}]
    for img_b64 in images_b64:
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{img_b64}",
                "detail": "low",
            },
        })
    response = client.chat.completions.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        messages=[{"role": "user", "content": content}],
        temperature=0.1,
        max_tokens=400,
    )
    return response.choices[0].message.content


def get_bedford_video_url():
    """Get the authenticated HLS stream URL for Bedford Ave."""
    cam = CAMERAS["bedford"]
    resp1 = requests.get(
        "https://www.511pa.com/Camera/GetVideoUrl"
        f"?imageId={cam['video_image_id']}",
        timeout=10,
    )
    resp1.raise_for_status()
    auth_data = resp1.json()

    resp2 = requests.post(
        "https://pa.arcadis-ivds.com"
        "/api/SecureTokenUri/GetSecureTokenUriBySourceId",
        json=auth_data,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    resp2.raise_for_status()
    token_query = resp2.json()

    base_url = (
        "https://pa-se4.arcadis-ivds.com:8200"
        "/chan-4321/index.m3u8"
    )
    return base_url + token_query


def capture_video_frames(duration=15, fps=1):
    """Capture frames from Bedford Ave HLS video stream.

    Uses ffmpeg to read ~duration seconds of live video and
    extract frames at the given fps. Returns list of JPEG bytes.
    """
    try:
        stream_url = get_bedford_video_url()
    except Exception as e:
        print(f"  [Video] Failed to get stream URL: {e}")
        return []

    with tempfile.TemporaryDirectory() as tmpdir:
        output_pattern = os.path.join(tmpdir, "frame_%03d.jpg")
        cmd = [
            "ffmpeg",
            "-y",                       # overwrite
            "-loglevel", "error",
            "-i", stream_url,
            "-t", str(duration),        # capture duration
            "-vf", f"fps={fps}",        # extract at fps
            "-q:v", "2",                # JPEG quality
            output_pattern,
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=duration + 30,  # generous timeout
            )
            if result.returncode != 0:
                print(f"  [Video] ffmpeg error: "
                      f"{result.stderr[:300]}")
                return []
        except subprocess.TimeoutExpired:
            print("  [Video] ffmpeg timed out")
            return []
        except FileNotFoundError:
            print("  [Video] ffmpeg not found")
            return []

        # Read extracted frames
        frames = []
        for fname in sorted(os.listdir(tmpdir)):
            if fname.endswith(".jpg"):
                fpath = os.path.join(tmpdir, fname)
                with open(fpath, "rb") as f:
                    frames.append(f.read())

        print(f"  [Video] Captured {len(frames)} frames "
              f"over {duration}s")
        return frames

# Global state
status_cache = {
    "status": "UNKNOWN",
    "bedford": {
        "status": "UNKNOWN",
        "reasoning": "Not yet analyzed",
        "image_b64": None,
    },
    "mm55": {
        "status": "N/A",
        "reasoning": "Visual reference only",
        "image_b64": None,
    },
    "last_check": None,
    "last_check_display": "Never",
    "error": None,
}
analysis_lock = threading.Lock()

# Sticky status: once confirmed OPEN (100%), hold for 10 minutes
# This gives 2 full analysis cycles to re-confirm before closing
STICKY_DURATION = 600  # 10 minutes in seconds
last_confirmed_open_time = None
last_confirmed_open_direction = None  # "INBOUND" or "OUTBOUND"


def get_hov_direction():
    """Determine HOV direction based on PennDOT schedule.

    Source: https://www.pa.gov/agencies/penndot/regional-offices/district-11/hov

    Mon-Fri 6:00 AM – 10:00 AM:  INBOUND (HOV 2+)
    Mon-Fri 10:00 AM – 2:00 PM:  CLOSED
    Mon-Fri 2:00 PM – 7:00 PM:   OUTBOUND (HOV 2+)
    Mon-Fri 7:00 PM – 5:00 AM:   UNRESTRICTED (outbound, any vehicle)
    Fri 7 PM – Mon 5 AM:          UNRESTRICTED (weekend)
    """
    now = now_eastern()
    hour = now.hour
    minute = now.minute
    weekday = now.weekday()  # 0=Monday, 6=Sunday

    # Weekend: Fri 7 PM through Mon 5 AM → UNRESTRICTED
    if weekday == 4 and hour >= 19:        # Friday 7 PM+
        return "UNRESTRICTED"
    if weekday in (5, 6):                  # Saturday, Sunday
        return "UNRESTRICTED"
    if weekday == 0 and hour < 5:          # Monday before 5 AM
        return "UNRESTRICTED"

    # Weekday schedule (Mon 5 AM through Fri 7 PM)
    if 6 <= hour < 10:
        return "INBOUND"
    elif 10 <= hour < 14:
        return "CLOSED"
    elif 14 <= hour < 19:
        return "OUTBOUND"
    elif hour >= 19 or hour < 5:
        return "UNRESTRICTED"
    else:
        # 5:00 AM – 6:00 AM: gap before inbound starts
        return "CLOSED"


def get_hov_schedule_info():
    """Return human-readable label for current period and next period."""
    direction = get_hov_direction()
    now = now_eastern()
    weekday = now.weekday()

    labels = {
        "INBOUND": "Inbound HOV 2+ (Mon–Fri 6–10 AM)",
        "OUTBOUND": "Outbound HOV 2+ (Mon–Fri 2–7 PM)",
        "CLOSED": "Closed (Mon–Fri 10 AM – 2 PM)",
        "UNRESTRICTED": "Open Unrestricted",
    }

    next_info = ""
    if direction == "INBOUND":
        next_info = "Closes at 10:00 AM"
    elif direction == "CLOSED":
        next_info = "Reopens OUTBOUND at 2:00 PM"
    elif direction == "OUTBOUND":
        next_info = "Switches to UNRESTRICTED at 7:00 PM"
    elif direction == "UNRESTRICTED":
        if weekday in (5, 6) or (weekday == 4 and now.hour >= 19):
            next_info = "INBOUND resumes Monday 6:00 AM"
        elif weekday == 0 and now.hour < 5:
            next_info = "INBOUND starts at 6:00 AM"
        else:
            next_info = "INBOUND starts at 6:00 AM"

    return {
        "period": direction,
        "period_label": labels.get(direction, direction),
        "next_period": next_info,
    }


def fetch_camera_image(camera_key):
    """Fetch the latest camera snapshot as bytes."""
    cam = CAMERAS[camera_key]
    url = f"{cam['image_url']}?t={int(time.time())}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"Error fetching {camera_key} image: {e}")
        return None


def preprocess_image(image_bytes):
    """Crop to the HOV exit ramp area (very top-left corner),
    enhance, and upscale to detect vehicles."""
    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size

    # Crop to the HOV ramp area in the upper-left
    # corner: top 50%, left 40% of the image
    cropped = img.crop((0, 0, int(w * 0.4), int(h * 0.5)))

    # Upscale 2x for better detail (3x was too large/slow)
    new_size = (cropped.width * 2, cropped.height * 2)
    upscaled = cropped.resize(new_size, Image.LANCZOS)

    # Enhance brightness (1.3x) and contrast (1.5x)
    # to make dark vehicles stand out
    enhancer = ImageEnhance.Brightness(upscaled)
    brightened = enhancer.enhance(1.3)
    enhancer = ImageEnhance.Contrast(brightened)
    enhanced = enhancer.enhance(1.5)

    # Also sharpen
    enhancer = ImageEnhance.Sharpness(enhanced)
    sharpened = enhancer.enhance(2.0)

    # Convert back to bytes
    buf = io.BytesIO()
    sharpened.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def analyze_bedford_image(image_bytes):
    """Analyze the Bedford Ave camera to detect HOV status.

    Preprocesses the image: crops to upper-left, enhances
    brightness/contrast, and upscales to help detect dark vehicles.
    """
    enhanced_bytes = preprocess_image(image_bytes)
    img_b64 = base64.b64encode(enhanced_bytes).decode("utf-8")

    prompt = """How many vehicles (cars, trucks, SUVs, buses) do you see in this image? If you see none, say 0. For each vehicle, note its color.

Respond with ONLY this JSON:
{"total_vehicles": number, "white_vehicle_present": true/false, "other_vehicles_present": true/false, "vehicle_description": "brief description or 'no vehicles'"}"""

    try:
        response_text = call_vision_model(prompt, img_b64)
        print(f"[AI Bedford raw] {response_text[:300]}")

        # Try to parse JSON from the response
        try:
            start = response_text.find("{")
            end = response_text.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(response_text[start:end])
        except json.JSONDecodeError:
            pass

        # Fallback: infer from text
        clean = response_text.replace("**", "").replace("*", "")
        clean = clean.replace("\n", " ")
        clean_upper = clean.upper()

        has_vehicles = any(
            p in clean_upper
            for p in [
                "VEHICLES VISIBLE", "CARS VISIBLE",
                "VEHICLES ON THE RAMP", "VEHICLES EXITING",
                "WHITE CAR", "WHITE VEHICLE",
            ]
        )
        no_vehicles = any(
            p in clean_upper
            for p in [
                "NO VEHICLES", "EMPTY", "NO CARS",
                "RAMP IS EMPTY", "NO TRAFFIC",
            ]
        )

        if has_vehicles and not no_vehicles:
            inferred = "OPEN"
        elif no_vehicles:
            inferred = "CLOSED"
        else:
            inferred = "UNCLEAR"

        return {
            "hov_status": inferred,
            "reasoning": " ".join(clean.split())[:300],
            "vehicles_at_exit": inferred == "OPEN",
        }

    except Exception as e:
        return {
            "hov_status": "ERROR",
            "reasoning": f"Analysis error: {str(e)}",
        }


def analyze_bedford_video(direction):
    """Capture ~15s of Bedford Ave video and analyze all
    frames together in a single GPT-4o call.

    Returns dict matching analyze_bedford_image format.
    """
    frames = capture_video_frames(duration=15, fps=1)
    if not frames:
        print("  [Video] No frames captured, falling back "
              "to snapshot")
        return None

    # Preprocess each frame (crop + enhance like snapshots)
    processed_b64 = []
    for frame_bytes in frames:
        enhanced = preprocess_image(frame_bytes)
        processed_b64.append(
            base64.b64encode(enhanced).decode("utf-8")
        )

    dir_label = "entering" if direction == "OUTBOUND" else "exiting"

    prompt = (
        f"These are {len(processed_b64)} consecutive frames "
        f"(1 per second) from a traffic camera at an HOV lane "
        f"entrance/exit ramp. Look across ALL frames for any "
        f"vehicles {dir_label} the HOV ramp. A vehicle may "
        f"appear in only 2-3 frames as it passes through. "
        f"Count the TOTAL unique vehicles you see across all "
        f"frames (don't double-count the same car). "
        f"For each vehicle note its color. "
        f"Respond with ONLY this JSON:\n"
        '{"total_vehicles": number, '
        '"white_vehicle_present": true/false, '
        '"other_vehicles_present": true/false, '
        '"vehicle_description": "brief description or '
        'no vehicles"}'
    )

    try:
        response_text = call_vision_model_multi(
            prompt, processed_b64
        )
        print(f"[AI Bedford Video raw] "
              f"{response_text[:300]}")

        try:
            start = response_text.find("{")
            end = response_text.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(
                    response_text[start:end]
                )
                result["_source"] = "video"
                result["_frames"] = len(processed_b64)
                return result
        except json.JSONDecodeError:
            pass

        return {"total_vehicles": 0,
                "vehicle_description": response_text[:200],
                "_source": "video"}

    except Exception as e:
        print(f"Bedford video analysis error: {e}")
        return None


def preprocess_mm55_image(image_bytes):
    """Crop to right 50% of MM 5.5 image (the highway side),
    enhance for vehicle/lane visibility."""
    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size

    # Crop to right 50% of the image (full height)
    cropped = img.crop((int(w * 0.5), 0, w, h))

    # Upscale 2x for better detail
    upscaled = cropped.resize(
        (cropped.width * 2, cropped.height * 2), Image.LANCZOS
    )

    # Enhance contrast and sharpness
    enhancer = ImageEnhance.Contrast(upscaled)
    enhanced = enhancer.enhance(1.5)
    enhancer = ImageEnhance.Sharpness(enhanced)
    sharpened = enhancer.enhance(2.0)

    buf = io.BytesIO()
    sharpened.save(buf, format="JPEG", quality=95)
    return buf.getvalue()

def analyze_mm55_vehicles(image_bytes):
    """Analyze the MM 5.5 camera for vehicles in the HOV
    exit lane (the left-most lane with dashed markings).
    Vehicles in this lane = HOV is open inbound."""
    enhanced = preprocess_mm55_image(image_bytes)
    img_b64 = base64.b64encode(enhanced).decode("utf-8")

    prompt = (
        "This is a highway traffic camera image. "
        "There is a yellow line on the left side of the road "
        "that curves to the right, creating an EXIT RAMP "
        "lane. This exit lane is separated from the main "
        "highway by SHORT DASHED lane markings. "
        "Do you see any vehicles in that EXIT RAMP lane "
        "(between the yellow line and the short dashes)? "
        "If none, say 0. "
        "Respond with ONLY this JSON: "
        '{"vehicles_in_exit_lane": number, '
        '"description": "what you see in the exit lane"}'
    )

    try:
        response_text = call_vision_model(prompt, img_b64)
        print(f"[AI MM55 raw] {response_text[:200]}")

        try:
            start = response_text.find("{")
            end = response_text.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(response_text[start:end])
        except json.JSONDecodeError:
            pass

        return {
            "vehicles_in_exit_lane": 0,
            "description": response_text[:200],
        }

    except Exception as e:
        print(f"MM55 analysis error: {e}")
        return {
            "vehicles_in_exit_lane": 0,
            "description": f"Error: {e}",
        }


def analyze_roadway_vehicles(image_bytes, camera_name=""):
    """Analyze MM 1.2 or MM 1.4 camera for any vehicles
    on the HOV roadway (center of image). Any vehicle
    on this road = 100% HOV is open."""
    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size

    # Upscale 2x for better detail
    upscaled = img.resize((w * 2, h * 2), Image.LANCZOS)
    enhancer = ImageEnhance.Contrast(upscaled)
    enhanced = enhancer.enhance(1.5)
    enhancer = ImageEnhance.Sharpness(enhanced)
    sharpened = enhancer.enhance(2.0)

    buf = io.BytesIO()
    sharpened.save(buf, format="JPEG", quality=95)
    img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    prompt = (
        "This traffic camera shows multiple roadways. "
        "Focus ONLY on the narrow two-lane HOV ramp/roadway "
        "that curves through the CENTER of the image — it has "
        "white dashed center-line markings and solid white edge "
        "lines. IGNORE all vehicles on the wider mainline "
        "highways on the left and right sides of the image. "
        "How many vehicles (cars, trucks, SUVs, buses) do you "
        "see on that center HOV roadway ONLY? "
        "If you see none on the center roadway, say 0. "
        "Respond with ONLY this JSON: "
        '{"total_vehicles": number, '
        '"description": "brief description of vehicles on '
        'the center HOV roadway, or no vehicles"}'
    )

    try:
        response_text = call_vision_model(prompt, img_b64)
        print(f"[AI {camera_name} raw] "
              f"{response_text[:200]}")

        try:
            start = response_text.find("{")
            end = response_text.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(response_text[start:end])
        except json.JSONDecodeError:
            pass

        return {"total_vehicles": 0, "description": response_text[:200]}

    except Exception as e:
        print(f"{camera_name} analysis error: {e}")
        return {"total_vehicles": 0, "description": f"Error: {e}"}

# Number of frames to capture per analysis cycle
CAPTURE_FRAMES = 6
CAPTURE_INTERVAL = 10  # seconds between frames (6 * 10 = 60s)

def capture_frames(camera_key, count=CAPTURE_FRAMES,
                   interval=CAPTURE_INTERVAL):
    """Capture multiple frames over time from a camera.
    Returns list of (image_bytes, timestamp) tuples."""
    frames = []
    for i in range(count):
        img = fetch_camera_image(camera_key)
        if img is not None:
            frames.append((img, now_eastern()))
            print(
                f"  [{camera_key}] frame {i+1}/{count} "
                f"captured ({len(img)} bytes)"
            )
        if i < count - 1:
            time.sleep(interval)
    return frames

def run_analysis():
    """Capture 6 frames over 60s from each camera, analyze
    all frames, and use the best result (most vehicles).

    Direction logic (PennDOT schedule):
      Mon-Fri 6-10 AM   INBOUND:  HOV 2+
      Mon-Fri 10AM-3PM  CLOSED
      Mon-Fri 3-7 PM    OUTBOUND: HOV 2+
      Evenings/weekends  UNRESTRICTED
    """
    global status_cache, last_confirmed_open_time
    global last_confirmed_open_direction

    direction = get_hov_direction()
    now = now_eastern()

    print(f"\n[{now.strftime('%H:%M:%S')}] Starting "
          f"analysis cycle...")

    # --- Capture Bedford video + snapshot frames for other cameras ---
    print("  Capturing Bedford Ave video (15s)...")
    bedford_video_result = analyze_bedford_video(direction)

    # Capture snapshot frames from all 4 cameras while
    # we process the video result
    mm55_frames = []
    mm12_frames = []
    mm14_frames = []
    bedford_snapshot = None  # single snapshot for display
    for i in range(CAPTURE_FRAMES):
        for key, lst in [("mm55", mm55_frames),
                         ("mm12", mm12_frames),
                         ("mm14", mm14_frames)]:
            img = fetch_camera_image(key)
            if img:
                lst.append(img)
        if bedford_snapshot is None:
            bedford_snapshot = fetch_camera_image("bedford")
        print(f"  Frame {i+1}/{CAPTURE_FRAMES} captured "
              f"(mm55/mm12/mm14)")
        if i < CAPTURE_FRAMES - 1:
            time.sleep(CAPTURE_INTERVAL)

    print(f"  Captured: MM55={len(mm55_frames)}, "
          f"MM12={len(mm12_frames)}, "
          f"MM14={len(mm14_frames)}. Analyzing...")

    # --- Bedford analysis: prefer video, fall back to snapshot ---
    if bedford_video_result is not None:
        analysis = bedford_video_result
        source = "video"
        n_frames = analysis.get("_frames", "?")
        print(f"  Bedford video analysis: "
              f"{analysis.get('total_vehicles', 0)} vehicles "
              f"({n_frames} frames)")
    elif bedford_snapshot:
        analysis = analyze_bedford_image(bedford_snapshot)
        source = "snapshot"
        print(f"  Bedford snapshot fallback: "
              f"{analysis.get('total_vehicles', 0)} vehicles")
    else:
        analysis = None
        source = None

    # Use latest snapshot for display image
    bedford_display_img = bedford_snapshot

    if analysis is None:
        bedford_result = {
            "status": "ERROR",
            "reasoning": "Failed to fetch camera image/video",
            "image_b64": None,
        }
    else:
        bedford_b64 = (
            base64.b64encode(bedford_display_img).decode("utf-8")
            if bedford_display_img else None
        )

        vehicle_desc = analysis.get("vehicle_description", "")
        white_present = analysis.get("white_vehicle_present", False)
        others_present = analysis.get(
            "other_vehicles_present",
            analysis.get("other_vehicles_on_ramp", False)
        )
        total = analysis.get("total_vehicles", 0)

        # Determine vehicle-based status
        vehicles_detected = total > 0

        # Build reasoning
        parts = []
        if vehicle_desc:
            parts.append(vehicle_desc)

        if direction == "INBOUND":
            # Morning: white vehicle + others = open inbound
            if total >= 2 or (white_present and others_present):
                hov_status = "OPEN"
                confidence = 100
                parts.append(
                    f"{total} vehicles exiting HOV — "
                    "OPEN INBOUND (100%)"
                )
            elif total == 1 and white_present:
                hov_status = "LIKELY_OPEN"
                confidence = 80
                parts.append(
                    "White vehicle at exit — "
                    "LIKELY OPEN INBOUND (80%)"
                )
            elif total == 1:
                hov_status = "OPEN"
                confidence = 100
                parts.append(
                    f"Vehicle at HOV exit — "
                    "OPEN INBOUND (100%)"
                )
            else:
                hov_status = "CLOSED"
                confidence = 100
                parts.append("No vehicles at exit")

        elif direction == "OUTBOUND":
            # Afternoon: any vehicle entering = open outbound
            if total >= 1:
                hov_status = "OPEN"
                confidence = 100
                parts.append(
                    f"{total} vehicle(s) entering HOV — "
                    "OPEN OUTBOUND (100%)"
                )
            else:
                hov_status = "CLOSED"
                confidence = 100
                parts.append(
                    "No vehicles entering HOV"
                )

        else:
            # Overnight
            hov_status = "CLOSED"
            confidence = 100
            parts.append("Outside HOV operating hours")

        source_label = (
            f"[{source}: {analysis.get('_frames', '?')} frames]"
            if source == "video" else "[snapshot]"
        )

        bedford_result = {
            "status": hov_status,
            "confidence": confidence,
            "reasoning": (
                f"{source_label} "
                + ". ".join(parts)
            )[:400],
            "image_b64": bedford_b64,
            "full_analysis": analysis,
        }

    # --- MM 5.5 analysis (best of captured frames) ---
    best_mm55_vehicles = 0
    best_mm55_desc = "Could not analyze"
    best_mm55_img = mm55_frames[0] if mm55_frames else None

    for idx, img_bytes in enumerate(mm55_frames):
        mm55_analysis = analyze_mm55_vehicles(img_bytes)
        v = mm55_analysis.get(
            "vehicles_in_exit_lane",
            mm55_analysis.get("vehicles_in_left_lane", 0)
        )
        desc = mm55_analysis.get(
            "description", "No description"
        )
        print(f"  MM55 frame {idx+1}: {v} exit-lane vehicles")
        if v > best_mm55_vehicles:
            best_mm55_vehicles = v
            best_mm55_desc = desc
            best_mm55_img = img_bytes

    mm55_vehicles = best_mm55_vehicles
    mm55_desc = best_mm55_desc
    mm55_b64 = (
        base64.b64encode(best_mm55_img).decode("utf-8")
        if best_mm55_img else None
    )
    mm55_status = "UNKNOWN"

    if best_mm55_img is not None:

        if direction == "INBOUND":
            if mm55_vehicles > 0:
                mm55_status = "OPEN"
                mm55_note = (
                    f"{mm55_vehicles} vehicle(s) in HOV "
                    f"lane — OPEN INBOUND"
                )
            else:
                mm55_status = "CLOSED"
                mm55_note = "No vehicles in HOV lane"
        elif direction == "OUTBOUND":
            mm55_status = "N/A"
            mm55_note = "Outbound mode — checking Bedford"
        else:
            mm55_status = "N/A"
            mm55_note = "Outside operating hours"
    else:
        mm55_note = "Could not fetch image"

    mm55_result = {
        "status": mm55_status,
        "reasoning": f"{mm55_note}. {mm55_desc}",
        "image_b64": mm55_b64,
        "vehicles_in_left_lane": mm55_vehicles,
    }

    # --- MM 1.2 analysis (best of captured frames) ---
    best_mm12_vehicles = 0
    best_mm12_desc = "Could not analyze"
    best_mm12_img = mm12_frames[0] if mm12_frames else None

    for idx, img_bytes in enumerate(mm12_frames):
        a = analyze_roadway_vehicles(img_bytes, "MM12")
        v = a.get("total_vehicles", 0)
        print(f"  MM12 frame {idx+1}: {v} vehicles")
        if v > best_mm12_vehicles:
            best_mm12_vehicles = v
            best_mm12_desc = a.get("description", "")
            best_mm12_img = img_bytes

    mm12_result = {
        "status": "OPEN" if best_mm12_vehicles > 0 else "CLOSED",
        "reasoning": (
            f"{best_mm12_vehicles} vehicle(s) on HOV "
            f"roadway. {best_mm12_desc}"
        ),
        "image_b64": (
            base64.b64encode(best_mm12_img).decode("utf-8")
            if best_mm12_img else None
        ),
        "vehicles": best_mm12_vehicles,
    }

    # --- MM 1.4 analysis (best of captured frames) ---
    best_mm14_vehicles = 0
    best_mm14_desc = "Could not analyze"
    best_mm14_img = mm14_frames[0] if mm14_frames else None

    for idx, img_bytes in enumerate(mm14_frames):
        a = analyze_roadway_vehicles(img_bytes, "MM14")
        v = a.get("total_vehicles", 0)
        print(f"  MM14 frame {idx+1}: {v} vehicles")
        if v > best_mm14_vehicles:
            best_mm14_vehicles = v
            best_mm14_desc = a.get("description", "")
            best_mm14_img = img_bytes

    mm14_result = {
        "status": "OPEN" if best_mm14_vehicles > 0 else "CLOSED",
        "reasoning": (
            f"{best_mm14_vehicles} vehicle(s) on HOV "
            f"roadway. {best_mm14_desc}"
        ),
        "image_b64": (
            base64.b64encode(best_mm14_img).decode("utf-8")
            if best_mm14_img else None
        ),
        "vehicles": best_mm14_vehicles,
    }

    # Total roadway vehicles from MM 1.2 + MM 1.4
    roadway_vehicles = best_mm12_vehicles + best_mm14_vehicles

    # --- Combined logic ---
    bedford_status = bedford_result.get("status", "UNCLEAR")
    bedford_conf = bedford_result.get("confidence", 0)

    # ANY camera detecting vehicles = HOV open (100%)
    # MM 1.2 and MM 1.4 are definitive — vehicles on the
    # HOV roadway means it's absolutely open
    any_roadway = roadway_vehicles > 0

    if direction == "INBOUND":
        if (any_roadway or bedford_status == "OPEN"
                or mm55_vehicles > 0):
            raw_status = "OPEN"
            confidence = 100
            confirms = []
            if any_roadway:
                confirms.append(
                    f"MM1.2/1.4: {roadway_vehicles} "
                    f"on HOV road"
                )
            if mm55_vehicles > 0:
                confirms.append(
                    f"MM5.5: {mm55_vehicles} in exit lane"
                )
            if confirms:
                bedford_result["reasoning"] = (
                    bedford_result.get("reasoning", "") +
                    f" [{'; '.join(confirms)}]"
                )[:400]
        elif bedford_status == "LIKELY_OPEN":
            raw_status = "LIKELY_OPEN"
            confidence = bedford_conf
        else:
            raw_status = "CLOSED"
            confidence = 100

    elif direction == "OUTBOUND":
        # Afternoon: any camera with vehicles = open
        if any_roadway or bedford_status == "OPEN":
            raw_status = "OPEN"
            confidence = 100
            if any_roadway:
                bedford_result["reasoning"] = (
                    bedford_result.get("reasoning", "") +
                    f" [MM1.2/1.4: {roadway_vehicles} "
                    f"on HOV road confirms OPEN]"
                )[:400]
        else:
            raw_status = bedford_status
            confidence = bedford_conf

    else:
        # Overnight
        raw_status = "CLOSED"
        confidence = 100

    # Add direction label to status
    if raw_status == "OPEN":
        display_status = f"OPEN {direction}"
    elif raw_status == "LIKELY_OPEN":
        display_status = f"LIKELY OPEN {direction}"
    else:
        display_status = raw_status

    # Sticky logic: once confirmed OPEN at 100%, hold for
    # 5 minutes. If another vehicle is seen within that
    # window, reset the timer for another 5 minutes.
    if raw_status == "OPEN" and confidence == 100:
        # New confirmed detection — reset the timer
        last_confirmed_open_time = now
        last_confirmed_open_direction = direction
        overall = display_status
    elif last_confirmed_open_time is not None:
        # We previously confirmed OPEN — check if within
        # the sticky window
        elapsed = (
            now - last_confirmed_open_time
        ).total_seconds()
        if elapsed < STICKY_DURATION:
            # Still within sticky window — hold OPEN
            remaining = int(STICKY_DURATION - elapsed)
            d = last_confirmed_open_direction or direction
            overall = f"OPEN {d}"
            confidence = 100
            bedford_result["reasoning"] = (
                bedford_result.get("reasoning", "") +
                f" [Holding OPEN — confirmed {int(elapsed)}s"
                f" ago, {remaining}s remaining]"
            )[:400]
        else:
            # Sticky window expired — now allow CLOSED
            last_confirmed_open_time = None
            last_confirmed_open_direction = None
            overall = display_status
    else:
        # No sticky state, no confirmed OPEN — use raw
        overall = display_status

    schedule = get_hov_schedule_info()

    with analysis_lock:
        status_cache = {
            "status": overall,
            "direction": direction,
            "confidence": confidence,
            "bedford": bedford_result,
            "mm55": mm55_result,
            "mm12": mm12_result,
            "mm14": mm14_result,
            "last_check": now.isoformat(),
            "last_check_display": now.strftime(
                "%I:%M:%S %p"
            ),
            "error": None,
            "period": schedule["period"],
            "period_label": schedule["period_label"],
            "next_period": schedule["next_period"],
            "ai_active": True,
        }

    print(
        f"[{now.strftime('%H:%M:%S')}] {direction} mode: "
        f"HOV is {overall} (conf: {confidence})"
    )
    return overall


def refresh_images_only():
    """Refresh camera images without AI analysis.
    Used outside HOV operating hours so images are
    available for visual inspection and ready when
    operating hours begin."""
    global status_cache
    now = now_eastern()
    schedule = get_hov_schedule_info()
    direction = get_hov_direction()

    print(f"[{now.strftime('%H:%M:%S')}] {direction} — "
          f"refreshing images only (AI paused)")

    # Fetch latest image from each camera
    camera_results = {}
    for key in CAMERAS:
        img = fetch_camera_image(key)
        img_b64 = (
            base64.b64encode(img).decode("utf-8")
            if img else None
        )
        camera_results[key] = {
            "status": "N/A",
            "reasoning": "AI analysis paused (outside operating hours)",
            "image_b64": img_b64,
        }

    # Set status based on schedule
    if direction == "UNRESTRICTED":
        overall = "OPEN UNRESTRICTED"
    else:
        overall = "CLOSED"

    with analysis_lock:
        status_cache = {
            "status": overall,
            "direction": direction,
            "confidence": 100,
            "bedford": camera_results.get("bedford", {}),
            "mm55": camera_results.get("mm55", {}),
            "mm12": camera_results.get("mm12", {}),
            "mm14": camera_results.get("mm14", {}),
            "last_check": now.isoformat(),
            "last_check_display": now.strftime(
                "%I:%M:%S %p"
            ),
            "error": None,
            "period": schedule["period"],
            "period_label": schedule["period_label"],
            "next_period": schedule["next_period"],
            "ai_active": False,
        }


def analysis_loop():
    """Background thread: full AI analysis during HOV
    operating hours, image-only refresh otherwise."""
    while True:
        try:
            direction = get_hov_direction()
            if direction in ("INBOUND", "OUTBOUND"):
                # Operating hours — run full AI analysis
                run_analysis()
                time.sleep(240)  # 4 minutes between cycles
            else:
                # CLOSED or UNRESTRICTED — images only
                refresh_images_only()
                time.sleep(60)   # Refresh images every 60s
        except Exception as e:
            print(f"Analysis error: {e}")
            with analysis_lock:
                status_cache["error"] = str(e)
            time.sleep(60)


# --- Start background analysis thread ---
# Must be at module level so it runs under both
# `python app.py` and gunicorn.
analysis_thread = threading.Thread(
    target=analysis_loop, daemon=True
)
analysis_thread.start()


# --- Routes ---


@app.route("/health")
def health():
    """Health check for Container Apps probes."""
    return "OK", 200


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    """Return current HOV status."""
    with analysis_lock:
        return jsonify(status_cache)


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """Trigger immediate analysis."""
    run_analysis()
    with analysis_lock:
        return jsonify(status_cache)

@app.route("/api/debug/crops")
def debug_crops():
    """Show the cropped regions being analyzed with red borders."""
    from PIL import ImageDraw

    results = {}

    # Bedford crop
    bedford_img = fetch_camera_image("bedford")
    if bedford_img:
        img = Image.open(io.BytesIO(bedford_img))
        w, h = img.size
        # Draw red rectangle showing crop area
        draw = ImageDraw.Draw(img)
        crop_box = (0, 0, int(w * 0.4), int(h * 0.5))
        draw.rectangle(crop_box, outline="red", width=3)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        results["bedford_full"] = base64.b64encode(
            buf.getvalue()
        ).decode()

        # Also save the actual crop
        cropped = Image.open(io.BytesIO(bedford_img)).crop(crop_box)
        buf2 = io.BytesIO()
        cropped.save(buf2, format="JPEG", quality=90)
        results["bedford_crop"] = base64.b64encode(
            buf2.getvalue()
        ).decode()

    # MM 5.5 - show full with crop box and cropped version
    mm55_img = fetch_camera_image("mm55")
    if mm55_img:
        img2 = Image.open(io.BytesIO(mm55_img))
        w2, h2 = img2.size
        draw2 = ImageDraw.Draw(img2)
        mm55_left = int(w2 * 0.5)
        mm55_crop_box = (mm55_left, 0, w2, h2)
        draw2.rectangle(mm55_crop_box, outline="red", width=3)
        buf3 = io.BytesIO()
        img2.save(buf3, format="JPEG", quality=90)
        results["mm55_full"] = base64.b64encode(
            buf3.getvalue()
        ).decode()

        cropped2 = Image.open(io.BytesIO(mm55_img)).crop(
            mm55_crop_box
        )
        buf4 = io.BytesIO()
        cropped2.save(buf4, format="JPEG", quality=90)
        results["mm55_crop"] = base64.b64encode(
            buf4.getvalue()
        ).decode()

    html = """<html><body style="background:#222;color:#fff;font-family:sans-serif;padding:20px">
    <h2>Debug: Crop Regions</h2>
    <h3>Bedford Ave - Full image with crop area (red box)</h3>
    <img src="data:image/jpeg;base64,{bedford_full}" style="border:2px solid #555;max-width:640px"><br><br>
    <h3>Bedford Ave - Cropped region (what AI sees)</h3>
    <img src="data:image/jpeg;base64,{bedford_crop}" style="border:2px solid #555;max-width:640px"><br><br>
    <h3>MM 5.5 - Full image with crop area (red box = right 50%)</h3>
    <img src="data:image/jpeg;base64,{mm55_full}" style="border:2px solid #555;max-width:640px"><br><br>
    <h3>MM 5.5 - Cropped region (what AI sees for HOV lane vehicles)</h3>
    <img src="data:image/jpeg;base64,{mm55_crop}" style="border:2px solid #555;max-width:640px">
    </body></html>""".format(**results)
    return html


@app.route("/api/camera/<camera_key>/image")
def camera_image(camera_key):
    """Proxy camera image to avoid CORS issues."""
    if camera_key not in CAMERAS:
        return "Not found", 404
    img = fetch_camera_image(camera_key)
    if img is None:
        return "Failed to fetch image", 502
    return Response(img, mimetype="image/jpeg")


@app.route("/api/camera/<camera_key>/video-token")
def camera_video_token(camera_key):
    """Get video stream token for a camera."""
    if camera_key not in CAMERAS:
        return jsonify({"error": "Not found"}), 404

    cam = CAMERAS[camera_key]
    try:
        # Step 1: Get auth info from 511PA
        resp1 = requests.get(
            "https://www.511pa.com/Camera/GetVideoUrl"
            f"?imageId={cam['video_image_id']}",
            timeout=10,
        )
        resp1.raise_for_status()
        auth_data = resp1.json()

        # Step 2: Get secure token
        resp2 = requests.post(
            "https://pa.arcadis-ivds.com"
            "/api/SecureTokenUri/GetSecureTokenUriBySourceId",
            json=auth_data,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        resp2.raise_for_status()
        token_query = resp2.json()

        # Video stream URLs
        video_urls = {
            "bedford": (
                "https://pa-se4.arcadis-ivds.com:8200"
                "/chan-4321/index.m3u8"
            ),
            "mm55": (
                "https://pa-se3.arcadis-ivds.com:8200"
                "/chan-3315/index.m3u8"
            ),
        }

        video_url = video_urls.get(camera_key, "")
        full_url = video_url + token_query

        return jsonify({
            "url": full_url,
            "type": "application/x-mpegURL",
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  Pittsburgh HOV Lane Status Checker")
    print("  Open http://localhost:5050 in your browser")
    print("=" * 60 + "\n")

    app.run(host="0.0.0.0", port=5050, debug=False)