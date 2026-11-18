import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from replicate_text import run_text_model


class TextModelTests(unittest.TestCase):
    def run_predictions(self, predictions):
        responses = [io.BytesIO(json.dumps(p).encode()) for p in predictions]
        with patch("replicate_text.urllib.request.urlopen", side_effect=responses) as send:
            with patch("replicate_text.time.sleep"):
                result = run_text_model("google/gemini-3-flash", "test memory", "test-token")
        return result, send

    def test_null_version_and_string_output(self):
        result, send = self.run_predictions([
            {"id": "p1", "version": None, "status": "succeeded", "output": '{"all_clusters": []}'}
        ])
        self.assertEqual(json.loads(result), {"all_clusters": []})
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.replicate.com/v1/models/google/gemini-3-flash/predictions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data)["input"], {"prompt": "test memory", "thinking_level": "low"})
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(request.get_header("User-agent"), "memory-garden/1.0")

    def test_poll_until_complete_and_join_chunks(self):
        result, send = self.run_predictions([
            {"id": "p1", "version": None, "status": "starting"},
            {"id": "p1", "version": None, "status": "processing"},
            {"id": "p1", "version": None, "status": "succeeded", "output": ['{"center_', 'name":"test"}']},
        ])
        self.assertEqual(json.loads(result), {"center_name": "test"})
        self.assertEqual(send.call_count, 3)
        self.assertEqual(send.call_args.args[0].get_method(), "GET")
        self.assertEqual(send.call_args.args[0].full_url, "https://api.replicate.com/v1/predictions/p1")

    def test_failed_or_canceled_prediction(self):
        for status in ("failed", "canceled"):
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, status):
                self.run_predictions([{"status": status, "error": "Model unavailable"}])

    def test_invalid_output(self):
        for output in (None, "", [], {"unexpected": True}):
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, "text output"):
                self.run_predictions([{"status": "succeeded", "output": output}])

    def test_http_auth_error(self):
        error = urllib.error.HTTPError("https://api.replicate.com", 401, "Unauthorized", {},
                                       io.BytesIO(b'{"detail":"Invalid token"}'))
        with patch("replicate_text.urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401.*Invalid token"):
                run_text_model("google/gemini-3-flash", "test", "test-token")

    def test_missing_token_does_not_send_request(self):
        with patch("replicate_text.urllib.request.urlopen") as send:
            with self.assertRaisesRegex(ValueError, "REPLICATE_API_TOKEN"):
                run_text_model("google/gemini-3-flash", "test", "")
            send.assert_not_called()

    def test_timeout_does_not_create_another_prediction(self):
        response = io.BytesIO(b'{"id":"p1","status":"processing","version":null}')
        with patch("replicate_text.urllib.request.urlopen", return_value=response) as send:
            with patch("replicate_text.time.monotonic", side_effect=[0, 0, 181]):
                with self.assertRaises(TimeoutError):
                    run_text_model("google/gemini-3-flash", "test", "test-token")
            self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
