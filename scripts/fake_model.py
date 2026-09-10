#!/usr/bin/env python3
"""A deterministic OpenAI-compatible endpoint for acceptance tests.

Hermes accepts any `base_url` plus `api_key`, so a real agent turn can be run
without credentials or spend. This answers chat completions with a fixed reply,
streamed as server-sent events when the client asks for a stream, which the
Hermes CLI does. That is enough to exercise the launcher, the provider
callbacks and the conversation database.

It stands in for a model only. Pubky and Hermes are never faked in an
acceptance test.

    python scripts/fake_model.py --port 8099 &
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLY = "Noted. I have recorded that in my memory."
MODEL = "fake/test-model"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # keep test output readable
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._json({"object": "list", "data": [
                {"id": MODEL, "object": "model", "owned_by": "hermes-pubky-tests"}]})
            return
        self._json({"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}
        model = body.get("model") or MODEL
        created = int(time.time())
        if body.get("stream"):
            self._stream(model, created)
            return
        self._json({
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": REPLY},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16},
        })

    def _stream(self, model: str, created: int) -> None:
        """The reply as SSE: content, then a finish, then [DONE]."""
        chunks = [
            {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
             "created": created, "model": model,
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": REPLY},
                          "finish_reason": None}]},
            {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
             "created": created, "model": model,
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16}},
        ]
        encoded = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        self._send(encoded.encode("utf-8"), "text/event-stream")

    def _json(self, payload: dict) -> None:
        self._send(json.dumps(payload).encode("utf-8"), "application/json")

    def _send(self, encoded: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class FakeModel:
    """Run the endpoint on a background thread."""

    def __init__(self, port: int = 0) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def device_config(self) -> dict:
        """The device-local settings that route a managed agent here.

        A bare "custom" provider is not routable; it must name a
        custom_providers entry, whose key_env supplies the api key.
        """
        return {
            "model": MODEL,
            "provider": "custom:faketest",
            "custom_providers": [{
                "name": "faketest", "base_url": self.base_url,
                "key_env": "FAKE_MODEL_KEY",
            }],
        }

    def __enter__(self) -> "FakeModel":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    with FakeModel(args.port) as model:
        print(f"fake model listening at {model.base_url}", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
