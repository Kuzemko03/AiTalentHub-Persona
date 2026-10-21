"""Local notebooks: isolated conversations and explicitly curated public game notes."""
import json
import re
import unicodedata
import uuid

KINDS = {"note": "заметка", "person": "человек", "place": "место",
         "agreement": "договорённость", "event": "событие"}
SCOPES = {"private": "owner", "minecraft": "world:minecraft", "call": "call:shared"}
NOTE_ID = re.compile(r"m_[a-f0-9]{12}\Z")
STOP_WORDS = set(("а без был была были быть в вам вас весь во вот все всего вы где да для до "
    "его ее ещё еще есть же за зачем здесь и из или их к как когда кто ли мне мной меня "
    "мы на над надо наш не него нет ни но о об обо он она они от по под пока помнишь "
    "помни помнить память про расскажи с со так там то том ты у уже что чтобы это этот "
    "я my me the a an and or you your what about remember tell please джокер").split())


class MemoryConflict(ValueError):
    pass


def normalized(text):
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


def words(text):
    return {w for w in re.findall(r"[^\W_]+", normalized(text), re.UNICODE)
            if len(w) >= 2 and w not in STOP_WORDS}


def matches(term, candidates):
    return term in candidates or len(term) >= 4 and any(
        len(word) >= 4 and term[:4] == word[:4] for word in candidates)


def rank(query, title="", body="", tags=()):
    terms = sorted(words(query))[:24]
    if not terms:
        return 0
    title_words, body_words, tag_words = words(title), words(body), words(" ".join(tags))
    score = sum(6 * matches(term, title_words) + 4 * matches(term, tag_words)
                + matches(term, body_words) for term in terms)
    if title and normalized(title) in normalized(query):
        score += 10
    return score


class Memory:
    MAX_NOTES = 500

    def __init__(self, app):
        self.app, self.store = app, app.store

    @staticmethod
    def scope(value="private"):
        if not isinstance(value, str) or value not in SCOPES:
            raise ValueError("Выбери личную память, общую память Minecraft или созвона.")
        return SCOPES[value]

    @staticmethod
    def check_id(note_id):
        if not isinstance(note_id, str) or not NOTE_ID.fullmatch(note_id):
            raise ValueError("Неверный ID заметки.")

    @staticmethod
    def version(value):
        if type(value) is not int or value < 1:
            raise ValueError("Нужна текущая версия заметки. Обнови список.")
        return value

    @staticmethod
    def fields(title, body, kind, tags, pinned):
        if (not isinstance(title, str) or not 1 <= len(title.strip()) <= 120
                or not isinstance(body, str) or not 1 <= len(body.strip()) <= 4000
                or not isinstance(kind, str) or kind not in KINDS
                or not isinstance(tags, list) or len(tags) > 8 or type(pinned) is not bool
                or any(not isinstance(t, str) or not 1 <= len(t.strip()) <= 32 for t in tags)):
            raise ValueError("Название: 1–120, текст: 1–4000 символов; до 8 тегов по 32 символа.")
        return title.strip(), body.strip(), kind, list(dict.fromkeys(t.strip() for t in tags)), pinned

    def _row(self, actor, note_id):
        self.check_id(note_id)
        row = self.store.db.execute("SELECT * FROM memory_notes WHERE actor=? AND id=?", (actor, note_id)).fetchone()
        if row is None:
            raise ValueError("Заметка не найдена в этой беседе.")
        return self.decode(row)

    @staticmethod
    def decode(row):
        result = dict(row)
        result.pop("actor")
        result["tags"] = json.loads(result["tags"])
        result["pinned"] = bool(result["pinned"])
        return result

    def get(self, actor, note_id):
        with self.store.lock:
            result = self._row(actor, note_id)
            rows = self.store.db.execute("""SELECT n.id,n.title,l.label FROM memory_links l
                JOIN memory_notes n ON n.actor=l.actor AND n.id=CASE WHEN l.source=? THEN l.target ELSE l.source END
                WHERE l.actor=? AND (l.source=? OR l.target=?) ORDER BY n.updated_at DESC LIMIT 24""",
                (note_id, actor, note_id, note_id)).fetchall()
            result["links"] = [dict(row) for row in rows]
            return result

    def all_notes(self, actor):
        with self.store.lock:
            rows = self.store.db.execute("SELECT * FROM memory_notes WHERE actor=? "
                "ORDER BY pinned DESC,updated_at DESC,rowid DESC LIMIT ?", (actor, self.MAX_NOTES)).fetchall()
            notes = [self.decode(row) for row in rows]
            index = {n["id"]: n for n in notes}
            for note in notes:
                note["links"] = []
            edges = self.store.db.execute("""SELECT l.source,l.target,l.label FROM memory_links l
                JOIN memory_notes a ON a.actor=l.actor AND a.id=l.source
                JOIN memory_notes b ON b.actor=l.actor AND b.id=l.target WHERE l.actor=?""", (actor,)).fetchall()
            for source, target, label in edges:
                if source in index and target in index:
                    for own, other in ((source, target), (target, source)):
                        index[own]["links"].append({"id": other, "title": index[other]["title"], "label": label})
            for note in notes:
                note["links"].sort(key=lambda edge: index[edge["id"]]["updated_at"], reverse=True)
                note["links"] = note["links"][:24]
            return notes

    def save(self, actor, *, title, body, kind="note", tags=None, pinned=False, note_id=None, version=None):
        title, body, kind, tags, pinned = self.fields(title, body, kind, [] if tags is None else tags, pinned)
        with self.app.guard, self.store.lock, self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            now = self.store.now()
            if note_id is None:
                count = self.store.db.execute("SELECT COUNT(*) FROM memory_notes WHERE actor=?", (actor,)).fetchone()[0]
                if count >= self.MAX_NOTES:
                    raise ValueError("В этой беседе уже 500 заметок. Удали ненужную перед новой.")
                note_id = "m_" + uuid.uuid4().hex[:12]
                self.store.db.execute("INSERT INTO memory_notes VALUES (?,?,?,?,?,?,?,1,?,?)",
                    (note_id, actor, kind, title, body, json.dumps(tags, ensure_ascii=False), int(pinned), now, now))
            else:
                self._row(actor, note_id)
                changed = self.store.db.execute("""UPDATE memory_notes SET kind=?,title=?,body=?,tags=?,pinned=?,
                    version=version+1,updated_at=? WHERE actor=? AND id=? AND version=?""",
                    (kind, title, body, json.dumps(tags, ensure_ascii=False), int(pinned), now,
                     actor, note_id, self.version(version))).rowcount
                if not changed:
                    raise MemoryConflict("Заметка уже изменена. Обнови список перед сохранением.")
            self.app.cancel_reply()  # Invalidate text/audio made from the previous memory snapshot.
            return self.get(actor, note_id)

    def delete(self, actor, note_id, version):
        with self.app.guard, self.store.lock, self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            self._row(actor, note_id)
            changed = self.store.db.execute("DELETE FROM memory_notes WHERE actor=? AND id=? AND version=?",
                                           (actor, note_id, self.version(version))).rowcount
            if not changed:
                raise MemoryConflict("Заметка уже изменена. Обнови список перед удалением.")
            self.store.db.execute("DELETE FROM memory_links WHERE actor=? AND (source=? OR target=?)",
                                  (actor, note_id, note_id))
            self.app.cancel_reply()

    def link(self, actor, source, target, label, version, remove=False):
        if type(remove) is not bool or not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
            raise ValueError("Название связи: от 1 до 80 символов.")
        with self.app.guard, self.store.lock, self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            self._row(actor, source)
            self._row(actor, target)
            if source == target:
                raise ValueError("Выбери две разные заметки.")
            version = self.version(version)
            if self._row(actor, source)["version"] != version:
                raise MemoryConflict("Заметка уже изменена. Обнови список.")
            if remove:
                self.store.db.execute("DELETE FROM memory_links WHERE actor=? AND "
                    "((source=? AND target=?) OR (source=? AND target=?))", (actor, source, target, target, source))
            else:
                exists = self.store.db.execute("SELECT 1 FROM memory_links WHERE actor=? AND "
                    "((source=? AND target=?) OR (source=? AND target=?))", (actor, source, target, target, source)).fetchone()
                if exists:
                    raise ValueError("Эти заметки уже связаны.")
                for note_id in (source, target):
                    count = self.store.db.execute("SELECT COUNT(*) FROM memory_links WHERE actor=? AND "
                        "(source=? OR target=?)", (actor, note_id, note_id)).fetchone()[0]
                    if count >= 12:
                        raise ValueError("У заметки уже 12 связей.")
                self.store.db.execute("INSERT INTO memory_links VALUES (?,?,?,?)", (actor, source, target, label.strip()))
            self.store.db.execute("UPDATE memory_notes SET version=version+1,updated_at=? WHERE actor=? AND id IN (?,?)",
                                  (self.store.now(), actor, source, target))
            self.app.cancel_reply()
            return self.get(actor, source)

    def fact_candidates(self, actor, limit=2048):
        with self.store.lock:
            return [row[0] for row in self.store.db.execute("SELECT fact FROM facts WHERE actor=? "
                "ORDER BY at DESC,rowid DESC LIMIT ?", (actor, limit))]

    def recall(self, actor, query):
        """Conversation memory plus explicitly curated public notes for game actors."""
        with self.store.lock:
            facts = self.fact_candidates(actor)
            notes = self.all_notes(actor)
            shared_ids = set()
            if actor.startswith("minecraft:"):
                shared = self.all_notes(SCOPES["minecraft"])
                shared_ids = {n["id"] for n in shared}
                notes.extend(shared)
        scored_facts = sorted(enumerate(facts), key=lambda pair: (-rank(query, body=pair[1]), pair[0]))
        selected_facts = [value for _, value in scored_facts[:16]]
        selected_facts = list(dict.fromkeys(selected_facts + facts[:8]))[:24]
        ranked = sorted(notes, key=lambda n: (-rank(query, n["title"], n["body"], n["tags"]), not n["pinned"]))
        chosen = [n for n in notes if n["pinned"]][:2]
        for note in ranked:
            if len(chosen) >= 4:
                break
            if rank(query, note["title"], note["body"], note["tags"]) > 0 and note not in chosen:
                chosen.append(note)
        known = {n["id"] for n in chosen}
        index = {n["id"]: n for n in notes}
        for note in list(chosen):
            for edge in note["links"]:
                if len(chosen) < 5 and edge["id"] not in known and edge["id"] in index:
                    chosen.append(index[edge["id"]])
                    known.add(edge["id"])
        facts_context, notes_context = [], []
        remaining = 6000
        for fact in selected_facts:
            snippet = fact[:min(600, remaining)]
            if not snippet:
                break
            facts_context.append(snippet)
            remaining -= len(snippet)
        remaining = 8000
        for note in chosen:
            value = {"id": note["id"], "kind": note["kind"], "title": note["title"],
                     "audience": "все игроки Minecraft" if note["id"] in shared_ids else "эта беседа",
                     "body": note["body"][:1000], "tags": note["tags"], "links": note["links"][:3]}
            size = len(json.dumps(value, ensure_ascii=False))
            if size > remaining:
                value["body"] = value["body"][:max(0, len(value["body"]) - (size - remaining))]
                size = len(json.dumps(value, ensure_ascii=False))
            if size <= remaining:
                notes_context.append(value)
                remaining -= size
        return {"facts": facts_context, "notes": notes_context}

    def list(self, actor, query="", offset=0):
        if not isinstance(query, str) or len(query) > 300 or type(offset) is not int or not 0 <= offset <= self.MAX_NOTES:
            raise ValueError("Поиск: до 300 символов.")
        all_notes = self.all_notes(actor)
        notes = all_notes
        if query.strip():
            notes = [n for n in notes if rank(query, n["title"], n["body"], n["tags"]) > 0]
            notes.sort(key=lambda n: -rank(query, n["title"], n["body"], n["tags"]))
        facts = self.fact_candidates(actor)
        if query.strip():
            facts = sorted((f for f in facts if rank(query, body=f) > 0), key=lambda f: -rank(query, body=f))
        return {"notes": notes[offset:offset + 20], "total": len(notes), "offset": offset,
                "facts": facts[:24], "index": [{"id": n["id"], "title": n["title"], "version": n["version"]} for n in all_notes]}

    def forget_fact(self, actor, fact):
        if not isinstance(fact, str) or not 1 <= len(fact) <= 1000:
            raise ValueError("Укажи точный сохранённый факт.")
        with self.app.guard, self.store.lock, self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            removed = self.store.db.execute("DELETE FROM facts WHERE actor=? AND fact=?", (actor, fact)).rowcount
            if removed:
                self.app.cancel_reply()
            return bool(removed)

    def export(self, actor):
        with self.store.lock:
            facts = [row[0] for row in self.store.db.execute("SELECT fact FROM facts WHERE actor=? ORDER BY at", (actor,))]
            return {"format": "joker-memory-v1", "created_at": self.store.now(),
                    "notes": self.all_notes(actor), "facts": facts}

    def command(self, text, actor):
        token, _, arg = text.strip().partition(" ")
        token = token.casefold()
        arg = arg.strip()
        if token in ("/заметки", "/notes"):
            data = self.list(actor, arg)
            return "\n".join(n["id"] + " · " + n["title"] for n in data["notes"]) or "Заметок пока нет."
        if token in ("/найди", "/find"):
            if not arg.strip():
                return "Напиши /найди и тему воспоминания."
            data = self.list(actor, arg)
            lines = [n["id"] + " · " + n["title"] + ": " + n["body"][:250] for n in data["notes"][:5]]
            return "\n".join(lines + data["facts"][:5]) or "В этой беседе ничего не нашёл."
        if token in ("/забудь", "/forget"):
            if NOTE_ID.fullmatch(arg):
                note = self.get(actor, arg)
                self.delete(actor, arg, note["version"])
                return "Заметка удалена из памяти. История беседы сохраняется."
            return ("Факт убран из сохранённой памяти. История беседы сохраняется."
                    if self.forget_fact(actor, arg) else "Такого сохранённого факта нет.")
        if token in ("/связь", "/link"):
            parts = [p.strip() for p in arg.split("|")]
            if len(parts) != 3:
                return "Формат: /связь ID первой заметки | ID второй | описание связи."
            note = self.get(actor, parts[0])
            self.link(actor, parts[0], parts[1], parts[2], note["version"])
            return "Заметки связаны."
        if token not in ("/заметка", "/note"):
            return None
        if NOTE_ID.fullmatch(arg):
            note = self.get(actor, arg)
            return note["title"] + "\n" + note["body"] + "\nСвязи: " + ", ".join(n["title"] for n in note["links"])
        parts = [p.strip() for p in arg.split("|", 2)]
        kinds = {value: key for key, value in KINDS.items()}
        if len(parts) != 3 or parts[0].casefold() not in kinds:
            return "Формат: /заметка человек | Имя | Что помнить. Типы: человек, место, договорённость, событие, заметка."
        note = self.save(actor, title=parts[1], body=parts[2], kind=kinds[parts[0].casefold()])
        return "Запомнил: " + note["title"] + " · " + note["id"]
