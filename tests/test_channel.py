import base64
import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from joker.backends import Cancelled, CodexBackend
from joker.channel import ChannelError, LOCAL_TZ, POST_SCHEMA
from joker.config import ROOT, Settings
from joker.core import App
from joker.telegram_bot import TelegramAPI, TelegramError, TelegramRunner


def payload(key="one"):
    return {"title": "Один автор и три карандаша", "text": "Я попытался написать заметку о мастерской. " * 32,
            "image_scene": "One masked character at a workshop desk with three different pencils.",
            "novelty_key": "Конфликт карандашей " + key, "mask": "joker", "kind": "fictional_scene", "event_id": ""}


class EditorialBackend:
    def generate(self, system, messages, cancel, **_):
        self.system, self.messages = system, messages
        return {**payload(), "model": "test-fixture", "usage": {}, "latency": 0.01}


class ChannelAPI:
    def __init__(self):
        self.type = "channel"
        self.owner = "creator"
        self.can_post = True
        self.sent = []
        self.fail_text = False
        self.on_photo = None

    def call(self, method, **kw):
        if method == "getMe":
            return {"id": 9}
        if method == "getChat":
            return {"id": int(kw["chat_id"]) if isinstance(kw["chat_id"], int) else -100555,
                    "type": self.type, "title": "Тестовый канал", "username": "test_fixture"}
        if method == "getChatMember":
            return {"status": "administrator", "can_post_messages": self.can_post} if kw["user_id"] == 9 else {"status": self.owner}
        if method == "sendMessage":
            self.sent.append(("text", kw))
            if self.fail_text:
                raise TelegramError("Network failure")
            return {"message_id": 42}
        raise AssertionError(method)

    def upload_photo(self, chat_id, path, caption):
        self.sent.append(("photo", {"chat_id": chat_id, "path": str(path), "caption": caption}))
        if self.on_photo:
            self.on_photo()
        return {"message_id": 41}


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / "agent", self.root / "agent")
        (self.root / "channel/assets").mkdir(parents=True)
        for name in ("EDITORIAL.md", "ideas.json"):
            shutil.copy(ROOT / "channel" / name, self.root / "channel" / name)
        self.photo = self.root / "channel/assets/test.png"
        self.photo.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="))
        self.backend = EditorialBackend()
        self.app = App(Settings(root=self.root, owner_id=123), self.backend)
        self.service = self.app.channel
        self.api = ChannelAPI()
        self.service.api = self.api
        self.service.bind("@test_fixture")
        self.service.enable(True)

    def tearDown(self):
        self.app.store.close()
        self.temp.cleanup()

    def draft(self, key="one", photo=False):
        value = payload(key)
        value["text"] += key
        post = self.service.save(value)
        if photo:
            self.service.attach(post["id"], self.photo)
        return post

    def test_editorial_uses_its_own_structured_schema_in_codex(self):
        process = Mock(returncode=0)
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(payload())}}]
        process.communicate.return_value = ("\n".join(json.dumps(e) for e in events), "")
        captured = []

        def launch(command, **kwargs):
            captured.append(json.loads(Path(command[command.index("--output-schema") + 1]).read_text()))
            return process

        self.app.settings.codex_bin = "codex"
        with patch("joker.backends.subprocess.Popen", side_effect=launch):
            result = CodexBackend(self.app.settings).generate("Редактор", [], threading.Event(), schema=POST_SCHEMA)
        self.assertEqual(captured, [POST_SCHEMA])
        self.assertEqual(result["text"], payload()["text"])
        self.assertNotIn("reply", result)

    def test_upload_sends_real_multipart_and_checks_acknowledgement(self):
        api = TelegramAPI("test-fixture")
        with patch.object(api, "_request", return_value={"message_id": 17}) as request:
            self.assertEqual(api.upload_photo(-100555, self.photo, "Русская подпись"), {"message_id": 17})
        method, body, content_type = request.call_args.args
        self.assertEqual(method, "sendPhoto")
        self.assertIn(b'name="chat_id"\r\n\r\n-100555', body)
        self.assertIn("Русская подпись".encode(), body)
        self.assertIn(self.photo.read_bytes(), body)
        self.assertIn("multipart/form-data; boundary=", content_type)
        with patch.object(api, "_request", return_value={}):
            with self.assertRaises(TelegramError):
                api.upload_photo(-100555, self.photo, "Подпись")

    def test_old_uncertain_post_blocks_schedule_even_after_many_new_drafts(self):
        blocked = self.draft()
        self.app.store.update_channel_post(blocked["id"], status="uncertain")
        for number in range(32):
            self.draft(str(number))
        self.assertEqual(self.service.next_draft()["action"], "needs_review")

    def test_requested_topic_creates_only_a_draft_while_automation_is_off(self):
        self.service.enable(False)
        result = self.service.command("/пост новый Одна кнопка с четырьмя названиями")
        self.assertIn("не опубликован", result)
        context = json.loads(self.backend.messages[0]["content"])
        self.assertEqual(context["requested_topic"], "Одна кнопка с четырьмя названиями")
        self.assertIsNone(context["seed_for_inspiration"])
        self.assertEqual(self.service.status()["posts"][0]["status"], "draft")
        self.assertFalse(self.api.sent)
        self.assertFalse(self.service.status()["enabled"])
        self.assertEqual(self.service.next_draft()["action"], "idle")

    def test_post_and_photo_keep_the_same_dynamic_role_after_switch(self):
        self.app.command("/newmask Марина | сварщица | спокойная | коротко")
        role_id = self.app.active
        post = self.service.generate(topic="Про забытый инструмент")
        self.assertEqual(post["mask"], role_id)
        self.assertEqual(json.loads(self.backend.messages[0]["content"])["active_role"]["display_name"], "Марина")
        self.app.switch("housewife")
        saved = self.service.view(self.app.store.channel_post(post["id"]))
        self.assertEqual(saved["author"], "Марина")
        self.assertIn("сварщица", saved["image_prompt"])
        self.assertIn("Natural visible human face", saved["image_prompt"])
        self.assertTrue(self.service.formatted(saved).endswith("\n\nМарина"))

    def test_editing_pass_uses_two_reservations_and_fails_closed_at_quota(self):
        self.app.settings.daily_requests = 1
        with self.assertRaises(ValueError):
            self.service.generate(topic="Забытый инструмент")
        self.assertFalse(self.service.status()["posts"])
        self.assertFalse(self.app.status()["generating"])
        self.assertFalse(self.api.sent)
        row = self.app.store.db.execute("SELECT result FROM calls").fetchone()
        self.assertEqual(json.loads(row[0])["purpose"], "channel_draft")
        self.assertEqual(json.loads(row[0])["status"], "done")

    def test_auto_role_rotates_authors_without_switching_private_chat(self):
        self.app.switch("housewife")
        first = self.service.generate(topic="Первый сюжет", auto_role=True)
        self.assertEqual(first["mask"], "plumber")
        self.assertEqual(self.app.active, "housewife")
        self.assertEqual(self.service.choose_author(), "housewife")

    def test_short_photo_post_is_sent_as_one_captioned_message(self):
        value = payload("short")
        value["text"] = "Короткая история с понятным началом и финалом. " * 5
        post = self.service.save(value)
        self.service.attach(post["id"], self.photo)
        sent = self.service.publish(post["id"])
        self.assertEqual([p[0] for p in self.api.sent], ["photo"])
        self.assertEqual(sent["photo_message_id"], sent["text_message_id"])
        self.assertIn(value["text"].strip(), self.api.sent[0][1]["caption"])

    def test_telegram_editorial_is_deferred_and_can_be_stopped(self):
        api = Mock()
        runner = TelegramRunner(self.app, api)
        update = {"update_id": 1, "message": {"chat": {"id": 123, "type": "private"},
                  "from": {"id": 123}, "text": "/пост новый тема"}}
        with patch.object(self.service, "generate", wraps=self.service.generate) as generate:
            runner.accept(update)
            generate.assert_not_called()
            self.assertEqual(len(self.app.store.jobs("pending")), 1)
            self.app.stop()
            runner.work_once()
            generate.assert_not_called()
        self.assertFalse(self.service.status()["posts"])

    def test_telegram_editorial_worker_delivers_draft_only_to_owner(self):
        api = Mock()
        runner = TelegramRunner(self.app, api)
        runner.accept({"update_id": 1, "message": {"chat": {"id": 123, "type": "private"},
                       "from": {"id": 123}, "text": "/пост новый тема"}})
        runner.work_once()
        api.send.assert_called_once()
        self.assertEqual(api.send.call_args.args[0], 123)
        self.assertIn("не опубликован", api.send.call_args.args[1])
        self.assertFalse(self.api.sent)
        self.assertEqual(self.service.status()["posts"][0]["status"], "draft")

    def test_bind_requires_channel_owner_and_posting_rights(self):
        for attribute, value in (("type", "supergroup"), ("owner", "member"), ("can_post", False)):
            with self.subTest(attribute=attribute):
                original = getattr(self.api, attribute)
                setattr(self.api, attribute, value)
                with self.assertRaises(ChannelError):
                    self.service.bind("@another_fixture")
                setattr(self.api, attribute, original)
        self.assertEqual(self.service.status()["target"]["id"], -100555)

    def test_rebind_pauses_and_draft_cannot_leak_into_different_channel(self):
        post = self.draft()
        self.service.bind(-100777)
        self.assertFalse(self.service.status()["enabled"])
        self.service.enable(True)
        with self.assertRaises(ChannelError):
            self.service.publish(post["id"])
        self.assertFalse(self.api.sent)

    def test_private_chat_and_memory_never_enter_editorial_prompt(self):
        self.app.store.append("owner", "user", "private-chat-canary", "joker")
        self.app.store.remember("owner", "private-memory-canary")
        event_id = self.service.add_event("Публично разрешённое событие: сегодня проверили панель управления.")
        post = self.service.generate()
        context = self.backend.system + json.dumps(self.backend.messages, ensure_ascii=False)
        self.assertNotIn("private-chat-canary", context)
        self.assertNotIn("private-memory-canary", context)
        self.assertIn("Публично разрешённое", context)
        self.assertEqual(post["kind"], "public_event")
        self.assertEqual(post["event_id"], event_id)

    def test_invented_public_event_and_duplicate_story_are_rejected(self):
        value = payload()
        value.update(kind="public_event", event_id="nonexistent")
        with self.assertRaises(ChannelError):
            self.service.save(value)
        self.draft()
        with self.assertRaises(ChannelError):
            self.draft()

    def test_only_verified_image_directories_are_allowed(self):
        post = self.draft()
        for file in (self.root / ".env", self.root / "channel/EDITORIAL.md", self.root.parent / "outside.png"):
            with self.subTest(file=file), self.assertRaises(ChannelError):
                self.service.attach(post["id"], file)
        self.service.attach(post["id"], self.photo)
        self.assertEqual(self.service.photo_path(self.app.store.channel_post(post["id"])), self.photo.resolve())

    def test_send_once_and_store_both_telegram_message_ids(self):
        post = self.draft(photo=True)
        with patch("joker.channel.time.sleep"):
            result = self.service.publish(post["id"])
            self.service.publish(post["id"])
        self.assertEqual([p[0] for p in self.api.sent], ["photo", "text"])
        self.assertEqual(result["status"], "published")
        self.assertEqual((result["photo_message_id"], result["text_message_id"]), (41, 42))
        self.assertTrue(self.api.sent[-1][1]["text"].endswith("\n\nДжокер"))

    def test_ambiguous_delivery_never_retries_and_pauses_new_issues(self):
        post = self.draft(photo=True)
        self.api.fail_text = True
        with patch("joker.channel.time.sleep"), self.assertRaises(ChannelError):
            self.service.publish(post["id"])
        self.assertEqual(self.app.store.channel_post(post["id"])["status"], "uncertain")
        with self.assertRaises(ChannelError):
            self.service.publish(post["id"])
        self.assertEqual([p[0] for p in self.api.sent], ["photo", "text"])
        self.assertEqual(self.service.next_draft()["action"], "needs_review")
        self.service.discard(post["id"])
        self.assertFalse(self.service.status()["attention"])

    def test_pause_during_photo_upload_stops_following_text(self):
        post = self.draft(photo=True)
        self.api.on_photo = lambda: self.service.enable(False)
        with patch("joker.channel.time.sleep"), self.assertRaises(ChannelError):
            self.service.publish(post["id"])
        self.assertEqual([p[0] for p in self.api.sent], ["photo"])

    def test_schedule_does_not_repeat_or_catch_up_past_days(self):
        first, second = self.draft("one"), self.draft("two")
        noon = datetime(2026, 10, 15, 12, tzinfo=LOCAL_TZ)
        one = self.service.next_draft(noon)
        self.assertEqual(one["post"]["id"], first["id"])
        self.assertEqual(self.service.next_draft(noon)["post"]["id"], first["id"])
        self.service.publish(first["id"])
        self.assertEqual(self.service.next_draft(noon)["action"], "idle")
        self.assertEqual(self.service.next_draft(noon.replace(hour=20))["post"]["id"], second["id"])
        self.assertEqual(self.service.next_draft(noon.replace(hour=3))["action"], "idle")

    def test_stop_cancels_editorial_generation(self):
        def stopped(*args, **kwargs):
            self.app.stop()
            return EditorialBackend.generate(self.backend, *args, **kwargs)
        self.backend.generate = stopped
        with self.assertRaises(Cancelled):
            self.service.generate()
        self.assertFalse(self.service.status()["posts"])
        self.assertFalse(self.app.status()["generating"])

    def test_channel_controls_are_owner_only(self):
        result = self.app.handle("/автопост выкл", owner=False)
        self.assertIn("владельцу", result["reply"])
        self.assertTrue(self.service.status()["enabled"])

    def test_two_publishers_cannot_send_the_same_post_twice(self):
        post = self.draft()
        other = App(self.app.settings, EditorialBackend())
        other.channel.api = self.api
        barrier = threading.Barrier(2)
        def publish(service):
            barrier.wait(3)
            try:
                service.publish(post["id"])
            except ChannelError:
                pass
        workers = [threading.Thread(target=publish, args=(s,)) for s in (self.service, other.channel)]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(4)
                self.assertFalse(worker.is_alive())
            self.assertEqual(len(self.api.sent), 1)
        finally:
            other.store.close()


if __name__ == "__main__":
    unittest.main()
