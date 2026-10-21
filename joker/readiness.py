"""Read-only setup report; configuration is never presented as a completed live test."""
from .backends import ModelError
from .minecraft import NICK


def report(app):
    s = app.settings
    checks = []

    def add(id, title, ready, detail, optional=False):
        checks.append(dict(id=id, title=title, ready=bool(ready), detail=detail, optional=optional))

    nicks = (bool(NICK.fullmatch(s.minecraft_owner)) and bool(NICK.fullmatch(s.minecraft_bot))
             and s.minecraft_owner.casefold() != s.minecraft_bot.casefold())
    add("nicks", "Minecraft-ники", nicks, "Ники владельца и Джокера заданы и различаются." if nicks else
        "Задай MINECRAFT_OWNER и MINECRAFT_BOT в .env: два разных ника, 3–16 латинских символов, цифр или _.")
    connected = app.minecraft.status()["connected"]
    add("minecraft", "Подключение игры", connected, "Клиент подключён." if connected else
        "Запусти отдельный клиент Джокера с UnionClef и подключи его к миру.")
    tts = app.voice.tts_status()
    add("tts", "Синтез речи", tts["ready"], "Выбранный голос доступен: " + tts["selected_voice"] if tts["ready"] else
        "Установи Piper и модель голоса либо настрой HTTP TTS. Выбери голос для текущей роли.")
    try:
        output = app.voice.routes.resolve("output")
        add("output", "Выход речи", True, output["name"] + " · " + output["hostapi"] +
            ". Формат проверяется перед воспроизведением; маршрут в игру ещё нужно проверить на слух.")
    except ModelError as exc:
        add("output", "Выход речи", False, str(exc))
    conversation = app.voice.mode == "conversation"
    add("stt", "Распознавание речи", s.stt_url and s.stt_model,
        "STT настроен; живой запрос ещё не проверен." if s.stt_url and s.stt_model else
        "Нужны STT_URL и STT_MODEL для голосовых обращений. Озвучка текста работает без STT.", not conversation)
    if conversation and app.voice.routes.configured("input"):
        try:
            app.voice.routes.stream_args("input", 48000, 1)
            add("input", "Вход созвона", True, "Устройство доступно для PCM16 mono 48 кГц. Сам звук ещё не проверен.")
        except ModelError as exc:
            add("input", "Вход созвона", False, str(exc))
    add("live", "Проба в игре", False,
        "После подключения проверь обращение по имени, смену роли, озвучку и Стоп. Автоматически звук не включается.", True)
    return {"checks": checks, "configured": all(c["ready"] for c in checks if not c["optional"]),
            "mode": app.voice.mode, "note": "Это проверка настроек и доступности, а не результат живого игрового теста."}
