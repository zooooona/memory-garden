#!/usr/bin/env python3
"""
Memory Garden Web Server
Serves the p5.js frontend and proxies Replicate API calls.

Launch:
    uv run --with flask --with replicate server.py
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
import urllib.request
import webbrowser
from pathlib import Path
from threading import Timer

from flask import Flask, jsonify, request, send_from_directory
from replicate_text import run_prediction, run_text_model

ROOT = Path(__file__).resolve().parent
MEMORY_GARDEN_DIR = ROOT / "MemoryGarden"
MEMORIES_DIR = ROOT / "memories_of_your_class_only"
MODEL = "google/nano-banana-pro"
LLM_MODEL = "google/gemini-3-flash"
# Load API token from .env file or environment
_env_path = ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

REPLICATE_API_TOKEN = os.environ.get("REPLICATE_API_TOKEN", "")

app = Flask(__name__, static_folder=str(ROOT / "static"))


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(app.static_folder, filename)


@app.route("/api/garden")
def get_garden():
    garden_path = ROOT / "garden.json"
    with open(garden_path, encoding="utf-8") as f:
        data = json.load(f)
    return jsonify(data)


@app.route("/api/generate", methods=["POST"])
def generate():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Expected an image and prompt as JSON."}), 400
    image_b64 = body.get("image", "")
    prompt = body.get("prompt", "")

    if not isinstance(image_b64, str) or not image_b64 or not isinstance(prompt, str) or not prompt.strip():
        return jsonify({"error": "Missing image or prompt"}), 400

    # Strip data URL prefix if present
    if "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]

    try:
        image_bytes = base64.b64decode(image_b64, validate=True)
    except (binascii.Error, ValueError):
        return jsonify({"error": "Invalid base64 image. Capture the flower again."}), 400
    if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return jsonify({"error": "Expected a PNG flower capture."}), 400

    try:
        # Embed the canvas PNG directly; avoid the SDK's extra /files upload.
        output = run_prediction(
            MODEL,
            {
                "prompt": prompt.strip(),
                "image_input": ["data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")],
                "aspect_ratio": "match_input_image",
                "resolution": "2K",
                "output_format": "png",
            },
            REPLICATE_API_TOKEN,
            timeout=300,
        )
    except TimeoutError as e:
        return jsonify({"error": f"Image generation timed out: {e}"}), 504
    except Exception as e:
        return jsonify({"error": f"Image generation failed: {e}"}), 502

    # The HTTP API returns an image URL, not an SDK FileOutput object.
    try:
        if isinstance(output, list) and output:
            output = output[0]
        if not isinstance(output, str) or not output.startswith("https://"):
            raise ValueError("Replicate did not return an HTTPS image URL.")
        download_request = urllib.request.Request(
            output, headers={"User-Agent": "memory-garden/1.0"}
        )
        with urllib.request.urlopen(download_request, timeout=60) as response:
            result_bytes = response.read()
        if not result_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("The generated image download was not a PNG.")
    except Exception as e:
        return jsonify({"error": f"Generated image download failed: {e}"}), 502

    # Save to MemoryGarden/
    out_name = f"nano-banana-{time.strftime('%Y%m%d-%H%M%S')}.png"
    out_path = MEMORY_GARDEN_DIR / out_name
    try:
        MEMORY_GARDEN_DIR.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(result_bytes)
    except OSError as e:
        return jsonify({"error": f"Generated image could not be saved: {e}"}), 500

    # Return as base64
    result_b64 = base64.b64encode(result_bytes).decode("ascii")
    return jsonify({
        "image": f"data:image/png;base64,{result_b64}",
        "filename": out_name,
    })


@app.route("/api/students")
def list_students():
    """List available student memory files."""
    files = sorted(p.stem for p in MEMORIES_DIR.glob("*.json"))
    return jsonify(files)


@app.route("/api/process-memories", methods=["POST"])
def process_memories():
    """Use Gemini 3 Flash to convert raw student memories into garden.json format."""
    body = request.get_json()
    student = body.get("student", "")

    student_file = MEMORIES_DIR / f"{student}.json"
    if not student_file.exists():
        return jsonify({"error": f"Student file not found: {student}"}), 404

    with open(student_file, encoding="utf-8") as f:
        raw_memories = json.load(f)

    student_name = raw_memories[0].get("name", student) if raw_memories else student
    activities = [m.get("activity", "") for m in raw_memories if m.get("activity")]

    # Load the example garden.json as a format reference
    with open(ROOT / "garden.json", encoding="utf-8") as f:
        example = json.load(f)
    example_cluster = json.dumps(example["all_clusters"][0], indent=2)

    prompt = f"""Analyze the following personal memories and convert them into a structured JSON format.

For EACH memory, you must:
1. Assign a short "theme" (2-3 words, e.g. "Pure Joy", "Heartbreak", "Quiet Moment")
2. Rate these 5 emotional features as floats:
   - valence: -1.0 (very sad/negative) to 1.0 (very happy/positive)
   - impact: 0.0 (trivial) to 1.0 (life-changing)
   - togetherness: 0.0 (completely alone) to 1.0 (deeply social)
   - motion: 0.0 (completely still) to 1.0 (full of physical movement)
   - length: 0.0 (brief moment) to 1.0 (extended experience)

Here is an example of ONE cluster in the output format:
{example_cluster}

The student's name is: {student_name}

Their memories are:
{chr(10).join(f"- {a}" for a in activities)}

Return ONLY valid JSON in this exact format (no markdown, no explanation):
{{
  "center_name": "{student_name}",
  "all_clusters": [
    ... one object per memory ...
  ]
}}"""

    try:
        result_text = run_text_model(LLM_MODEL, prompt, REPLICATE_API_TOKEN)
    except Exception as e:
        return jsonify({"error": f"LLM call failed: {e}"}), 500

    # Extract JSON from response (might have extra text around it)
    try:
        # Try to find JSON in the response
        start = result_text.index("{")
        end = result_text.rindex("}") + 1
        json_str = result_text[start:end]
        garden_data = json.loads(json_str)
    except (ValueError, json.JSONDecodeError) as e:
        return jsonify({"error": f"Failed to parse LLM output: {e}", "raw": result_text}), 500

    # Validate structure
    if "all_clusters" not in garden_data or "center_name" not in garden_data:
        return jsonify({"error": "Invalid garden.json structure from LLM", "raw": result_text}), 500

    return jsonify(garden_data)


if __name__ == "__main__":
    Timer(1.5, lambda: webbrowser.open("http://localhost:8080")).start()
    print("Memory Garden running at http://localhost:8080")
    app.run(host="127.0.0.1", port=8080, debug=False)
