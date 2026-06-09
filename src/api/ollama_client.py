"""Client for a local Ollama server (embeddings + chat judge).

All methods degrade gracefully: on any connectivity/parse failure they log once
and return None (or an empty list), so callers can fall back to existing behavior.
"""

import json
import logging
import math
import os
import time
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

# Judge responses are tiny JSON objects; cap generation so a confused model
# can't stream tokens until the request timeout.
_JUDGE_NUM_PREDICT = 200


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two equal-length vectors. Returns 0.0 on bad input."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


class OllamaClient:
    """Thin wrapper over the Ollama HTTP API used for optional, gated enhancements."""

    def __init__(self):
        self.session = requests.Session()
        self._embed_endpoint_missing = False  # set True if /api/embed 404s once
        self._schema_format_unsupported = False  # set True if a schema `format` 400s once

    # --- configuration (read live from os.environ, like other clients) ---

    @property
    def base_url(self) -> str:
        return os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")

    @property
    def embed_model(self) -> str:
        return os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")

    @property
    def chat_model(self) -> str:
        return os.environ.get("OLLAMA_CHAT_MODEL", "qwen2.5:14b")

    @property
    def keep_alive(self) -> str:
        return os.environ.get("OLLAMA_KEEP_ALIVE", "5m").strip()

    def is_configured(self) -> bool:
        return (
            os.environ.get("OLLAMA_ENABLED", "false").lower() == "true"
            and bool(self.base_url)
        )

    def _chat_options(self) -> dict:
        options = {"temperature": 0.0, "num_predict": _JUDGE_NUM_PREDICT}
        raw_ctx = os.environ.get("OLLAMA_NUM_CTX", "").strip()
        if raw_ctx:
            try:
                options["num_ctx"] = int(raw_ctx)
            except ValueError:
                logger.warning(f"Ignoring non-integer OLLAMA_NUM_CTX: {raw_ctx!r}")
        return options

    def _with_keep_alive(self, payload: dict) -> dict:
        if self.keep_alive:
            payload["keep_alive"] = self.keep_alive
        return payload

    def _post(self, url: str, payload: dict, timeout: int) -> requests.Response:
        """POST with a single retry on transient connection errors (not timeouts)."""
        try:
            return self.session.post(url, json=payload, timeout=timeout)
        except requests.exceptions.ConnectionError:
            time.sleep(0.5)
            return self.session.post(url, json=payload, timeout=timeout)

    # --- model discovery (also powers the settings Test button) ---

    def list_models(self) -> List[str]:
        """Return the names of locally pulled models, or [] on failure."""
        try:
            r = self.session.get(f"{self.base_url}/api/tags", timeout=5)
            if r.status_code != 200:
                return []
            data = r.json() or {}
            return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        except Exception as e:
            logger.warning(f"Ollama list_models failed: {e}")
            return []

    # --- embeddings ---

    def embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        """Embed a batch of texts. Returns one vector per input, or None on failure."""
        if not self.is_configured() or not texts:
            return None

        if not self._embed_endpoint_missing:
            vectors = self._embed_batch(texts)
            if vectors is not None:
                return vectors

        # Fallback for older Ollama builds without /api/embed.
        return self._embed_legacy(texts)

    def embed_one(self, text: str) -> Optional[List[float]]:
        vectors = self.embed([text])
        if vectors and len(vectors) == 1:
            return vectors[0]
        return None

    def _embed_batch(self, texts: List[str]) -> Optional[List[List[float]]]:
        try:
            r = self._post(
                f"{self.base_url}/api/embed",
                self._with_keep_alive({"model": self.embed_model, "input": texts}),
                timeout=60,
            )
            if r.status_code == 404:
                self._embed_endpoint_missing = True
                return None
            if r.status_code != 200:
                logger.warning(f"Ollama /api/embed returned {r.status_code}")
                return None
            embeddings = (r.json() or {}).get("embeddings")
            if isinstance(embeddings, list) and len(embeddings) == len(texts):
                return embeddings
            logger.warning("Ollama /api/embed returned unexpected payload shape")
            return None
        except Exception as e:
            logger.warning(f"Ollama /api/embed failed: {e}")
            return None

    def _embed_legacy(self, texts: List[str]) -> Optional[List[List[float]]]:
        vectors: List[List[float]] = []
        try:
            for text in texts:
                r = self.session.post(
                    f"{self.base_url}/api/embeddings",
                    json=self._with_keep_alive({"model": self.embed_model, "prompt": text}),
                    timeout=60,
                )
                if r.status_code != 200:
                    logger.warning(f"Ollama /api/embeddings returned {r.status_code}")
                    return None
                vec = (r.json() or {}).get("embedding")
                if not isinstance(vec, list):
                    logger.warning("Ollama /api/embeddings returned no embedding")
                    return None
                vectors.append(vec)
            return vectors
        except Exception as e:
            logger.warning(f"Ollama /api/embeddings failed: {e}")
            return None

    # --- chat judge ---

    def _post_chat(self, prompt: str, schema: Optional[dict]) -> requests.Response:
        return self._post(
            f"{self.base_url}/api/chat",
            self._with_keep_alive({
                "model": self.chat_model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "format": schema if schema else "json",
                "options": self._chat_options(),
            }),
            timeout=120,
        )

    def judge(self, prompt: str, schema: Optional[dict] = None) -> Optional[dict]:
        """Run a JSON-mode chat completion and return the parsed object, or None.

        When `schema` is given it is sent as a structured-output `format` (Ollama
        >= 0.5); older servers reject that with a 400, in which case we fall back
        to plain JSON mode for the rest of the process lifetime.
        """
        if not self.is_configured() or not prompt:
            return None
        use_schema = schema if (schema and not self._schema_format_unsupported) else None
        try:
            r = self._post_chat(prompt, use_schema)
            if r.status_code == 400 and use_schema is not None:
                self._schema_format_unsupported = True
                logger.info("Ollama rejected schema format; falling back to JSON mode")
                r = self._post_chat(prompt, None)
            if r.status_code != 200:
                logger.warning(f"Ollama /api/chat returned {r.status_code}")
                return None
            content = ((r.json() or {}).get("message") or {}).get("content", "")
            if not content:
                return None
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return parsed
            logger.warning("Ollama judge returned non-object JSON")
            return None
        except Exception as e:
            logger.warning(f"Ollama judge failed: {e}")
            return None
