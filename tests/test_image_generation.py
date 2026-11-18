import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import server


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="
)
IMAGE = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")
OUTPUT_URL = "https://replicate.delivery/test/output.png"


class ImageGenerationTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parent
        self.temp = tempfile.TemporaryDirectory(prefix="image-test-", dir=test_root)
        assert Path(self.temp.name).resolve().is_relative_to(test_root)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "MemoryGarden"
        directory_patch = patch.object(server, "MEMORY_GARDEN_DIR", self.directory)
        token_patch = patch.object(server, "REPLICATE_API_TOKEN", "test-token")
        directory_patch.start()
        token_patch.start()
        self.addCleanup(directory_patch.stop)
        self.addCleanup(token_patch.stop)
        self.client = server.app.test_client()

    def post_image(self):
        return self.client.post("/api/generate", json={"image": IMAGE, "prompt": "watercolor"})

    def test_http_prediction_image_download_and_save(self):
        for output in (OUTPUT_URL, [OUTPUT_URL]):
            with self.subTest(output=output):
                responses = [
                    io.BytesIO(b'{"id":"p1","version":null,"status":"processing"}'),
                    io.BytesIO(json.dumps({"id": "p1", "version": None, "status": "succeeded", "output": output}).encode()),
                    io.BytesIO(PNG),
                ]
                with patch("urllib.request.urlopen", side_effect=responses) as send:
                    with patch("replicate_text.time.sleep"):
                        response = self.post_image()
                self.assertEqual(response.status_code, 200, response.get_json())
                body = response.get_json()
                self.assertEqual(body["image"], IMAGE)
                self.assertEqual((self.directory / body["filename"]).read_bytes(), PNG)
                prediction_request = send.call_args_list[0].args[0]
                self.assertEqual(prediction_request.full_url, "https://api.replicate.com/v1/models/google/nano-banana-pro/predictions")
                inputs = json.loads(prediction_request.data)["input"]
                self.assertEqual(inputs["image_input"], [IMAGE])
                self.assertEqual(inputs["prompt"], "watercolor")
                self.assertEqual(inputs["output_format"], "png")
                self.assertNotIn("thinking_level", inputs)
                self.assertEqual(send.call_args_list[1].args[0].get_method(), "GET")
                download_request = send.call_args_list[2].args[0]
                self.assertEqual(download_request.full_url, OUTPUT_URL)
                self.assertIsNone(download_request.get_header("Authorization"))
                for call in send.call_args_list:
                    self.assertEqual(call.args[0].get_header("User-agent"), "memory-garden/1.0")
                self.assertEqual(send.call_count, 3)

    def test_upstream_500_is_reported_without_duplicate_generation(self):
        error = urllib.error.HTTPError("https://api.replicate.com", 500, "Error", {},
                                       io.BytesIO(b'{"detail":"An unexpected error occurred"}'))
        with patch("urllib.request.urlopen", side_effect=error) as send:
            response = self.post_image()
        self.assertEqual(response.status_code, 502)
        self.assertIn("Image generation failed: Replicate HTTP 500", response.get_json()["error"])
        self.assertIn("/models/google/nano-banana-pro/predictions", response.get_json()["error"])
        self.assertEqual(send.call_count, 1)
        self.assertFalse(self.directory.exists())

    def test_invalid_image_is_rejected_before_api_call(self):
        for image in ("not-base64", "", 42, "data:image/png;base64,aGVsbG8="):
            with self.subTest(image=image), patch("urllib.request.urlopen") as send:
                response = self.client.post("/api/generate", json={"image": image, "prompt": "watercolor"})
                self.assertEqual(response.status_code, 400)
                send.assert_not_called()

    def test_empty_output_does_not_download(self):
        with patch("server.run_prediction", return_value=None), patch("urllib.request.urlopen") as send:
            response = self.post_image()
        self.assertEqual(response.status_code, 502)
        self.assertIn("download failed", response.get_json()["error"])
        send.assert_not_called()

    def test_download_failure_returns_json_and_does_not_save(self):
        with patch("server.run_prediction", return_value=OUTPUT_URL):
            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Download unavailable")):
                response = self.post_image()
        self.assertEqual(response.status_code, 502)
        self.assertIn("download failed", response.get_json()["error"])
        self.assertFalse(self.directory.exists())

    def test_generation_timeout_returns_json(self):
        with patch("server.run_prediction", side_effect=TimeoutError("Prediction still processing")):
            response = self.post_image()
        self.assertEqual(response.status_code, 504)
        self.assertIn("timed out", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
