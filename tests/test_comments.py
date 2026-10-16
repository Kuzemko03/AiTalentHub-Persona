import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from joker.comments import COMMENT_SCHEMA
from joker.config import ROOT, Settings
from joker.core import App
from joker.telegram_bot import TelegramError


class Backend:
    def __init__(self):
        self.calls = 0
        self.should_reply = True

    def generate(self, system, messages, cancel, *, schema=None):
        self.calls += 1
        self.context = json.loads(messages[0]["content"])
        self.system = system
        assert schema == COMMENT_SCHEMA
        return {"reply": "Да, знакомая история.", "should_reply": self.should_reply,
                "model": "test-fixture", "usage": {}, "latency": 0.01}


class API:
    def __init__(self):
        self.sent = []
        self.fail = False
        self.linked = -100777
        self.admin = True

    def call(self, method, **kw):
        if method == "getChat":
            return {"id": -100555, "title": "Тестовый канал", "type": "channel", "linked_chat_id": self.linked}
        if method == "getMe":
            return {"id": 9}
        if method == "getChatMember":
            return {"status": "administrator" if self.admin else "member", "can_post_messages": True} if kw["user_id"] == 9 else {"status": "creator"}
        if method == "sendMessage":
            self.sent.append(kw)
            if self.fail:
                raise TelegramError("Тестовый разрыв сети")
            return {"message_id": 900 + len(self.sent)}
        raise AssertionError(method)


class CommentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / "agent", self.root / "agent")
        self.backend = Backend()
        self.app = App(Settings(root=self.root, owner_id=123), self.backend)
        self.api = API()
        self.app.channel.api = self.api
        self.app.channel.bind("@test_fixture")
        self.post = self.app.channel.save({"title": "Тихая поездка", "text": "Пассажир попросил ехать без разговоров. " * 5,
            "mask": "driver", "image_scene": "A fictional taxi driver sitting at the wheel.", "novelty_key": "Тихая поездка тест",
            "kind": "fictional_scene", "event_id": ""})
        self.app.store.update_channel_post(self.post["id"], status="published", text_message_id=55, photo_message_id=54)
        self.service = self.app.comments
        self.service.enable(True)
        self.service.sync(self.api, force=True)

    def tearDown(self):
        self.app.store.close()
        self.temp.cleanup()

    def root_message(self):
        return {"message_id": 80, "is_automatic_forward": True,
                "forward_origin": {"type": "channel", "chat": {"id": -100555}, "message_id": 55}}

    def update(self, update_id=1, text="Знакомая история!", **extra):
        message = {"message_id": 100 + update_id, "chat": {"id": -100777, "type": "supergroup"},
                   "from": {"id": 456, "is_bot": False}, "text": text, "reply_to_message": self.root_message()}
        message.update(extra)
        return {"update_id": update_id, "message": message}

    def test_author_is_pinned_and_private_facts_never_enter_comments(self):
        self.app.switch("housewife")
        self.app.store.remember("owner", "PRIVATE_MEMORY_CANARY")
        self.app.store.append("owner", "user", "PRIVATE_CHAT_CANARY", "joker")
        self.service.accept(self.update())
        self.service.work_once(self.api)
        self.assertEqual(self.backend.context["author_profile"]["display_name"], "Игорь")
        self.assertNotIn("PRIVATE_", self.backend.system + json.dumps(self.backend.context))
        self.assertEqual(self.app.active, "housewife")
        self.assertEqual(self.api.sent[0]["chat_id"], -100777)
        self.assertEqual(self.api.sent[0]["reply_parameters"]["message_id"], 101)

    def test_duplicate_update_and_own_reply_never_create_loop(self):
        event = self.update()
        self.service.accept(event)
        self.service.work_once(self.api)
        self.service.accept(event)
        self.service.accept(self.update(2, **{"from": {"id": 9, "is_bot": True}}))
        self.service.work_once(self.api)
        self.assertEqual(self.backend.calls, 1)
        self.assertEqual(len(self.api.sent), 1)

    def test_replies_to_bot_continue_the_same_post_thread(self):
        self.service.accept(self.update())
        self.service.work_once(self.api)
        self.service.accept(self.update(2, text="А дальше что?", reply_to_message={"message_id": 901}))
        self.service.work_once(self.api)
        self.assertEqual(self.backend.context["public_thread"][0]["reply"], "Да, знакомая история.")
        self.assertEqual(len(self.api.sent), 2)

    def test_unrelated_group_unmapped_thread_and_manual_forward_are_ignored(self):
        for event in (self.update(chat={"id": -100999, "type": "supergroup"}),
                      self.update(reply_to_message={"message_id": 404}),
                      self.update(reply_to_message={**self.root_message(), "is_automatic_forward": False})):
            self.service.accept(event)
        self.service.work_once(self.api)
        self.assertEqual(self.backend.calls, 0)
        self.assertFalse(self.api.sent)

    def test_comment_commands_do_not_control_owner_role(self):
        self.service.accept(self.update(text="/mask engineer"))
        self.service.work_once(self.api)
        self.assertEqual(self.app.active, "joker")
        self.assertEqual(self.backend.calls, 0)

    def test_pause_and_model_skip_send_nothing(self):
        self.backend.should_reply = False
        self.service.accept(self.update())
        self.service.work_once(self.api)
        self.assertFalse(self.api.sent)
        self.service.accept(self.update(2))
        self.service.enable(False)
        self.service.work_once(self.api)
        self.assertEqual(self.backend.calls, 1)

    def test_uncertain_delivery_is_not_retried_after_restart(self):
        self.service.accept(self.update())
        self.api.fail = True
        self.service.work_once(self.api)
        self.app.store.recover_jobs()
        self.service.work_once(self.api)
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.service.status()["attention"], [1])

    def test_binding_requires_linked_group_and_bot_admin(self):
        self.api.linked = None
        self.service.sync(self.api, force=True)
        self.assertFalse(self.service.status()["connected"])
        self.api.linked = -100777
        self.api.admin = False
        self.service.sync(self.api, force=True)
        self.assertFalse(self.service.status()["connected"])

    def test_rate_limit_bounds_public_queue(self):
        for index in range(10):
            self.service.accept(self.update(index + 1))
        self.assertEqual(self.app.store.db.execute("SELECT COUNT(*) FROM comment_jobs").fetchone()[0], 3)

    def test_channel_comments_use_channel_identity_and_keep_owner_commands_private(self):
        channel_sender = {"sender_chat": {"id": -100555, "type": "channel"},
                          "from": {"id": 888, "is_bot": True}}
        self.service.accept(self.update(1, **channel_sender))
        self.service.work_once(self.api)
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.app.store.db.execute("SELECT user_id FROM comment_jobs").fetchone()[0], -100555)
        self.service.accept(self.update(2, text="/mask engineer", **channel_sender))
        self.service.accept(self.update(3, **{**channel_sender, "from": {"id": 9, "is_bot": True}}))
        self.service.accept(self.update(4, is_automatic_forward=True, **channel_sender))
        self.service.work_once(self.api)
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.app.active, "joker")

    def test_different_channels_do_not_share_fake_sender_rate_limit(self):
        for index in range(8):
            self.service.accept(self.update(index + 1, sender_chat={"id": -100100 - index // 4, "type": "channel"},
                                            **{"from": {"id": 888, "is_bot": True}}))
        self.assertEqual(self.app.store.db.execute("SELECT COUNT(*) FROM comment_jobs").fetchone()[0], 6)

    def test_two_workers_claim_comment_only_once(self):
        self.service.accept(self.update())
        other = App(self.app.settings, Backend())
        barrier = threading.Barrier(2)
        def work(service):
            barrier.wait(3)
            service.work_once(self.api)
        workers = [threading.Thread(target=work, args=(s,)) for s in (self.service, other.comments)]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(4)
                self.assertFalse(worker.is_alive())
            self.assertEqual(len(self.api.sent), 1)
        finally:
            other.store.close()
