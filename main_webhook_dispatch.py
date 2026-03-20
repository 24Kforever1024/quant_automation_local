from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from config import get_settings
from services.watchlist_price_init_webhook_service import WatchlistPriceInitWebhookService


class _WebhookHandler(BaseHTTPRequestHandler):
    service = WatchlistPriceInitWebhookService()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
            status, body = self.service.handle_event(payload, headers=dict(self.headers.items()))
        except PermissionError as exc:
            status, body = 401, {"ok": False, "error": str(exc)}
        except ValueError as exc:
            status, body = 400, {"ok": False, "error": str(exc)}
        except Exception as exc:  # pragma: no cover
            status, body = 500, {"ok": False, "error": str(exc)}

        response_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


if __name__ == "__main__":
    settings = get_settings()
    server = ThreadingHTTPServer((settings.webhook_host, settings.webhook_port), _WebhookHandler)
    print(f"Webhook server listening on {settings.webhook_host}:{settings.webhook_port}")
    server.serve_forever()
