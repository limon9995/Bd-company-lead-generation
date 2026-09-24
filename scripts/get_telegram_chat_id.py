#!/usr/bin/env python3
"""Find your Telegram chat ID for the notification bot.

1. Create a bot with @BotFather and copy its token.
2. Open the bot in Telegram and send it /start (for a group: add the bot to the group and send a message).
3. Run:  python scripts/get_telegram_chat_id.py --token 123456:ABC...   [--send-test]

Uses only the Python standard library, so it runs anywhere (no project install needed).
"""
import argparse
import json
import sys
import urllib.error
import urllib.request


def call(token: str, method: str, params: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.load(r)
    except urllib.error.HTTPError as exc:
        body = json.load(exc)
    if not body.get("ok"):
        raise SystemExit(f"Telegram error on {method}: {body.get('description')}")
    return body["result"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token", required=True, help="bot token from @BotFather")
    ap.add_argument("--send-test", action="store_true", help="send a test message to each chat found")
    args = ap.parse_args()

    me = call(args.token, "getMe")
    print(f"Bot: @{me.get('username')} (id {me.get('id')})")
    try:
        updates = call(args.token, "getUpdates", {"timeout": 0})
    except SystemExit as exc:
        if "webhook" in str(exc).lower():
            raise SystemExit("This bot has a webhook set, so getUpdates is disabled. Remove it with "
                             f"https://api.telegram.org/bot<TOKEN>/deleteWebhook and try again.") from exc
        raise
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            chats[chat["id"]] = chat
    if not chats:
        print("\nNo chats found. Send /start to the bot in Telegram (or a message in the group), then run again.")
        sys.exit(1)
    print("\nChats that messaged this bot:")
    for cid, chat in chats.items():
        name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
        print(f"  chat_id = {cid:<16} type = {chat.get('type'):<10} name = {name}  @{chat.get('username', '')}")
        if args.send_test:
            call(args.token, "sendMessage", {"chat_id": cid, "text": "✅ Chat ID detected. Notifications will arrive here."})
    print("\nPaste the chat_id into Admin → Settings → Telegram → Chat ID (or use the 'Detect chat ID' button there).")


if __name__ == "__main__":
    main()
