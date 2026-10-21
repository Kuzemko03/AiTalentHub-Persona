import json
import re
import shutil
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from joker.backends import Cancelled
from joker.core import App
from joker.memory import MemoryConflict
from joker.panel import make_server
from test_next_stage import Fixture, Model


class MemoryTests(Fixture):
    def note(self, title="База", body="У реки", actor="owner", **kwargs):
        return self.app.memory.save(actor, title=title, body=body, **kwargs)

    def test_old_relevant_fact_is_found_after_leaving_recent_memory(self):
        fact = "Антон построил мост около берёзовой рощи"
        self.app.store.remember("owner", fact)
        with self.app.store.lock, self.app.store.db:
            self.app.store.db.execute("UPDATE facts SET at='2000-01-01' WHERE actor='owner'")
        for i in range(40): self.app.store.remember("owner", f"Новая заметка о шахте номер {i}")
        self.assertNotIn(fact, self.app.store.facts("owner"))
        self.app.reply("Где мост около березовой рощи?")
        self.assertIn(fact, self.model.system)

    def test_person_and_linked_agreement_enter_context_after_mask_switch(self):
        person = self.note("Никита", "Любит строить у реки", kind="person")
        promise = self.note("Экспедиция", "Обещали взять 16 брёвен", kind="agreement")
        self.app.memory.link("owner", person["id"], promise["id"], "участвует", person["version"])
        self.app.switch("engineer")
        self.app.reply("Что помнишь о Никите?")
        self.assertIn("Обещали взять 16 брёвен", self.model.system)
        self.assertIn("Любит строить у реки", self.model.system)
        self.assertEqual(self.app.active, "engineer")

    def test_public_game_notes_are_explicit_and_private_notes_never_leak(self):
        self.note("База", "PRIVATE_NOTE_FIXTURE", pinned=True)
        self.note("База", "CALL_NOTE_FIXTURE", actor="call:shared", pinned=True)
        self.note("База у кедра", "Координаты 123 64 -8", actor="world:minecraft", kind="place")
        self.app.reply("Где база у кедра?", actor="minecraft:guest")
        self.assertIn("Координаты 123 64 -8", self.model.system)
        self.assertNotIn("PRIVATE_NOTE_FIXTURE", self.model.system)
        self.assertNotIn("CALL_NOTE_FIXTURE", self.model.system)
        self.app.reply("Где база?", actor="call:shared")
        self.assertNotIn("Координаты 123 64 -8", self.model.system)
        self.assertNotIn("PRIVATE_NOTE_FIXTURE", self.model.system)

    def test_cross_scope_read_edit_delete_and_links_are_denied(self):
        private = self.note(body="PRIVATE_SCOPE_FIXTURE")
        public = self.note(actor="world:minecraft")
        for operation in (
            lambda: self.app.memory.get("world:minecraft", private["id"]),
            lambda: self.app.memory.save("world:minecraft", note_id=private["id"], version=1, title="bad", body="bad"),
            lambda: self.app.memory.delete("world:minecraft", private["id"], 1),
            lambda: self.app.memory.link("world:minecraft", public["id"], private["id"], "bad", 1)):
            with self.assertRaises(ValueError): operation()
        self.assertEqual(self.app.memory.get("owner", private["id"])["body"], "PRIVATE_SCOPE_FIXTURE")
        self.assertEqual(self.app.memory.get("world:minecraft", public["id"])["version"], 1)

    def test_stale_editor_cannot_overwrite_or_delete_a_newer_note(self):
        note = self.note()
        updated = self.app.memory.save("owner", note_id=note["id"], version=1, title="Новая база", body="У леса")
        self.assertEqual(updated["version"], 2)
        with self.assertRaises(MemoryConflict):
            self.app.memory.save("owner", note_id=note["id"], version=1, title="Старая вкладка", body="bad")
        with self.assertRaises(MemoryConflict): self.app.memory.delete("owner", note["id"], 1)
        self.assertEqual(self.app.memory.get("owner", note["id"])["title"], "Новая база")

    def test_two_connections_cannot_both_save_the_same_version(self):
        note = self.note()
        other = App(self.settings, Model())
        try:
            def save(memory, name):
                try:
                    return memory.save("owner", note_id=note["id"], version=1, title=name, body=name)["title"]
                except MemoryConflict:
                    return "conflict"
            with ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(save, memory, name) for memory, name in
                           ((self.app.memory, "Первый"), (other.memory, "Второй"))]
                results = [future.result(5) for future in futures]
            self.assertEqual(results.count("conflict"), 1)
            self.assertEqual(self.app.memory.get("owner", note["id"])["version"], 2)
        finally:
            other.store.close()

    def test_delete_removes_links_and_preserves_other_notes(self):
        person, place = self.note("Лена"), self.note("Дом")
        self.app.memory.link("owner", person["id"], place["id"], "живёт", 1)
        self.assertEqual(self.app.memory.get("owner", place["id"])["links"][0]["id"], person["id"])
        self.app.memory.delete("owner", person["id"], 2)
        self.assertEqual(self.app.memory.get("owner", place["id"])["links"], [])

    def test_mutation_cancels_old_reply_and_preserves_game_task(self):
        note = self.note()
        self.app.minecraft.execute("follow")
        original = self.model.generate
        def edited(*args, **kwargs):
            result = original(*args, **kwargs)
            self.app.memory.delete("owner", note["id"], note["version"])
            return result
        self.model.generate = edited
        with self.assertRaises(Cancelled): self.app.reply("Где наша база?")
        self.assertEqual(self.app.minecraft.task["kind"], "follow")
        self.assertEqual(self.entry.stop_calls, 0)
        self.assertEqual([r for r in self.app.store.history("owner") if r["role"] == "assistant"], [])

    def test_scoped_export_and_forget_leave_other_conversations_untouched(self):
        self.note(body="PRIVATE_EXPORT_FIXTURE")
        self.note(body="PUBLIC_EXPORT_FIXTURE", actor="world:minecraft")
        self.app.store.remember("owner", "Одинаковый факт")
        self.app.store.remember("call:shared", "Одинаковый факт")
        self.assertTrue(self.app.memory.forget_fact("owner", "Одинаковый факт"))
        self.assertEqual(self.app.store.facts("call:shared"), ["Одинаковый факт"])
        exported = json.dumps(self.app.memory.export("world:minecraft"))
        self.assertIn("PUBLIC_EXPORT_FIXTURE", exported)
        self.assertNotIn("PRIVATE_EXPORT_FIXTURE", exported)

    def test_context_size_limits_survive_long_notes_and_facts(self):
        for i in range(8): self.note(f"Экспедиция {i}", "Место " * 660, pinned=True, tags=["экспедиция"])
        for i in range(30): self.app.store.remember("owner", (f"Экспедиция {i} " + "р" * 900))
        data = self.app.memory.recall("owner", "экспедиция")
        self.assertLessEqual(len(data["notes"]), 5)
        self.assertLessEqual(len(data["facts"]), 24)
        self.assertLessEqual(sum(len(json.dumps(n, ensure_ascii=False)) for n in data["notes"]), 8000)
        self.assertLessEqual(sum(map(len, data["facts"])), 6000)

    def test_restart_and_pagination_preserve_notes_without_model_calls(self):
        for i in range(23): self.note(f"Заметка {i}", "Текст")
        first, second = self.app.memory.list("owner"), self.app.memory.list("owner", offset=20)
        self.assertEqual((len(first["notes"]), len(second["notes"]), first["total"]), (20, 3, 23))
        restored = App(self.settings, Model())
        try:
            self.assertEqual(restored.memory.list("owner")["total"], 23)
        finally:
            restored.store.close()
        self.assertEqual(self.model.calls, 0)

    def test_limit_validation_and_search_use_no_extra_model(self):
        self.note("Ёлочная поляна", "У воды", kind="place", tags=["добыча"])
        self.assertEqual(self.app.memory.list("owner", "елочной")["total"], 1)
        self.assertEqual(self.app.memory.list("owner", "добыча")["total"], 1)
        with self.assertRaises(ValueError): self.note(kind="/op")
        with self.assertRaises(ValueError): self.note(body="x" * 4001)
        with self.assertRaises(ValueError): self.app.memory.get("owner", "' OR 1=1 --")
        with self.assertRaises(ValueError): self.app.memory.list("owner", offset=True)
        with self.assertRaises(ValueError): self.app.memory.scope("owner")
        self.app.memory.MAX_NOTES = 1
        with self.assertRaises(ValueError): self.note("Лишняя")
        self.assertEqual(self.model.calls, 0)

    def test_minecraft_owner_can_write_only_game_memory_and_guests_cannot_edit(self):
        self.app.minecraft.accept({"user": "AlexPlayer", "msg": "Джокер, /заметка место | База | Рядом с рекой", "parse_type": "parsed"})
        self.app.minecraft.work_once()
        self.assertEqual(self.app.memory.list("minecraft:alexplayer")["total"], 1)
        self.assertEqual(self.app.memory.list("owner")["total"], 0)
        self.app.minecraft.accept({"user": "Guest", "msg": "Джокер, /заметка место | Подмена | bad", "parse_type": "parsed"})
        self.app.minecraft.work_once()
        self.assertEqual(self.app.memory.list("minecraft:guest")["total"], 0)
        self.assertEqual(self.model.calls, 0)

    def test_runtime_uses_curated_game_memory_without_private_notebook(self):
        self.note("Дерево", "PRIVATE_PLAN_NOTE", pinned=True)
        self.note("Дерево", "PUBLIC_PLAN_NOTE", actor="world:minecraft", pinned=True)
        self.app.handle("/цель Добудь дерево")
        self.app.handle("/автономность вкл")
        self.app.runtime.tick()
        self.assertIn("PUBLIC_PLAN_NOTE", self.model.context)
        self.assertNotIn("PRIVATE_PLAN_NOTE", self.model.context + self.model.system)


class MemoryAPITests(Fixture):
    def test_http_scopes_cas_export_and_csrf_without_live_services(self):
        (self.root / "joker").mkdir()
        from joker.config import ROOT
        shutil.copy(ROOT / "joker" / "panel.html", self.root / "joker" / "panel.html")
        server = make_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base) as response: page = response.read().decode()
            key = re.search(r'const key="([^"]+)"', page)[1]
            def post(payload, token=key):
                request = urllib.request.Request(base + "/api/memory", data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json", "X-Joker-Key": token})
                with urllib.request.urlopen(request) as response: return json.load(response)
            payload = {"action": "save", "scope": "private", "title": "Лена", "body": "PRIVATE_HTTP_NOTE", "kind": "person"}
            with self.assertRaises(urllib.error.HTTPError) as caught: post(payload, "")
            self.assertEqual(caught.exception.code, 403)
            note = post(payload)["note"]
            post({**payload, "id": note["id"], "version": 1, "body": "PRIVATE_UPDATED_NOTE"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                post({**payload, "id": note["id"], "version": 1})
            self.assertEqual(caught.exception.code, 409)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                post({"action": "read", "scope": "minecraft", "id": note["id"], "actor": "owner"})
            self.assertEqual(caught.exception.code, 400)
            post({"action": "save", "scope": "minecraft", "title": "База", "body": "PUBLIC_HTTP_NOTE"})
            with urllib.request.urlopen(base + "/api/memory/export?scope=minecraft") as response:
                exported = response.read().decode()
                self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertIn("PUBLIC_HTTP_NOTE", exported)
            self.assertNotIn("PRIVATE_UPDATED_NOTE", exported)
            self.assertEqual(post({"action": "list", "scope": "private", "query": "Лена"})["total"], 1)
            self.assertEqual(self.model.calls, 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == "__main__": unittest.main()
