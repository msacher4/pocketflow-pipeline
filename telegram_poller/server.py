#!/usr/bin/env python3
"""Telegram poller for PocketFlow — polls callbacks + messages, forwards to pipeline."""
import json
import os
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

BOT_TOKEN = os.getenv("PF_TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("PF_TG_CHAT_ID", "1155339708")
PIPELINE_URL = os.getenv("PF_PIPELINE_URL", "http://localhost:8766")
POLL_INTERVAL = 3
OFFSET_FILE = "/tmp/tg_poller_offset.txt"
HTTP_PORT = 48485


def send_telegram_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        body = json.loads(resp.read().decode())
        print(f"[tg-sender] Message sent to {chat_id}: {body.get('ok')}", flush=True)
        return body
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        print(f"[tg-sender] HTTP {e.code}: {body}", flush=True)
        return {"ok": False, "error": body}
    except Exception as e:
        print(f"[tg-sender] Error: {e}", flush=True)
        return {"ok": False, "error": str(e)}


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/status":
            self._respond(200, {"ok": True, "service": "tg-poller-pocketflow"})
        else:
            self._respond(404, {"ok": False, "error": "not found"})

    def _respond(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, fmt, *args):
        pass


def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"timeout": 10, "allowed_updates": json.dumps(["callback_query", "message"])}
    if offset:
        params["offset"] = offset
    qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
    try:
        resp = urllib.request.urlopen(f"{url}?{qs}", timeout=15)
        return json.loads(resp.read().decode()).get("result", [])
    except Exception as e:
        print(f"[tg-poller] getUpdates error: {e}", flush=True)
        return []


def answer_callback(callback_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    data = json.dumps({"callback_query_id": callback_id}).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=10)
    except Exception as e:
        print(f"[tg-poller] answerCallback error: {e}", flush=True)


def forward_callback(update):
    cb = update.get("callback_query", {})
    if not cb:
        return
    data = cb.get("data", "")
    if ":" not in data:
        print(f"[tg-poller] Unknown callback format: {data}", flush=True)
        answer_callback(cb.get("id"))
        return

    action, script_id = data.split(":", 1)
    if "_pf" not in script_id:
        print(f"[tg-poller] Non-pipeline callback ignored: {data}", flush=True)
        answer_callback(cb.get("id"))
        return

    body = json.dumps({
        "callback_query": {"data": data, "id": cb.get("id")}
    }).encode()
    req = urllib.request.Request(
        f"{PIPELINE_URL}/webhook/tg-callback",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"[tg-poller] Forwarded {action}:{script_id} to pipeline", flush=True)
        answer_callback(cb.get("id"))
    except Exception as e:
        print(f"[tg-poller] Pipeline error: {e}", flush=True)


def forward_message(update):
    """Forward text messages to pipeline for feedback handling."""
    msg = update.get("message", {})
    if not msg:
        return
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != TG_CHAT_ID:
        return
    text = msg.get("text", "")
    if not text:
        return
    message_id = msg.get("message_id")
    reply_to = msg.get("reply_to_message", {}).get("message_id")
    body = json.dumps({
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "reply_to_message_id": reply_to,
    }).encode()
    req = urllib.request.Request(
        f"{PIPELINE_URL}/webhook/tg-message",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"[tg-poller] Forwarded message {message_id} to pipeline", flush=True)
    except Exception as e:
        print(f"[tg-poller] Message forward error: {e}", flush=True)


def load_offset():
    try:
        with open(OFFSET_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def save_offset(offset):
    with open(OFFSET_FILE, "w") as f:
        f.write(str(offset))


def run_poller():
    while True:
        try:
            offset = load_offset()
            updates = get_updates(offset)
            for upd in updates:
                if "callback_query" in upd:
                    forward_callback(upd)
                elif "message" in upd:
                    forward_message(upd)
                new_offset = upd["update_id"] + 1
                save_offset(new_offset)
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[tg-poller] Loop error: {e}", flush=True)
            time.sleep(5)


def run_http():
    server = HTTPServer(("0.0.0.0", HTTP_PORT), StatusHandler)
    print(f"[tg-http] Listening on port {HTTP_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    print("[tg-poller] Starting Telegram poller (PocketFlow)", flush=True)
    Thread(target=run_poller, daemon=True).start()
    run_http()
