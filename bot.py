import json
import os
import re
import secrets
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

WINDOW = 48 * 3600
MAX_COUNT = 5000


class TelegramError(Exception):
    def __init__(self, response):
        self.code = response.get("error_code")
        self.retry_after = response.get("parameters", {}).get("retry_after")


class Bot:
    def __init__(self, token, db_path):
        self.url = f"https://api.telegram.org/bot{token}/"
        self.db = sqlite3.connect(db_path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                chat INTEGER, id INTEGER, author INTEGER, sent INTEGER,
                PRIMARY KEY (chat, id)
            );
            CREATE INDEX IF NOT EXISTS by_author ON messages (chat, author, sent DESC, id DESC);
            CREATE INDEX IF NOT EXISTS by_time ON messages (sent);
        """)
        self.pending = {}
        self.me = self.call("getMe")
        self.last_prune = 0

    def call(self, method, **data):
        for attempt in range(3):
            request = Request(
                self.url + method, json.dumps(data).encode(),
                {"Content-Type": "application/json"},
            )
            try:
                with urlopen(request, timeout=40) as response:
                    result = json.load(response)
            except HTTPError as error:
                try:
                    result = json.load(error)
                except ValueError:
                    raise TelegramError({"error_code": error.code}) from None
            except (URLError, TimeoutError):
                raise TelegramError({"error_code": "network"}) from None

            if result.get("ok"):
                return result["result"]
            error = TelegramError(result)
            if not error.retry_after or error.retry_after > 60 or attempt == 2:
                raise error
            time.sleep(error.retry_after)

    def record(self, chat, message):
        author = message.get("from")
        if not author or message.get("sender_chat") or message["date"] <= time.time() - WINDOW:
            return
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?)",
                            (chat, message["message_id"], author["id"], message["date"]))

    def can_delete(self, chat, user, group_type):
        member = self.call("getChatMember", chat_id=chat, user_id=user)
        return member["status"] == "creator" or (
            member["status"] == "administrator" and
            (group_type == "group" or member.get("can_delete_messages", False))
        )

    def send(self, chat, text, markup=None, topic=None):
        data = {"chat_id": chat, "text": text}
        if markup:
            data["reply_markup"] = markup
        if topic:
            data["message_thread_id"] = topic
        self.call("sendMessage", **data)

    def handle_message(self, message):
        group = message["chat"]
        if group["type"] not in ("group", "supergroup"):
            return
        chat = group["id"]
        self.record(chat, message)
        body = message.get("text", "").split()
        if not body:
            return
        command, _, username = body[0].lower().partition("@")
        if command != "/purge" or (username and username != self.me["username"].lower()):
            return
        if message.get("sender_chat") or "from" not in message:
            return
        admin = message["from"]["id"]
        topic = message.get("message_thread_id")
        if not self.can_delete(chat, self.me["id"], group["type"]):
            self.send(chat, "Выдай боту право удалять сообщения.", topic=topic)
            return
        if not self.can_delete(chat, admin, group["type"]):
            return
        target = message.get("reply_to_message", {})
        author = target.get("from")
        query = parse_query(body[1:])
        if not query or not author or target.get("sender_chat"):
            self.send(chat, "Ответь на сообщение участника командой /purge 5000 "
                            "или /purge 6ч. Период от 1 минуты до 48 часов. "
                            "По количеству можно выбрать от 1 до 5000.", topic=topic)
            return
        seconds, count = query
        if target.get("chat", {}).get("id") == chat:
            self.record(chat, target)
        rows = self.db.execute(
            """SELECT id, sent FROM messages
               WHERE chat = ? AND author = ? AND sent > ? AND id < ?
               ORDER BY sent DESC, id DESC LIMIT ?""",
            (chat, author["id"], time.time() - seconds, message["message_id"], count),
        ).fetchall()
        if not rows:
            self.send(chat, "Доступных сообщений этого автора за выбранный период нет.", topic=topic)
            return
        key = "purge:" + secrets.token_urlsafe(12)
        self.pending[key] = (chat, admin, time.time() + 120, rows)
        name = author.get("first_name", "")[:100]
        self.send(chat, f"Удалить найденные сообщения автора {name} (ID {author['id']})?\n"
                        f"В очереди: {len(rows)}. Поиск охватывает весь чат.",
                  {"inline_keyboard": [[{"text": "Удалить", "callback_data": key}]]}, topic)

    def handle_callback(self, callback):
        key = callback.get("data", "")
        if not key.startswith("purge:"):
            return
        query_id = callback["id"]
        pending = self.pending.get(key)
        if not pending:
            self.call("answerCallbackQuery", callback_query_id=query_id, text="Запрос истёк.")
            return
        chat, admin, expires, rows = pending
        message = callback.get("message", {})
        group = message.get("chat", {})
        if (group.get("id") != chat or callback["from"]["id"] != admin or
                not self.can_delete(chat, admin, group["type"])):
            self.call("answerCallbackQuery", callback_query_id=query_id,
                      text="Подтвердить может администратор, который запустил команду.")
            return
        if expires <= time.time() or not self.can_delete(chat, self.me["id"], group["type"]):
            self.call("answerCallbackQuery", callback_query_id=query_id,
                      text="Запрос истёк или у бота больше нет прав.")
            return
        self.call("answerCallbackQuery", callback_query_id=query_id, text="Удаляю сообщения…")
        del self.pending[key]
        processed = 0
        try:
            for start in range(0, len(rows), 100):
                cutoff = time.time() - WINDOW
                ids = [mid for mid, sent in rows[start:start + 100] if sent > cutoff]
                processed += self.delete_batch(chat, ids)
        except TelegramError as error:
            print(f"Удаление прервано. Код API {error.code}", flush=True)
        self.call("editMessageText", chat_id=chat, message_id=message["message_id"],
                  text=f"Telegram принял удаление для {processed} из {len(rows)} ID. "
                       "В это число могут входить сообщения, удалённые ранее.",
                  reply_markup={"inline_keyboard": []})

    def delete_batch(self, chat, ids):
        if not ids:
            return 0
        try:
            self.call("deleteMessages", chat_id=chat, message_ids=ids)
        except TelegramError as error:
            if error.code != 400:
                raise
            if len(ids) == 1:
                return 0
            middle = len(ids) // 2
            return self.delete_batch(chat, ids[:middle]) + self.delete_batch(chat, ids[middle:])
        with self.db:
            self.db.executemany("DELETE FROM messages WHERE chat = ? AND id = ?",
                                ((chat, mid) for mid in ids))
        return len(ids)

    def run(self):
        offset = 0
        print("Бот запущен", flush=True)
        while True:
            now = time.time()
            if now - self.last_prune >= 60:
                with self.db:
                    self.db.execute("DELETE FROM messages WHERE sent <= ?", (now - WINDOW,))
                self.pending = {key: value for key, value in self.pending.items() if value[2] > now}
                self.last_prune = now
            try:
                updates = self.call("getUpdates", offset=offset, timeout=25,
                                    allowed_updates=["message", "callback_query"])
            except TelegramError as error:
                print(f"Ошибка получения обновлений. Код API {error.code}", flush=True)
                time.sleep(5)
                continue
            for update in updates:
                try:
                    if "message" in update:
                        self.handle_message(update["message"])
                    elif "callback_query" in update:
                        self.handle_callback(update["callback_query"])
                except TelegramError as error:
                    print(f"Ошибка обработки. Код API {error.code}", flush=True)
                offset = update["update_id"] + 1


def parse_query(args):
    if len(args) != 1:
        return None
    raw = args[0].lower()
    match = re.fullmatch(r"([0-9]{1,4})([mhdмчд]?)", raw)
    if not match:
        return None
    number = int(match[1])
    unit = match[2]
    if not unit:
        return (WINDOW, number) if 1 <= number <= MAX_COUNT else None
    scale = {"m": 60, "м": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400}
    seconds = number * scale[unit]
    return (seconds, -1) if 0 < seconds <= WINDOW else None


if __name__ == "__main__":
    token = os.environ.get("9260663764:AAGBK2mVgNo-UmKo60YMQnZryKtASiNYvD4", "").strip()
    if not token:
        raise SystemExit("Укажи BOT_TOKEN в переменных окружения.")
    try:
        Bot(token, os.environ.get("DB_PATH", "messages.sqlite3")).run()
    except TelegramError as error:
        raise SystemExit(f"Бот не запустился. Код API {error.code}") from None
