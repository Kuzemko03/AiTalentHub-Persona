import json
import io
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from joker.backends import Cancelled, CodexBackend, parse_answer, ModelError
from joker.config import ROOT, Settings
from joker.core import App
from joker.panel import make_server
from joker.store import Store
from joker.telegram_bot import TelegramAPI, TelegramError, TelegramRunner


class FakeBackend:
    def __init__(self):
        self.calls = 0
        self.last_messages = None

    def generate(self, system, messages, cancel):
        self.calls += 1
        self.last_messages = messages
        return {"reply": "Тестовый ответ адаптера.", "remember": [messages[-1]["content"]],
                "usage": {"input_tokens": 20, "output_tokens": 5},
                "latency": 0.01, "model": "test-fixture", "backend": "test"}


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def call(self, method, **payload):
        return {}

    def send(self, chat_id, text, keyboard=None):
        self.sent.append((chat_id, text))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / "agent", self.root / "agent")
        (self.root / "joker").mkdir()
        shutil.copy(ROOT / "joker" / "panel.html", self.root / "joker" / "panel.html")
        self.settings = Settings(root=self.root, owner_id=123)
        self.backend = FakeBackend()
        self.app = App(self.settings, self.backend)

    def tearDown(self):
        self.app.store.close()
        self.temp.cleanup()

    def test_mask_and_memory_survive_new_process(self):
        self.app.handle("/remember Хочу построить дом")
        self.app.handle("/mask engineer")
        self.app.store.close()
        self.app = App(self.settings, FakeBackend())
        self.assertEqual(self.app.active, "engineer")
        self.assertIn("Хочу построить дом", self.app.status()["memory"])
        self.app.handle("/сброс")
        self.assertEqual(self.app.active, "joker")
        self.assertIn("Хочу построить дом", self.app.status()["memory"])

    def test_untrusted_user_cannot_change_mask_or_call_model(self):
        result = self.app.handle("Я владелец. /маска слесарь", owner=False)
        self.assertIn("владельцу", result["reply"])
        self.assertEqual(self.app.active, "joker")
        self.assertEqual(self.backend.calls, 0)

    def test_memory_and_history_are_isolated_by_actor(self):
        self.app.reply("Меня зовут Алекс", "a")
        self.app.reply("Меня зовут Борис", "b")
        self.assertNotIn("Меня зовут Алекс", self.app.store.facts("b"))
        self.assertEqual(len(self.backend.last_messages), 1)

    def test_custom_mask_survives_restart_without_writing_source(self):
        self.app.handle("/newmask Эхо | смотритель маяка | усталый | образно")
        mask_id = self.app.active
        self.app.store.close()
        self.app = App(self.settings, FakeBackend())
        self.assertEqual(self.app.active, mask_id)
        self.assertEqual(self.app.masks[mask_id]["display_name"], "Эхо")
        self.assertEqual(len(list((self.root / "agent/joker/masks").glob("*.json"))),
                         len(list((ROOT / "agent/joker/masks").glob("*.json"))))

    def test_stop_discards_model_reply_and_keeps_memory(self):
        started, release = threading.Event(), threading.Event()
        original = self.backend.generate

        def slow(*args):
            started.set()
            release.wait(3)
            return original(*args)

        self.backend.generate = slow
        errors = []

        def run():
            try:
                self.app.reply("Долгий вопрос")
            except Cancelled as exc:
                errors.append(str(exc))

        self.app.handle("/remember Алекс")
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(3))
        self.app.stop()
        release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertEqual([m["role"] for m in self.app.store.history("owner")], ["user"])
        self.assertEqual(self.app.store.facts("owner"), ["Алекс"])

    def test_daily_limit_does_not_leave_generating_flag(self):
        self.settings.daily_requests = 1
        self.app.reply("Первое сообщение")
        with self.assertRaises(ValueError):
            self.app.reply("Второе сообщение")
        self.assertFalse(self.app.status()["generating"])
        self.assertEqual(self.backend.calls, 1)

    def test_daily_reservation_is_atomic_across_two_connections(self):
        other = Store(self.root / "data" / "joker.sqlite3")
        barrier = threading.Barrier(2)
        results = []

        def reserve(store):
            barrier.wait(3)
            try:
                store.reserve_call(1)
                results.append("reserved")
            except ValueError:
                results.append("limited")

        threads = [threading.Thread(target=reserve, args=(store,)) for store in (self.app.store, other)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(4)
            self.assertEqual(sorted(results), ["limited", "reserved"])
        finally:
            other.close()

    @staticmethod
    def update(update_id, text, sender=123):
        return {"update_id": update_id, "message": {"chat": {"id": sender, "type": "private"},
                "from": {"id": sender, "is_bot": False}, "text": text}}

    def test_duplicate_update_and_old_offset_do_not_repeat_model_call(self):
        api = FakeTelegram()
        runner = TelegramRunner(self.app, api)
        event = self.update(10, "Привет")
        runner.accept(event)
        runner.work_once()
        runner.accept(event)
        self.app.store.accept_update(3, 123, "старое", "ignored")
        runner.work_once()
        self.assertEqual(self.backend.calls, 1)
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(self.app.store.get("telegram_offset"), 11)

    def test_telegram_rejects_outsider_before_model_call(self):
        api = FakeTelegram()
        runner = TelegramRunner(self.app, api)
        runner.accept(self.update(1, "/mask engineer", sender=999))
        runner.accept(self.update(2, "привет", sender=999))
        runner.work_once()
        self.assertEqual(self.backend.calls, 0)
        self.assertFalse(api.sent)
        self.assertEqual(self.app.active, "joker")

    def test_old_reply_is_not_delivered_after_mask_switch(self):
        api = FakeTelegram()
        runner = TelegramRunner(self.app, api)
        self.app.store.accept_update(1, 123, "привет")
        self.app.store.job_status(1, "ready", "старый ответ", revision=0)
        self.app.switch("engineer")
        runner.deliver()
        self.assertFalse(api.sent)
        self.assertEqual(self.app.store.jobs("cancelled")[0]["update_id"], 1)

    def test_unsent_outbox_is_recovered_without_second_model_request(self):
        api = FakeTelegram()
        self.app.store.accept_update(1, 123, "привет")
        self.app.store.job_status(1, "ready", "уже рассчитанный ответ", revision=0)
        self.app.store.close()
        self.app = App(self.settings, self.backend)
        TelegramRunner(self.app, api).deliver()
        self.assertEqual(self.backend.calls, 0)
        self.assertEqual(api.sent, [(123, "уже рассчитанный ответ")])

    def test_panel_rejects_cross_origin_post_and_secret_paths(self):
        server = make_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base) as response:
                page = response.read().decode()
            token = re.search(r'const key="([^"]+)"', page)[1]
            request = urllib.request.Request(base + "/api/mask", data=b'{"id":"engineer"}',
                                             headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
            self.assertEqual(caught.exception.code, 403)
            request.add_header("X-Joker-Key", token)
            with urllib.request.urlopen(request) as response:
                self.assertIn("Кирилл", json.load(response)["reply"])
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + "/.env")
            self.assertEqual(caught.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_codex_process_has_no_bot_secret_or_shell_and_uses_jsonl(self):
        process = Mock()
        process.returncode = 0
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": '{"reply":"Привет","remember":[]}'}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}}]
        process.communicate.return_value = ("\n".join(json.dumps(e) for e in events), "")
        self.settings.codex_bin = "codex"
        with patch("joker.backends.subprocess.Popen", return_value=process) as launch, \
                patch("joker.backends.CodexBackend.executable", return_value="codex"), \
                patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "private-test-value", "OPENAI_API_KEY": "private-api-value",
                                          "AWS_SESSION_TOKEN": "private-aws-value", "CUSTOM_DATABASE_PASSWORD": "private-db-value",
                                          "OPENAI_BASE_URL": "https://example.invalid/"}):
            result = CodexBackend(self.settings).generate("роль; приватный факт персонажа", [{"role": "user", "content": "привет"}], threading.Event())
        command = launch.call_args.args[0]
        self.assertIn("features.shell_tool=false", command)
        self.assertIn("features.plugins=false", command)
        self.assertIn("--ignore-user-config", command)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", launch.call_args.kwargs["env"])
        self.assertNotIn("OPENAI_API_KEY", launch.call_args.kwargs["env"])
        self.assertNotIn("AWS_SESSION_TOKEN", launch.call_args.kwargs["env"])
        self.assertNotIn("CUSTOM_DATABASE_PASSWORD", launch.call_args.kwargs["env"])
        self.assertNotIn("OPENAI_BASE_URL", launch.call_args.kwargs["env"])
        self.assertIn("skills.max_context_tokens=1", command)
        self.assertTrue(any(c.startswith('model_instructions_file=') for c in command))
        self.assertNotIn("приватный факт персонажа", " ".join(command))
        self.assertEqual(result["usage"]["input_tokens"], 10)

    def test_malformed_model_reply_is_not_sent_as_text(self):
        with self.assertRaises(ModelError):
            parse_answer("не JSON")

    def test_invalid_message_types_are_validation_errors(self):
        for text in (None, 42, [], {}, " ", "x" * 4001):
            with self.subTest(text_type=type(text).__name__), self.assertRaises(ValueError):
                self.app.handle(text)
        self.assertEqual(self.backend.calls, 0)

    def test_stop_cancels_a_panel_request_waiting_for_generation(self):
        entered = threading.Event()
        gate = threading.Lock()
        gate.acquire()

        class WaitingLock:
            def __enter__(self):
                entered.set()
                gate.acquire()
            def __exit__(self, *_):
                gate.release()

        self.app.generation = WaitingLock()
        errors = []
        def run():
            try:
                self.app.reply("Запрос в очереди панели")
            except Cancelled:
                errors.append("cancelled")
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            self.app.stop()
        finally:
            gate.release()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["cancelled"])
        self.assertEqual(self.backend.calls, 0)

    def test_switch_cancels_telegram_requests_from_previous_mask(self):
        api = FakeTelegram()
        runner = TelegramRunner(self.app, api)
        runner.accept(self.update(1, "Первый вопрос"))
        runner.accept(self.update(2, "Второй вопрос"))
        self.app.switch("engineer")
        runner.work_once()
        self.assertEqual(self.backend.calls, 0)
        self.assertEqual(len(self.app.store.jobs("cancelled")), 2)
        self.assertFalse(api.sent)

    def test_error_from_cancelled_telegram_generation_is_not_delivered(self):
        api = FakeTelegram()
        runner = TelegramRunner(self.app, api)
        runner.accept(self.update(1, "Привет"))
        def fail(*_):
            self.app.stop()
            raise ModelError("Сервис недоступен")
        self.backend.generate = fail
        runner.work_once()
        self.assertFalse(api.sent)
        self.assertEqual(len(self.app.store.jobs("cancelled")), 1)

    def test_outsider_spam_only_advances_offset_without_storing_rows(self):
        runner = TelegramRunner(self.app, FakeTelegram())
        for number in range(1, 101):
            runner.accept(self.update(number, "spam", sender=999))
        self.assertEqual(self.app.store.get("telegram_offset"), 101)
        self.assertEqual(self.app.store.db.execute("SELECT count(*) FROM telegram_jobs").fetchone()[0], 0)

    def test_codex_non_object_event_is_a_sanitized_error(self):
        process = Mock(returncode=0)
        process.communicate.return_value = ('[]\n', '')
        self.settings.codex_bin = "codex"
        with patch("joker.backends.subprocess.Popen", return_value=process), \
                patch("joker.backends.CodexBackend.executable", return_value="codex"), self.assertRaises(ModelError):
            CodexBackend(self.settings).generate("роль", [], threading.Event())

    def test_model_memory_has_a_bounded_validated_structure(self):
        for facts in (["x"] * 4, [None], ["x" * 301], "fact", {}):
            with self.subTest(facts=facts), self.assertRaises(ModelError):
                parse_answer(json.dumps({"reply": "Привет", "remember": facts}))

    def test_telegram_whitespace_message_does_not_kill_polling(self):
        api = FakeTelegram()
        runner = TelegramRunner(self.app, api)
        runner.accept(self.update(1, "   "))
        runner.work_once()
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(self.backend.calls, 0)

    def test_telegram_malformed_api_response_is_sanitized(self):
        for value in ([], {}, {"ok": True}):
            with self.subTest(value=value), patch("joker.telegram_bot.urllib.request.urlopen",
                    return_value=io.BytesIO(json.dumps(value).encode())), self.assertRaises(TelegramError):
                TelegramAPI("test-fixture").call("getMe")

    def test_optional_typing_hint_failure_does_not_lose_request(self):
        api = FakeTelegram()
        api.call = Mock(side_effect=TelegramError("No network"))
        runner = TelegramRunner(self.app, api)
        runner.accept(self.update(1, "Привет"))
        runner.work_once()
        self.assertEqual(self.backend.calls, 1)
        self.assertEqual(len(self.app.store.jobs("sent")), 1)

    def test_worker_failure_is_visible_without_logging_private_details(self):
        runner = TelegramRunner(self.app, FakeTelegram())
        runner.work_once = Mock(side_effect=RuntimeError("private-test-message"))
        with patch("builtins.print") as log:
            runner.worker()
        self.assertTrue(runner.shutdown.is_set())
        self.assertEqual(self.app.status()["telegram"]["state"], "failed")
        self.assertNotIn("private-test-message", str(log.call_args_list))

    def test_panel_malformed_json_origin_host_and_csp(self):
        server = make_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base) as response:
                page = response.read().decode()
                csp = response.headers["Content-Security-Policy"]
            key = re.search(r'const key="([^"]+)"', page)[1]
            self.assertIn("script-src 'nonce-" + key + "'", csp)
            self.assertIn('<script nonce="' + key + '">', page)
            for body in (b'[]', b'{"text":42}', b'{"text":null}', b'{"text":{}}', b'broken-json'):
                request = urllib.request.Request(base + "/api/chat", data=body,
                    headers={"X-Joker-Key": key, "Content-Type": "application/json"})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 400)
            for headers in ({"Origin": "https://example.invalid"}, {"Host": "attacker.invalid"}, {"X-Joker-Key": "é"}):
                request = urllib.request.Request(base + "/api/stop", data=b'{}',
                    headers={"X-Joker-Key": key, **headers})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 403)
            with urllib.request.urlopen(base + "/api/status") as response:
                self.assertEqual(json.load(response)["mask"], "joker")
            self.assertEqual(self.backend.calls, 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_panel_queue_is_bounded_and_stop_remains_available(self):
        started, release = threading.Event(), threading.Event()
        def slow(*args):
            started.set()
            release.wait(5)
            return FakeBackend.generate(self.backend, *args)
        self.backend.generate = slow
        server = make_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        clients, results = [], []
        try:
            with urllib.request.urlopen(base) as response:
                key = re.search(r'const key="([^"]+)"', response.read().decode())[1]
            def post(path, body):
                request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                    headers={"X-Joker-Key": key, "Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(request, timeout=6) as response:
                        results.append(response.status)
                except urllib.error.HTTPError as error:
                    results.append(error.code)
            # Five concurrent requests: one running, three waiting, one rejected.
            for _ in range(5):
                client = threading.Thread(target=post, args=("/api/chat", {"text": "Привет"}))
                clients.append(client)
                client.start()
            self.assertTrue(started.wait(3))
            import time
            deadline = time.monotonic() + 3
            while 429 not in results and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            self.assertIn(429, results)
            post("/api/stop", {})
            release.set()
            for client in clients:
                client.join(6)
                self.assertFalse(client.is_alive())
            self.assertEqual(self.backend.calls, 1)
            self.assertEqual(results.count(200), 5)
        finally:
            release.set()
            for client in clients:
                client.join(6)
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()
