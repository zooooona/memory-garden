"""Run Replicate predictions over HTTP without SDK version validation."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request


API_BASE = "https://api.replicate.com/v1"


def run_prediction(model, model_input, api_token, timeout=180):
    """Create one prediction, wait for completion, and return its raw output."""
    if not api_token:
        raise ValueError("Missing REPLICATE_API_TOKEN. Save .env and restart the server.")

    deadline = time.monotonic() + timeout

    def request_json(path, payload=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for the Replicate prediction.")
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
            "User-Agent": "memory-garden/1.0",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers.update({"Content-Type": "application/json", "Prefer": "wait=60"})
        request = urllib.request.Request(API_BASE + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=min(70, remaining)) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                error = json.loads(detail)
                detail = error.get("detail") or error.get("error") or detail
            except (ValueError, AttributeError):
                pass
            raise RuntimeError(f"Replicate HTTP {exc.code} ({path}): {detail}") from exc

    # Official models use owner/name; their response version may be null.
    prediction = request_json(
        f"/models/{model}/predictions",
        {"input": model_input},
    )
    while prediction.get("status") in {"starting", "processing"}:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for the Replicate prediction.")
        prediction_id = prediction.get("id")
        if not prediction_id:
            raise RuntimeError("Replicate returned a prediction without an ID.")
        time.sleep(min(1, remaining))
        prediction = request_json(
            f"/predictions/{urllib.parse.quote(prediction_id, safe='')}"
        )

    if prediction.get("status") != "succeeded":
        raise RuntimeError(
            f"Replicate prediction {prediction.get('status', 'unknown')}: "
            f"{prediction.get('error') or 'No result returned.'}"
        )

    return prediction.get("output")


def run_text_model(model, prompt, api_token, timeout=180):
    """Wait for one text prediction, accepting strings or text chunks."""
    output = run_prediction(
        model, {"prompt": prompt, "thinking_level": "low"}, api_token, timeout
    )
    if isinstance(output, list) and all(isinstance(chunk, str) for chunk in output):
        output = "".join(output)
    if not isinstance(output, str) or not output.strip():
        raise ValueError("Replicate returned empty or unsupported text output.")
    return output
