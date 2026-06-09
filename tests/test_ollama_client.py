import os
import unittest
from unittest.mock import MagicMock

from src.api.ollama_client import OllamaClient, cosine_similarity


class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _EnvGuard(unittest.TestCase):
    """Base that snapshots/restores the OLLAMA_* env between tests."""

    OLLAMA_KEYS = [
        "OLLAMA_ENABLED", "OLLAMA_URL", "OLLAMA_EMBED_MODEL", "OLLAMA_CHAT_MODEL",
        "OLLAMA_KEEP_ALIVE", "OLLAMA_NUM_CTX",
    ]

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.OLLAMA_KEYS}
        os.environ["OLLAMA_ENABLED"] = "true"
        os.environ["OLLAMA_URL"] = "http://ollama:11434"
        os.environ["OLLAMA_EMBED_MODEL"] = "nomic-embed-text"
        os.environ["OLLAMA_CHAT_MODEL"] = "qwen2.5:14b"
        os.environ.pop("OLLAMA_KEEP_ALIVE", None)
        os.environ.pop("OLLAMA_NUM_CTX", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestCosineSimilarity(unittest.TestCase):
    def test_identical_vectors(self):
        self.assertAlmostEqual(cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]), 1.0)

    def test_orthogonal_vectors(self):
        self.assertEqual(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_bad_input(self):
        self.assertEqual(cosine_similarity([], [1.0]), 0.0)
        self.assertEqual(cosine_similarity([1.0, 2.0], [1.0]), 0.0)
        self.assertEqual(cosine_similarity([0.0, 0.0], [1.0, 1.0]), 0.0)


class TestOllamaClient(_EnvGuard):
    def test_is_configured_gating(self):
        client = OllamaClient()
        self.assertTrue(client.is_configured())
        os.environ["OLLAMA_ENABLED"] = "false"
        self.assertFalse(client.is_configured())

    def test_embed_parses_api_embed(self):
        client = OllamaClient()
        client.session = MagicMock()
        client.session.post.return_value = _FakeResp(200, {"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

        result = client.embed(["a", "b"])
        self.assertEqual(result, [[0.1, 0.2], [0.3, 0.4]])
        self.assertIn("/api/embed", client.session.post.call_args[0][0])

    def test_embed_falls_back_to_legacy_on_404(self):
        client = OllamaClient()
        client.session = MagicMock()

        def _post(url, **kwargs):
            if url.endswith("/api/embed"):
                return _FakeResp(404)
            return _FakeResp(200, {"embedding": [0.5, 0.6]})

        client.session.post.side_effect = _post
        result = client.embed(["only"])
        self.assertEqual(result, [[0.5, 0.6]])
        # Once flagged missing, it should not retry /api/embed.
        self.assertTrue(client._embed_endpoint_missing)

    def test_embed_network_error_returns_none(self):
        client = OllamaClient()
        client.session = MagicMock()
        client.session.post.side_effect = RuntimeError("connection refused")
        self.assertIsNone(client.embed(["x"]))

    def test_embed_disabled_returns_none(self):
        os.environ["OLLAMA_ENABLED"] = "false"
        client = OllamaClient()
        client.session = MagicMock()
        self.assertIsNone(client.embed(["x"]))
        client.session.post.assert_not_called()

    def test_judge_parses_json(self):
        client = OllamaClient()
        client.session = MagicMock()
        client.session.post.return_value = _FakeResp(
            200, {"message": {"content": '{"choice": 1, "confidence": 90}'}}
        )
        result = client.judge("pick one")
        self.assertEqual(result, {"choice": 1, "confidence": 90})

    def test_judge_bad_json_returns_none(self):
        client = OllamaClient()
        client.session = MagicMock()
        client.session.post.return_value = _FakeResp(200, {"message": {"content": "not json"}})
        self.assertIsNone(client.judge("pick one"))

    def test_list_models(self):
        client = OllamaClient()
        client.session = MagicMock()
        client.session.get.return_value = _FakeResp(200, {"models": [{"name": "qwen2.5:14b"}]})
        self.assertEqual(client.list_models(), ["qwen2.5:14b"])


class TestOllamaClientOptions(_EnvGuard):
    def test_embed_payload_includes_keep_alive(self):
        client = OllamaClient()
        client.session = MagicMock()
        client.session.post.return_value = _FakeResp(200, {"embeddings": [[0.1]]})
        client.embed(["a"])
        payload = client.session.post.call_args.kwargs["json"]
        self.assertEqual(payload["keep_alive"], "5m")

    def test_empty_keep_alive_omitted(self):
        os.environ["OLLAMA_KEEP_ALIVE"] = ""
        client = OllamaClient()
        client.session = MagicMock()
        client.session.post.return_value = _FakeResp(200, {"embeddings": [[0.1]]})
        client.embed(["a"])
        payload = client.session.post.call_args.kwargs["json"]
        self.assertNotIn("keep_alive", payload)

    def test_judge_options_include_num_predict_and_ctx(self):
        os.environ["OLLAMA_NUM_CTX"] = "8192"
        client = OllamaClient()
        client.session = MagicMock()
        client.session.post.return_value = _FakeResp(200, {"message": {"content": "{}"}})
        client.judge("prompt")
        options = client.session.post.call_args.kwargs["json"]["options"]
        self.assertEqual(options["num_predict"], 200)
        self.assertEqual(options["num_ctx"], 8192)
        self.assertEqual(options["temperature"], 0.0)

    def test_judge_omits_num_ctx_when_unset(self):
        client = OllamaClient()
        client.session = MagicMock()
        client.session.post.return_value = _FakeResp(200, {"message": {"content": "{}"}})
        client.judge("prompt")
        options = client.session.post.call_args.kwargs["json"]["options"]
        self.assertNotIn("num_ctx", options)

    def test_retry_once_on_connection_error(self):
        import requests as _requests

        client = OllamaClient()
        client.session = MagicMock()
        client.session.post.side_effect = [
            _requests.exceptions.ConnectionError("blip"),
            _FakeResp(200, {"embeddings": [[0.1]]}),
        ]
        self.assertEqual(client.embed(["a"]), [[0.1]])
        self.assertEqual(client.session.post.call_count, 2)

    def test_no_second_retry_on_persistent_connection_error(self):
        import requests as _requests

        client = OllamaClient()
        client.session = MagicMock()
        client.session.post.side_effect = _requests.exceptions.ConnectionError("down")
        # _embed_batch fails after its retry; legacy fallback also fails after its own.
        self.assertIsNone(client.embed(["a"]))


class TestOllamaStructuredOutputs(_EnvGuard):
    SCHEMA = {"type": "object", "properties": {"choice": {"type": "integer"}}}

    def test_schema_sent_as_format(self):
        client = OllamaClient()
        client.session = MagicMock()
        client.session.post.return_value = _FakeResp(200, {"message": {"content": '{"choice": 1}'}})
        result = client.judge("pick", schema=self.SCHEMA)
        self.assertEqual(result, {"choice": 1})
        payload = client.session.post.call_args.kwargs["json"]
        self.assertEqual(payload["format"], self.SCHEMA)

    def test_no_schema_uses_json_format(self):
        client = OllamaClient()
        client.session = MagicMock()
        client.session.post.return_value = _FakeResp(200, {"message": {"content": "{}"}})
        client.judge("pick")
        payload = client.session.post.call_args.kwargs["json"]
        self.assertEqual(payload["format"], "json")

    def test_schema_400_falls_back_to_json_and_sticks(self):
        client = OllamaClient()
        client.session = MagicMock()
        client.session.post.side_effect = [
            _FakeResp(400),
            _FakeResp(200, {"message": {"content": '{"choice": 2}'}}),
            _FakeResp(200, {"message": {"content": '{"choice": 3}'}}),
        ]
        result = client.judge("pick", schema=self.SCHEMA)
        self.assertEqual(result, {"choice": 2})
        self.assertTrue(client._schema_format_unsupported)
        # Subsequent calls skip the schema entirely.
        client.judge("pick again", schema=self.SCHEMA)
        payload = client.session.post.call_args.kwargs["json"]
        self.assertEqual(payload["format"], "json")
        self.assertEqual(client.session.post.call_count, 3)


if __name__ == "__main__":
    unittest.main()
