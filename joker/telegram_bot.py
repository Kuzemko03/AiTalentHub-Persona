"""Telegram long polling. One private owner, durable inbox and outbox."""
import json
import threading
import time
import urllib.error
import urllib.request
import uuid

from .backends import Cancelled, ModelError


class TelegramError(Exception):
    pass


class TelegramAPI:
    def __init__(self, token):
        if not token:
            raise TelegramError("Заполните TELEGRAM_BOT_TOKEN в .env.")
        self._base = "https://api.telegram.org/bot" + token + "/"

    def call(self, method, **payload):
        return self._request(method, json.dumps(payload).encode(), "application/json")

    def _request(self, method, body, content_type):
        request = urllib.request.Request(self._base + method,
                                         data=body, headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                data = json.load(response)
        except (urllib.error.URLError, ValueError, OSError):
            # urllib exceptions contain the token-bearing URL; never log their repr.
            raise TelegramError("Telegram недоступен или отклонил запрос. Проверьте токен и соединение.") from None
        if not isinstance(data, dict) or not data.get("ok") or "result" not in data:
            raise TelegramError("Telegram не подтвердил выполнение запроса.")
        return data["result"]

    def upload_photo(self, chat_id, path, caption):
        if not 0 < path.stat().st_size <= 10_000_000:
            raise TelegramError("Иллюстрация должна быть не больше 10 МБ.")
        boundary = "joker" + uuid.uuid4().hex
        parts = []
        for key, value in (("chat_id", str(chat_id)), ("caption", caption)):
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
        kind = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="illustration{path.suffix.lower()}"\r\n'
                      f'Content-Type: {kind}\r\n\r\n').encode())
        parts.extend((path.read_bytes(), f"\r\n--{boundary}--\r\n".encode()))
        result = self._request("sendPhoto", b"".join(parts), "multipart/form-data; boundary=" + boundary)
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise TelegramError("Telegram не подтвердил отправку иллюстрации.")
        return result

    def send(self, chat_id, text, keyboard=None):
        chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or ["…"]
        for i, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id, "text": chunk}
            if keyboard and i == len(chunks) - 1:
                payload["reply_markup"] = keyboard
            self.call("sendMessage", **payload)


def keyboard(app):
    masks = app.status()["masks"]
    rows = [[{"text": m["name"], "callback_data": "mask:" + m["id"]}]
            for m in masks[-12:] if m["id"] != "joker"]
    rows.append([{"text": "Джокер", "callback_data": "mask:joker"},
                 {"text": "Стоп", "callback_data": "stop"},
                 {"text": "Статус", "callback_data": "status"}])
    return {"inline_keyboard": rows}


def message_from_update(update):
    callback = update.get("callback_query")
    if callback:
        message = callback.get("message", {})
        data = callback.get("data", "")
        text = "/mask " + data[5:] if data.startswith("mask:") else {"stop": "/stop", "status": "/status"}.get(data, "/help")
        return message.get("chat", {}), callback.get("from", {}), text, callback.get("id")
    message = update.get("message", {})
    return message.get("chat", {}), message.get("from", {}), message.get("text", ""), None


def editorial_topic(text):
    token, _, argument = text.strip().partition(" ")
    if token.split("@", 1)[0].casefold() not in ("/пост", "/post"):
        return None
    action, _, topic = argument.partition(" ")
    return topic.strip() if action.casefold() in ("новый", "new", "черновик", "draft", "авто", "auto") else None


class TelegramRunner:
    def __init__(self, app, api=None):
        self.app = app
        self.api = api or TelegramAPI(app.settings.telegram_token)
        self.shutdown = threading.Event()
        self.delivery_lock = threading.RLock()
        self.app.telegram_status = {"state": "connecting", "detail": "Подключение Telegram"}

    def state(self, state, detail):
        self.app.telegram_status = {"state": state, "detail": detail}

    def accept(self, update):
        membership = update.get("my_chat_member")
        channel_post = update.get("channel_post")
        if membership or channel_post:
            chat = (membership or channel_post).get("chat", {})
            if chat.get("type") == "channel":
                known = self.app.store.get("channel_candidates", {})
                known[str(chat["id"])] = {"id": chat["id"], "title": chat.get("title", ""),
                    "username": chat.get("username", ""),
                    "added_by_owner": bool(membership and membership.get("from", {}).get("id") == self.app.settings.owner_id)}
                self.app.store.set("channel_candidates", dict(list(known.items())[-10:]))
            self.app.store.accept_update(update["update_id"], 0, "", "ignored")
            return
        chat, sender, text, callback_id = message_from_update(update)
        update_id = update["update_id"]
        if self.app.comments.accept(update):
            return
        if (chat.get("type") != "private" or sender.get("is_bot")
                or sender.get("id") != self.app.settings.owner_id or not text):
            self.app.store.accept_update(update_id, chat.get("id", 0), "", "ignored")
            return
        if callback_id:
            self.api.call("answerCallbackQuery", callback_query_id=callback_id)
        with self.delivery_lock:
            if not self.app.store.accept_update(update_id, chat["id"], text):
                return
            if len(text) > 4000:
                result = "Сообщение слишком длинное: до 4000 символов."
            else:
                try:
                    # Model work belongs in the worker; polling must remain available for Stop.
                    result = None if editorial_topic(text) is not None or self.app.needs_role_generation(text) else self.app.command(text)
                except ValueError as exc:
                    result = str(exc)
            if result is not None:
                self.app.store.job_status(update_id, "ready", result)

    def work_once(self):
        with self.app.guard:
            jobs = self.app.store.jobs("pending")
            revision = self.app.store.get("response_revision", 0)
            if jobs:
                self.app.store.job_status(jobs[0]["update_id"], "running")
        if jobs:
            job = jobs[0]
            try:
                self.api.call("sendChatAction", chat_id=job["chat_id"], action="typing")
            except TelegramError:
                pass  # This hint is optional; it must never strand a running job.
            try:
                topic = editorial_topic(job["text"])
                if self.app.needs_role_generation(job["text"]):
                    reply = self.app.command(job["text"], expected_revision=revision)
                    result = {"reply": reply, "_revision": self.app.store.get("response_revision", 0)}
                elif topic is None:
                    result = self.app.reply(job["text"], expected_revision=revision,
                                            request_id="telegram:" + str(job["update_id"]))
                else:
                    action = job["text"].strip().partition(" ")[2].partition(" ")[0].casefold()
                    draft = self.app.channel.generate(expected_revision=revision, topic=topic, auto_role=action in ("авто", "auto"))
                    result = {"reply": f"Черновик {draft['id']} · не опубликован\n\n" + self.app.channel.formatted(draft),
                              "_revision": revision}
                with self.delivery_lock:
                    self.app.store.job_status(job["update_id"], "ready", result["reply"], revision=result["_revision"])
            except Cancelled:
                self.app.store.job_status(job["update_id"], "cancelled")
            except (ModelError, ValueError) as exc:
                error_revision = getattr(exc, "response_revision", revision)
                self.app.store.job_status(job["update_id"], "ready", str(exc), type(exc).__name__, revision=error_revision)
        self.deliver()
        if not jobs:
            self.app.comments.sync(self.api)
            self.app.comments.work_once(self.api)

    def deliver(self):
        with self.delivery_lock:
            for job in self.app.store.jobs("ready"):
                if job["revision"] is not None and job["revision"] != self.app.store.get("response_revision", 0):
                    self.app.store.job_status(job["update_id"], "cancelled")
                    continue
                self.api.send(job["chat_id"], job["reply"], keyboard(self.app))
                self.app.store.job_status(job["update_id"], "sent", job["reply"], job["error"])
                self.app.experience.record("owner", "telegram", "reply.sent",
                    {"update_id": job["update_id"]}, "owner:telegram:" + str(job["update_id"]))

    def worker(self):
        while not self.shutdown.is_set():
            try:
                self.work_once()
            except TelegramError:
                print("Telegram: доставка отложена, повтор через 5 секунд.", flush=True)
                self.shutdown.wait(5)
            except Exception as exc:
                # A dead worker must be visible; never log exception details with user data.
                self.state("failed", "Обработчик Telegram остановлен; требуется перезапуск")
                print(f"Telegram: обработчик остановлен ({type(exc).__name__}).", flush=True)
                self.shutdown.set()
            self.shutdown.wait(0.2)

    def run(self):
        if not self.app.settings.owner_id:
            raise TelegramError("Сначала выполните python -m joker pair и задайте TELEGRAM_OWNER_ID.")
        while not self.shutdown.is_set():
            try:
                info = self.api.call("getMe")
                webhook = self.api.call("getWebhookInfo")
                break
            except TelegramError:
                self.state("retrying", "Нет связи с Telegram; повтор через 5 секунд")
                self.shutdown.wait(5)
        else:
            return
        if webhook.get("url"):
            self.state("failed", "У бота настроен webhook; polling не запущен")
            raise TelegramError("У бота настроен webhook. Выберите отдельного бота или отключите его самостоятельно.")
        print(f"Telegram подключён: @{info['username']}; доступ только владельцу.", flush=True)
        self.state("connected", "@" + info["username"])
        self.app.store.recover_jobs()
        thread = threading.Thread(target=self.worker, daemon=True)
        thread.start()
        try:
            while not self.shutdown.is_set():
                try:
                    updates = self.api.call("getUpdates", offset=self.app.store.get("telegram_offset", 0),
                                            timeout=20, allowed_updates=["message", "callback_query", "my_chat_member", "channel_post"])
                    if self.shutdown.is_set():
                        break
                    for update in updates:
                        self.accept(update)
                    self.deliver()
                    self.state("connected", "@" + info["username"])
                except TelegramError:
                    self.state("retrying", "Нет связи с Telegram; повтор через 5 секунд")
                    print("Telegram: соединение потеряно, повтор через 5 секунд.", flush=True)
                    self.shutdown.wait(5)
        finally:
            self.shutdown.set()
            self.app.runtime.close()
            self.app.cancel_reply()
            self.app.minecraft.stop_task()
            thread.join(timeout=self.app.settings.timeout + 2)
            if self.app.telegram_status["state"] != "failed":
                self.state("stopped", "Telegram остановлен")


def pair(settings):
    """Show candidate IDs without claiming the first person who contacts the bot."""
    api = TelegramAPI(settings.telegram_token)
    info = api.call("getMe")
    print(f"Бот: @{info['username']}. Напишите ему /start, затем повторите эту команду.")
    if api.call("getWebhookInfo").get("url"):
        raise TelegramError("У бота настроен webhook; привязка через polling недоступна.")
    users = {}
    for update in api.call("getUpdates", timeout=0, allowed_updates=["message", "callback_query"]):
        chat, sender, text, _ = message_from_update(update)
        if chat.get("type") == "private" and sender.get("id") and not sender.get("is_bot"):
            users[sender["id"]] = sender.get("username", "без username")
    for user_id, username in users.items():
        print(f"Кандидат: @{username}; TELEGRAM_OWNER_ID={user_id}")
    if not users:
        print("Входящих сообщений пока нет. Автоматическая привязка первого пользователя отключена.")
