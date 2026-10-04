"""Notification channels. Configure any mix; all configured ones fire.

- ntfy:     free push to your phone (ntfy app, subscribe to your topic).
            The notification's tap action opens the booking link.
- Telegram: bot token from @BotFather + your chat id.
- command:  any shell command; gets BBT_TITLE / BBT_MESSAGE / BBT_URL in its
            environment (e.g. `notify-send "$BBT_TITLE" "$BBT_MESSAGE"`).
"""
from __future__ import annotations

import os
import subprocess

import requests


def channels(cfg: dict) -> list[str]:
    n = cfg.get("notify", {})
    out = []
    if n.get("ntfy_topic"):
        out.append("ntfy")
    if n.get("telegram_token") and n.get("telegram_chat_id"):
        out.append("telegram")
    if n.get("command"):
        out.append("command")
    return out


def send(cfg: dict, title: str, message: str, url: str | None = None,
         priority: int = 4) -> list[str]:
    """Send to every configured channel. Returns a list of error strings
    (empty = all good). Never raises: a dead channel must not kill the run."""
    n = cfg.get("notify", {})
    errors = []

    if n.get("ntfy_topic"):
        payload = {"topic": n["ntfy_topic"], "title": title, "message": message,
                   "priority": priority, "tags": ["train"]}
        if url:
            payload["click"] = url
            payload["actions"] = [{"action": "view", "label": "Book now",
                                   "url": url, "clear": True}]
        try:
            # JSON publishing to the server root: UTF-8 safe (headers aren't).
            r = requests.post(n.get("ntfy_server") or "https://ntfy.sh",
                              json=payload, timeout=15)
            r.raise_for_status()
        except Exception as e:
            errors.append(f"ntfy: {e}")

    if n.get("telegram_token") and n.get("telegram_chat_id"):
        text = f"{title}\n{message}" + (f"\n{url}" if url else "")
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{n['telegram_token']}/sendMessage",
                json={"chat_id": n["telegram_chat_id"], "text": text,
                      "disable_web_page_preview": True}, timeout=15)
            r.raise_for_status()
        except Exception as e:
            # Don't echo the URL: it contains the bot token.
            errors.append(f"telegram: {type(e).__name__}")

    if n.get("command"):
        env = dict(os.environ, BBT_TITLE=title, BBT_MESSAGE=message,
                   BBT_URL=url or "")
        try:
            subprocess.run(n["command"], shell=True, env=env, timeout=30,
                           check=True)
        except Exception as e:
            errors.append(f"command: {e}")

    return errors
