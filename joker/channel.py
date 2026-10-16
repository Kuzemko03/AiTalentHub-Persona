"""Owner-bound editorial queue. No private chat history is used as a source."""
import hashlib
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .backends import Cancelled, ModelError
from .telegram_bot import TelegramAPI, TelegramError
from .roles import ROLE_ID, visual_description

LOCAL_TZ = timezone(timedelta(hours=3), "Europe/Volgograd")
SECRET = re.compile(r"\b(?:\d{8,12}:[A-Za-z0-9_-]{30,50}|gh[pousr]_[A-Za-z0-9]{25,}|sk-[A-Za-z0-9_-]{25,})\b")
POST_SCHEMA = {"type": "object", "properties": {
    "title": {"type": "string"}, "text": {"type": "string"}, "image_scene": {"type": "string"},
    "novelty_key": {"type": "string"}, "mask": {"type": "string"},
    "kind": {"type": "string", "enum": ["fictional_scene", "public_event"]}, "event_id": {"type": "string"}},
    "required": ["title", "text", "image_scene", "novelty_key", "mask", "kind", "event_id"], "additionalProperties": False}
IMAGE_STYLE = (
    "Use case: photorealistic-natural. Candid square editorial photograph of a fictional adult. "
    "Natural visible human face, ordinary skin texture, believable hands, clothing and objects. "
    "Show the person doing the specific action in their own everyday surroundings. "
    "Use an attached reference ONLY if it depicts this SAME role; preserve its face and age. "
    "Different roles are different people. No blank white face, theatrical mask, hooded avatar, "
    "DC Joker, face paint, plastic CGI skin, text, logos or fake chat screenshots. "
    "This is an AI-created fictional photograph, not documentary evidence. Identity: "
)


class ChannelError(ValueError):
    pass


def public_text(value):
    if not isinstance(value, str) or SECRET.search(value):
        raise ChannelError("В публикации обнаружены недопустимые данные.")
    return value.strip()


def validate_post(value):
    if not isinstance(value, dict):
        raise ChannelError("Ожидается объект публикации.")
    result = {}
    for key, minimum, maximum in (("title", 3, 120), ("text", 100, 2800),
                                   ("image_scene", 20, 1600), ("novelty_key", 5, 160)):
        text = public_text(value.get(key))
        if not minimum <= len(text) <= maximum:
            raise ChannelError("Неверная длина поля публикации: " + key)
        result[key] = text
    if not isinstance(value.get("mask"), str) or not ROLE_ID.fullmatch(value["mask"]) or value.get("kind") not in ("fictional_scene", "public_event"):
        raise ChannelError("Неверная маска или источник публикации.")
    result.update(mask=value["mask"], kind=value["kind"], event_id=value.get("event_id", ""))
    if not isinstance(result["event_id"], str):
        raise ChannelError("Неверный источник события.")
    if "role_profile" in value:
        profile = value["role_profile"]
        if not isinstance(profile, dict):
            raise ChannelError("Неверный профиль автора.")
        result["role_profile"] = {key: public_text(profile.get(key, "")) for key in
                                  ("display_name", "role", "traits", "speech_style", "visual_description")}
        if any(len(v) > 1200 for v in result["role_profile"].values()):
            raise ChannelError("Профиль автора слишком длинный.")
    return result


class ChannelService:
    def __init__(self, app, api=None):
        self.app = app
        self.store = app.store
        self.api = api

    def telegram(self):
        if self.api is None:
            self.api = TelegramAPI(self.app.settings.telegram_token)
        return self.api

    def verify(self, target):
        api = self.telegram()
        chat = api.call("getChat", chat_id=target)
        if chat.get("type") != "channel":
            raise ChannelError("Это группа. Для ленты публикаций нужен именно Telegram-канал.")
        bot = api.call("getMe")
        rights = api.call("getChatMember", chat_id=chat["id"], user_id=bot["id"])
        owner = api.call("getChatMember", chat_id=chat["id"], user_id=self.app.settings.owner_id)
        if rights.get("status") != "administrator" or not rights.get("can_post_messages"):
            raise ChannelError("Добавьте бота администратором канала с правом публикации.")
        if owner.get("status") not in ("creator", "administrator"):
            raise ChannelError("Владелец бота должен управлять выбранным каналом.")
        return {"id": chat["id"], "title": chat.get("title", "Джокер"),
                "username": chat.get("username", "")}

    def bind(self, target):
        if isinstance(target, str):
            target = target.strip().replace("https://t.me/", "@").replace("t.me/", "@").rstrip("/")
            if target.lstrip("-").isdigit():
                target = int(target)
            elif not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", target):
                raise ChannelError("Укажите @имя канала или числовой ID.")
        info = self.verify(target)
        # Rebinding always pauses automation until the operator explicitly resumes it.
        with self.app.guard:
            self.store.set("channel_target", info)
            self.store.set("channel_enabled", False)
        return info

    def enable(self, enabled):
        if enabled:
            target = self.store.get("channel_target", {})
            if not target:
                raise ChannelError("Сначала подключите канал: /канал @имя.")
            self.verify(target["id"])
        self.store.set("channel_enabled", bool(enabled))

    def view(self, row):
        payload = json.loads(row["payload"])
        profile = payload.get("role_profile") or self.app.masks.get(payload["mask"], self.app.masks["joker"])
        return {"id": row["id"], "status": row["status"], "created_at": row["created_at"],
                "published_at": row["published_at"], "has_photo": bool(row["photo"]),
                "photo_url": "/api/channel/photo/" + row["id"] if row["photo"] else "",
                "photo_message_id": row["photo_message_id"], "text_message_id": row["text_message_id"],
                **payload, "author": profile["display_name"],
                "image_prompt": IMAGE_STYLE + visual_description(profile) + ". Scene: " + payload["image_scene"]}

    def status(self):
        rows = self.store.channel_posts(30)
        attention = self.store.channel_attention()
        return {"target": self.store.get("channel_target", {}), "enabled": self.store.get("channel_enabled", False),
                "schedule": "12:00 и 20:00 · Europe/Volgograd" if self.store.get("channel_enabled", False) else "Только ручная публикация", "attention": attention,
                "author": self.app.masks[self.app.active]["display_name"],
                "events_waiting": len(self.store.channel_events()),
                "posts": [self.view(p) for p in rows if p["status"] in ("draft", "publishing", "uncertain")][:8]}

    def add_event(self, text):
        text = public_text(text)
        if not 10 <= len(text) <= 2000:
            raise ChannelError("Публичное событие: от 10 до 2000 символов.")
        event_id = uuid.uuid4().hex[:12]
        self.store.add_channel_event(event_id, text)
        return event_id

    def save(self, payload, post_id=None):
        payload = validate_post(payload)
        if payload["mask"] not in self.app.masks:
            raise ChannelError("Роль автора не найдена.")
        if "role_profile" not in payload:
            mask = self.app.masks[payload["mask"]]
            payload["role_profile"] = {key: mask.get(key, "") for key in
                                       ("display_name", "role", "traits", "speech_style", "visual_description")}
            payload["role_profile"]["visual_description"] = visual_description(mask)
        if payload["kind"] == "public_event":
            event = next((e for e in self.store.channel_events() if e["id"] == payload["event_id"]), None)
            if not event:
                raise ChannelError("Для реального события нужен явно переданный публичный источник.")
        fingerprint = hashlib.sha256(payload["text"].casefold().encode()).hexdigest()
        for previous in self.store.channel_posts(50):
            data = json.loads(previous["payload"])
            if (fingerprint == hashlib.sha256(data["text"].casefold().encode()).hexdigest()
                    or payload["novelty_key"].casefold() == data["novelty_key"].casefold()):
                raise ChannelError("Такая история уже есть в журнале; нужен другой конфликт и финал.")
        post_id = post_id or uuid.uuid4().hex[:12]
        self.store.add_channel_post(post_id, payload, self.store.get("channel_target", {}).get("id", 0))
        return self.view(self.store.channel_post(post_id))

    def choose_author(self):
        preferred = ("plumber", "housewife", "driver", "cook", "engineer", "locksmith")
        pool = [key for key in (*preferred, *self.app.masks) if key in self.app.masks and key != "joker"]
        pool = list(dict.fromkeys(pool)) or ["joker"]
        recent = [json.loads(p["payload"])["mask"] for p in self.store.channel_posts(30)
                  if p["status"] in ("draft", "published", "publishing")]
        return min(pool, key=lambda key: (recent.count(key), -recent.index(key) if key in recent else -999))

    def generate(self, expected_revision=None, *, topic="", auto_role=False):
        topic = public_text(topic)
        if len(topic) > 1000:
            raise ChannelError("Тема черновика должна быть короче 1000 символов.")
        if not self.app.generation.acquire(blocking=False):
            raise ChannelError("Сейчас идёт разговор. Подготовку поста можно повторить позже.")
        cancel = threading.Event()
        call_id = None
        try:
            with self.app.guard:
                revision = self.store.get("response_revision", 0)
                if expected_revision is not None and revision != expected_revision:
                    raise Cancelled("Подготовка поста отменена.")
                self.app.active_cancel = cancel
                call_id = self.store.reserve_call(self.app.settings.daily_requests)
                role_id = self.choose_author() if auto_role else self.app.active
                author = dict(self.app.masks[role_id])
            root = self.app.settings.root
            recent = [json.loads(p["payload"]) for p in self.store.channel_posts(20)]
            events = self.store.channel_events()
            used_event_ids = {p.get("event_id") for p in recent}
            event = next((e for e in events if e["id"] not in used_event_ids), None)
            ideas = json.loads((root / "channel/ideas.json").read_text(encoding="utf-8"))
            seed = ideas[len(recent) % len(ideas)]
            system = ((root / "channel/EDITORIAL.md").read_text(encoding="utf-8") + "\n\n"
                      + (root / "agent/joker/PERSONA.md").read_text(encoding="utf-8")
                      + '\nТы пишешь пост, а не ответ собеседнику. Верни JSON с полями title, text, image_scene '
                      '(описание одной сцены по-английски), novelty_key (суть нового конфликта), '
                      'mask (id текущего автора), kind (fictional_scene/public_event), event_id. '
                      'Не добавляй заголовок и подпись внутрь text: приложение добавит их само. '
                      'Пиши только от лица active_role; поле mask должно совпадать с её id. '
                      'Не вставляй в историю Джокера, белую маску и других внутренних персонажей. '
                      'Тема и действия относятся к жизни и занятиям active_role. Не копируй примеры. '
                      'При отсутствии публичного события создай явно вымышленную сцену. '
                      'Внешние ссылки, чужие люди и новые технические факты не нужны. '
                      'Пост должен развиваться, а не повторять одну шутку пятью абзацами.')
            context = {"active_role": author, "requested_topic": topic, "public_event": event, "seed_for_inspiration": seed if not topic else None,
                       "recent_public_editorial_material": recent}
            result = self.app.backend.generate(system, [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}], cancel,
                                               schema=POST_SCHEMA)
            with self.app.guard:
                if cancel.is_set() or revision != self.store.get("response_revision", 0):
                    raise Cancelled("Подготовка поста отменена.")
                validate_post(result)
                self.store.finish_call(call_id, {"status": "done", "purpose": "channel_draft", "model": result["model"],
                                                "latency": result["latency"], "usage": result["usage"]})
                call_id = None
                call_id = self.store.reserve_call(self.app.settings.daily_requests)
            editing = (system + "\nСейчас отредактируй уже написанный черновик. Сохрани тему, автора и источник. "
                       "Найди самую искусственную реплику или метафору и замени обычной живой реакцией. "
                       "Убери объяснение шутки, повторы и сценки, в которых люди ведут себя нелепо только ради панчлайна. "
                       "Проверь, что бытовые действия имеют понятную причину и результат; не превращай ремонт в магическое 'покрутил и всё'. "
                       "Не добавляй новых реальных фактов. Подтяни начало, если оно общее, и закончи на точной детали. "
                       "Если фраза уже хорошая, сохрани её. Верни только улучшенный пост по той же JSON-схеме.")
            edit_context = {**context, "draft_to_edit": validate_post(result)}
            result = self.app.backend.generate(editing, [{"role": "user", "content": json.dumps(edit_context, ensure_ascii=False)}],
                                               cancel, schema=POST_SCHEMA)
            with self.app.guard:
                if cancel.is_set() or revision != self.store.get("response_revision", 0):
                    raise Cancelled("Подготовка поста отменена.")
                payload = validate_post(result)
                payload["mask"] = author["id"]
                payload["kind"] = "public_event" if event else "fictional_scene"
                payload["event_id"] = event["id"] if event else ""
                saved = self.save(payload)
                self.store.finish_call(call_id, {"status": "done", "purpose": "channel", "model": result["model"],
                                                "latency": result["latency"], "usage": result["usage"]})
                return saved
        except (ValueError, ModelError):
            if call_id is not None:
                self.store.finish_call(call_id, {"status": "failed", "purpose": "channel"})
            raise
        finally:
            with self.app.guard:
                if self.app.active_cancel is cancel:
                    self.app.active_cancel = None
            self.app.generation.release()

    def photo_path(self, row):
        if not row or not row["photo"]:
            raise ChannelError("У поста пока нет иллюстрации.")
        root = self.app.settings.root.resolve()
        path = (root / row["photo"]).resolve()
        allowed = ((root / "channel/assets").resolve(), (root / "data/channel/media").resolve())
        if not any(path.is_relative_to(folder) for folder in allowed):
            raise ChannelError("Изображения доступны только из папки иллюстраций канала.")
        if path.suffix.lower() not in (".png", ".jpg", ".jpeg") or not path.is_file() or not 0 < path.stat().st_size <= 10_000_000:
            raise ChannelError("Нужен PNG/JPEG размером до 10 МБ.")
        with path.open("rb") as f:
            magic = f.read(8)
        if not (magic == b'\x89PNG\r\n\x1a\n' or magic.startswith(b'\xff\xd8\xff')):
            raise ChannelError("Файл не является PNG/JPEG.")
        return path

    def attach(self, post_id, path):
        row = self.store.channel_post(post_id)
        if not row or row["status"] != "draft":
            raise ChannelError("Иллюстрацию можно менять только у черновика.")
        root = self.app.settings.root.resolve()
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(root):
            raise ChannelError("Сначала сохраните картинку в data/channel/media.")
        row["photo"] = str(resolved.relative_to(root))
        self.photo_path(row)
        self.store.update_channel_post(post_id, photo=row["photo"])

    def formatted(self, payload):
        label = "Вымышленная история" if payload["kind"] == "fictional_scene" else "Из дневника"
        profile = payload.get("role_profile") or self.app.masks[payload["mask"]]
        name = profile["display_name"]
        return payload["title"] + "\n\n" + payload["text"] + "\n\n" + name + " · " + label

    def publish(self, post_id, automatic=True):
        target = self.store.get("channel_target", {})
        if not target:
            raise ChannelError("Канал пока не подключён.")
        if automatic and not self.store.get("channel_enabled", False):
            raise ChannelError("Автопостинг выключен.")
        row = self.store.channel_post(post_id)
        if not row:
            raise ChannelError("Пост не найден.")
        if row["status"] == "published":
            return self.view(row)
        payload = validate_post(json.loads(row["payload"]))
        text = public_text(self.formatted(payload))
        if len(text.encode("utf-16-le")) // 2 > 4000:
            raise ChannelError("Пост слишком длинный для Telegram.")
        photo = self.photo_path(row) if row["photo"] else None
        self.verify(target["id"])
        if automatic and not self.store.get("channel_enabled", False):
            raise ChannelError("Автопостинг остановлен владельцем.")
        if self.store.get("channel_target", {}).get("id") != target["id"]:
            raise ChannelError("Канал изменился; публикация отменена.")
        if not self.store.claim_channel_post(post_id, target["id"]):
            raise ChannelError("Пост уже отправляется, закрыт или требует проверки доставки.")
        try:
            api = self.telegram()
            caption = text
            compact = bool(photo and len(caption.encode("utf-16-le")) // 2 <= 1024)
            if photo:
                name = payload.get("role_profile", self.app.masks[payload["mask"]])["display_name"]
                sent = api.upload_photo(target["id"], photo, caption if compact else payload["title"] + "\n\n" + name + " · Вымышленный образ")
                photo_id = sent["message_id"]
                self.store.update_channel_post(post_id, photo_message_id=photo_id)
                if not compact:
                    time.sleep(1.05)
            else:
                photo_id = None
            args = {"chat_id": target["id"], "text": text, "link_preview_options": {"is_disabled": True}}
            if not compact and automatic and (not self.store.get("channel_enabled", False)
                              or self.store.get("channel_target", {}).get("id") != target["id"]):
                raise ChannelError("Публикация остановлена владельцем.")
            if photo_id:
                args["reply_parameters"] = {"message_id": photo_id, "allow_sending_without_reply": True}
            if not compact:
                sent = api.call("sendMessage", **args)
            self.store.update_channel_post(post_id, text_message_id=sent["message_id"], status="published", published_at=self.store.now())
            if payload.get("event_id"):
                with self.store.lock, self.store.db:
                    self.store.db.execute("UPDATE channel_events SET used_by=? WHERE id=?", (post_id, payload["event_id"]))
            return self.view(self.store.channel_post(post_id))
        except Exception:
            # The remote side may have accepted a request before the connection failed.
            self.store.update_channel_post(post_id, status="uncertain", error="Доставку нужно проверить в Telegram; автоматический повтор отключён.")
            raise ChannelError("Не удалось подтвердить доставку. Проверьте канал; повтор автоматически не выполняется.") from None

    def discard(self, post_id):
        row = self.store.channel_post(post_id)
        if not row or row["status"] not in ("draft", "uncertain"):
            raise ChannelError("Закрыть можно черновик или публикацию с неподтверждённой доставкой.")
        self.store.update_channel_post(post_id, status="discarded")

    def next_draft(self, now=None):
        status = self.status()
        if not status["target"] or not status["enabled"]:
            return {"action": "idle", "reason": "Канал не подключён или автопостинг выключен"}
        if status["attention"]:
            return {"action": "needs_review", "posts": status["attention"]}
        now = (now or datetime.now(LOCAL_TZ)).astimezone(LOCAL_TZ)
        hour = 20 if now.hour >= 20 else 12 if now.hour >= 12 else None
        if hour is None or now.hour > hour + 2:
            return {"action": "idle", "reason": "Вне окна публикации"}
        slot = f"{status['target']['id']}/{now.date()}/{hour:02d}:00"
        existing = self.store.channel_slot(slot)
        if existing:
            row = self.store.channel_post(existing["post_id"]) if existing["post_id"] else None
            if row and row["status"] == "draft":
                return {"action": "prepare_image", "slot": slot, "post": self.view(row)}
            return {"action": "idle", "reason": "Этот выпуск уже обработан или готовится"}
        if not self.store.claim_channel_slot(slot):
            return {"action": "idle", "reason": "Другой процесс уже готовит выпуск"}
        try:
            drafts = [p for p in reversed(self.store.channel_posts(100)) if p["status"] == "draft"
                      and p["target"] in (0, status["target"]["id"])]
            post = self.view(drafts[0]) if drafts else self.generate()
            self.store.set_channel_slot(slot, post["id"], "ready")
            return {"action": "prepare_image", "slot": slot, "post": post}
        except (ValueError, ModelError, TelegramError):
            self.store.set_channel_slot(slot, "", "failed")
            raise

    def command(self, text):
        token, _, argument = text.strip().partition(" ")
        token = token.split("@", 1)[0].casefold()
        if token in ("/канал", "/channel"):
            if argument.strip():
                info = self.bind(argument.strip())
                return f"Канал «{info['title']}» подключён. /автопост вкл — начать; /автопост выкл — пауза."
            s = self.status()
            name = s["target"].get("title", "не подключён")
            return f"Канал: {name}. Автопостинг: {'включён' if s['enabled'] else 'выключен'}. {s['schedule']}."
        if token in ("/автопост", "/autopost"):
            if argument.strip().casefold() in ("вкл", "on"):
                self.enable(True)
                return "Автопостинг включён: 12:00 и 20:00, Волгоград. /автопост выкл — пауза."
            if argument.strip().casefold() in ("выкл", "off", "стоп"):
                self.enable(False)
                return "Автопостинг на паузе. Готовые посты и картинки сохранены."
            return self.command("/канал")
        if token in ("/событие", "/event"):
            event_id = self.add_event(argument)
            return "Событие разрешено для публичного дневника: " + event_id + ". Личные чаты туда автоматически не попадают."
        if token in ("/пост", "/post"):
            action, _, post_id = argument.partition(" ")
            if action.casefold() in ("новый", "new", "черновик", "draft", "авто", "auto"):
                view = self.generate(topic=post_id.strip(), auto_role=action.casefold() in ("авто", "auto"))
                return f"Черновик {view['id']} · не опубликован\n\n" + self.formatted(view)
            if action.casefold() in ("опубликовать", "publish"):
                p = self.publish(post_id.strip(), automatic=False)
                return "Пост опубликован: " + p["title"]
            if action.casefold() in ("убрать", "discard"):
                self.discard(post_id.strip())
                return "Пост закрыт; повторной отправки не будет."
            posts = self.store.channel_posts(10)
            draft = next((p for p in posts if p["status"] == "draft"), None)
            if not draft:
                return "Готовых черновиков пока нет. /пост авто тема — подготовить новый; /событие текст — дать материал."
            view = self.view(draft)
            return f"Черновик {view['id']}\n\n" + self.formatted(view) + f"\n\n/пост опубликовать {view['id']}"
        return None


def cli(arguments):
    import argparse
    from .config import Settings
    from .core import App
    parser = argparse.ArgumentParser(description="Редакция Telegram-канала Джокера")
    parser.add_argument("action", choices=("status", "bind", "on", "off", "draft", "next", "show", "attach", "publish", "import", "seed", "discard", "event", "comments"))
    parser.add_argument("value", nargs="?")
    parser.add_argument("--photo")
    parser.add_argument("--auto-role", action="store_true", help="выбрать следующего автора самостоятельно")
    parser.add_argument("--manual", action="store_true", help="явная ручная публикация при выключенном расписании")
    args = parser.parse_args(arguments)
    app = App(Settings.load())
    service = app.channel
    try:
        action = args.action
        if action == "status":
            result = service.status()
        elif action == "comments":
            if args.value in ("on", "off"):
                app.comments.enable(args.value == "on")
            app.comments.sync(service.telegram(), force=True)
            result = app.comments.status()
        elif action == "bind":
            if not args.value:
                parser.error("Укажите @имя канала")
            result = service.bind(args.value)
        elif action in ("on", "off"):
            service.enable(action == "on")
            result = {"enabled": action == "on"}
        elif action == "draft":
            result = service.generate(topic=args.value or "", auto_role=args.auto_role)
        elif action == "next":
            result = service.next_draft()
        elif action in ("show", "attach", "publish", "discard"):
            row = app.store.channel_post(args.value)
            if not row:
                raise ChannelError("Пост не найден.")
            if action == "attach":
                if not args.photo:
                    parser.error("Укажите --photo и путь к иллюстрации")
                service.attach(args.value, args.photo)
            elif action == "publish":
                service.publish(args.value, automatic=not args.manual)
            elif action == "discard":
                service.discard(args.value)
            result = service.view(app.store.channel_post(args.value))
        elif action == "event":
            result = {"event_id": service.add_event(args.value or "")}
        elif action == "import":
            if not args.value:
                parser.error("Укажите JSON-файл публикации")
            result = service.save(json.loads(Path(args.value).read_text(encoding="utf-8-sig")))
        else:
            result = []
            seed_path = app.settings.root / "channel/starter-posts.json"
            for item in json.loads(seed_path.read_text(encoding="utf-8")):
                post_id = item["id"]
                if app.store.channel_post(post_id):
                    continue
                service.save(item, post_id=post_id)
                asset = app.settings.root / item["asset"]
                service.attach(post_id, asset)
                result.append(service.view(app.store.channel_post(post_id)))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        app.store.close()
