"""Tests for the settings Test button backend for Ollama (_test_ollama)."""

import os
import unittest
from unittest.mock import patch, MagicMock

from src.web_server import _test_llm_provider, _test_ollama, _ollama_show_info


def _resp(status_code, payload=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload or {}
    return r


_TAGS = {"models": [{"name": "nomic-embed-text:latest"}, {"name": "qwen2.5:14b"}]}


class TestOllamaTestEndpoint(unittest.TestCase):
    def test_disabled(self):
        result = _test_ollama(False, "http://x", "", "")
        self.assertFalse(result["ok"])

    def test_missing_model_reports_pull_command(self):
        with patch("src.web_server.requests.get", return_value=_resp(200, {"models": []})):
            result = _test_ollama(True, "http://x", "nomic-embed-text", "qwen2.5:14b")
        self.assertFalse(result["ok"])
        self.assertIn("ollama pull nomic-embed-text", result["message"])

    def test_success_includes_show_info(self):
        show = {
            "model_info": {"nomic-bert.context_length": 2048},
            "capabilities": ["embedding"],
        }
        with patch("src.web_server.requests.get", return_value=_resp(200, _TAGS)), \
             patch("src.web_server.requests.post", return_value=_resp(200, show)):
            result = _test_ollama(True, "http://x", "nomic-embed-text", "qwen2.5:14b")
        self.assertTrue(result["ok"])
        self.assertIn("ctx 2048", result["message"])
        self.assertIn("embedding", result["message"])

    def test_success_degrades_when_show_unavailable(self):
        with patch("src.web_server.requests.get", return_value=_resp(200, _TAGS)), \
             patch("src.web_server.requests.post", side_effect=RuntimeError("old server")):
            result = _test_ollama(True, "http://x", "nomic-embed-text", "qwen2.5:14b")
        self.assertTrue(result["ok"])
        self.assertIn("nomic-embed-text ✓", result["message"])

    def test_warns_when_embed_model_lacks_embedding_capability(self):
        show = {"model_info": {}, "capabilities": ["completion"]}
        with patch("src.web_server.requests.get", return_value=_resp(200, _TAGS)), \
             patch("src.web_server.requests.post", return_value=_resp(200, show)):
            result = _test_ollama(True, "http://x", "nomic-embed-text", "qwen2.5:14b")
        self.assertTrue(result["ok"])
        self.assertIn("does not report embedding capability", result["message"])


class TestLLMProviderTestEndpoint(unittest.TestCase):
    def test_openai_requires_api_key(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            result = _test_llm_provider(
                True, "openai", "", "", "", "", "http://ollama:11434", "", ""
            )
        self.assertFalse(result["ok"])
        self.assertIn("Missing OpenAI API key", result["message"])

    def test_openai_compatible_accepts_no_api_key(self):
        payload = {"data": [{"id": "embed"}, {"id": "chat"}]}
        with patch("src.web_server.requests.get", return_value=_resp(200, payload)) as get:
            result = _test_llm_provider(
                True, "openai_compatible", "http://llama:8080/v1", "", "embed", "chat",
                "http://ollama:11434", "", ""
            )
        self.assertTrue(result["ok"])
        self.assertNotIn("Authorization", get.call_args.kwargs["headers"])

    def test_openai_compatible_reports_missing_models(self):
        payload = {"data": [{"id": "other"}]}
        with patch("src.web_server.requests.get", return_value=_resp(200, payload)):
            result = _test_llm_provider(
                True, "openai_compatible", "http://llama:8080/v1", "", "embed", "chat",
                "http://ollama:11434", "", ""
            )
        self.assertFalse(result["ok"])
        self.assertIn("not listed", result["message"])

    def test_ollama_provider_uses_legacy_probe(self):
        with patch("src.services.integration_connection_probes._test_ollama", return_value={"ok": True, "message": "ollama"}) as probe:
            result = _test_llm_provider(
                True, "ollama", "", "", "generic-embed", "generic-chat",
                "http://ollama:11434", "old-embed", "old-chat"
            )
        self.assertTrue(result["ok"])
        probe.assert_called_once_with(True, "http://ollama:11434", "generic-embed", "generic-chat")


class TestOllamaShowInfo(unittest.TestCase):
    def test_parses_context_length_and_capabilities(self):
        show = {
            "model_info": {"qwen2.context_length": 32768, "other": "x"},
            "capabilities": ["completion", "tools"],
        }
        with patch("src.web_server.requests.post", return_value=_resp(200, show)):
            info = _ollama_show_info("http://x", "qwen2.5:14b")
        self.assertEqual(info["context_length"], 32768)
        self.assertEqual(info["capabilities"], ["completion", "tools"])

    def test_non_200_returns_empty(self):
        with patch("src.web_server.requests.post", return_value=_resp(404)):
            info = _ollama_show_info("http://x", "qwen2.5:14b")
        self.assertIsNone(info["context_length"])
        self.assertEqual(info["capabilities"], [])


if __name__ == "__main__":
    unittest.main()
