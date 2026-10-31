import json
import shutil

from joker.backends import ModelError
from joker.capabilities import implementation
from joker.config import ROOT
from test_next_stage import Fixture
from test_workplace import Scripted, answer, tool
from test_channel import ChannelAPI, payload


class ChannelToolTests(Fixture):
    def prepare(self):
        target = self.root / "channel"
        target.mkdir(exist_ok=True)
        for name in ("ideas.json", "EDITORIAL.md"):
            shutil.copy(ROOT / "channel" / name, target / name)
        self.settings.owner_id = 123
        self.api = ChannelAPI()
        self.app.channel.api = self.api
        self.app.channel.bind("@test_fixture")

    def post_answer(self):
        return {**payload(), "kind": "project_note", "model": "fixture", "usage": {}, "latency": .01}

    def test_implementation_exposes_models_but_no_credentials(self):
        self.settings.stt_backend, self.settings.stt_model = "whisper", "small"
        self.settings.tts_backend = "piper"
        self.settings.telegram_token = "PRIVATE_TOKEN_FIXTURE"
        self.settings.api_key = "PRIVATE_API_FIXTURE"
        value = implementation(self.app)
        self.assertEqual(value["stt"]["backend"], "whisper")
        self.assertEqual(value["tts"]["backend"], "piper")
        self.assertNotIn("PRIVATE_", json.dumps(value))

    def test_natural_draft_uses_existing_model_lane_and_stays_unpublished(self):
        self.prepare()
        self.app.backend = Scripted(tool("channel.post", topic="Как устроены STT и TTS", publish=False),
                                    self.post_answer(), self.post_answer(), answer("Черновик готов."))
        self.app.handle("Сделай черновик поста об устройстве голоса")
        posts = self.app.store.channel_posts()
        self.assertEqual(posts[0]["status"], "draft")
        self.assertEqual(json.loads(posts[0]["payload"])["kind"], "project_note")
        self.assertEqual(self.api.sent, [])
        self.assertIn('"implementation"', self.app.backend.calls[1][1][0]["content"])
        self.assertFalse(self.app.generation.locked())

    def test_explicit_post_can_publish_once_without_enabling_schedule(self):
        self.prepare()
        self.app.backend = Scripted(tool("channel.post", topic="Устройство голоса", publish=True),
                                    self.post_answer(), self.post_answer(), answer("Пост опубликован."))
        self.app.handle("Опубликуй в моём канале пост об устройстве голоса")
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.app.store.channel_posts()[0]["status"], "published")
        self.assertFalse(self.app.store.get("channel_enabled", False))

    def test_game_owner_cannot_publish_or_see_editorial_tools(self):
        actor = "minecraft:alexplayer"
        tools = {t["name"] for t in self.app.workplace.context(actor, True)["available_tools"]}
        self.assertNotIn("channel.post", tools)
        self.app.backend = Scripted(tool("channel.post", topic="anything", publish=True))
        with self.assertRaises(ModelError):
            self.app.reply("Опубликуй", actor=actor, owner=True)
