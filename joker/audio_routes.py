"""Explicit audio routes, stable across PortAudio index changes; no default-device fallback."""
import hashlib

from .backends import ModelError


class RouteConflict(ValueError):
    pass


def device_id(name, hostapi):
    return hashlib.sha256((hostapi + "\0" + name).encode()).hexdigest()[:24]


def driver():
    try:
        import sounddevice
        return sounddevice
    except (ImportError, OSError):
        raise ModelError("Аудиодрайвер недоступен. Установи sounddevice и проверь PortAudio.") from None


class AudioRoutes:
    def __init__(self, app):
        self.app = app

    def saved(self):
        return self.app.store.get("audio_routes", {"version": 0})

    def configured(self, kind):
        saved = self.saved()
        return bool(saved[kind]) if kind in saved else bool(self.legacy(kind))

    def legacy(self, kind):
        return (self.app.settings.voice_device if kind == "output" else
                self.app.settings.voice_input_device)

    def inventory(self):
        sounddevice = driver()
        try:
            hosts = sounddevice.query_hostapis()
            devices = sounddevice.query_devices()
            result = [{"id": device_id(d["name"], hosts[d["hostapi"]]["name"]),
                       "index": i, "name": d["name"], "hostapi": hosts[d["hostapi"]]["name"],
                       "input_channels": d["max_input_channels"],
                       "output_channels": d["max_output_channels"]} for i, d in enumerate(devices)]
            for device in result:
                device["ambiguous"] = sum(d["id"] == device["id"] for d in result) != 1
            return result
        except (sounddevice.PortAudioError, OSError, RuntimeError, KeyError, IndexError):
            raise ModelError("Список аудиоустройств недоступен. Проверь sounddevice и звуковой драйвер.") from None

    def resolve(self, kind, devices=None):
        devices = self.inventory() if devices is None else devices
        saved = self.saved()
        if kind in saved:
            target = saved[kind]
            matches = [] if target is None else [d for d in devices
                if d["name"] == target["name"] and d["hostapi"] == target["hostapi"]]
        else:
            target = self.legacy(kind)
            # Legacy .env names must resolve exactly and unambiguously; never pick a default.
            matches = [d for d in devices if d["index"] == int(target)] if target.isdecimal() else [
                d for d in devices if d["name"] == target]
        if len(matches) != 1 or matches[0]["ambiguous"] or matches[0][kind + "_channels"] < 1:
            raise ModelError(("Выход" if kind == "output" else "Вход") +
                             " звука не выбран или недоступен. Обнови список устройств в панели.")
        return matches[0]

    def snapshot(self):
        saved = self.saved()
        result = {"version": saved["version"]}
        for kind in ("output", "input"):
            target = saved.get(kind)
            result[kind] = {"configured": self.configured(kind),
                            "id": device_id(target["name"], target["hostapi"]) if target else None,
                            "label": target["name"] + " · " + target["hostapi"] if target else
                                     self.legacy(kind) if kind not in saved else ""}
        return result

    def report(self):
        devices = self.inventory()
        saved = self.snapshot()
        for kind in ("output", "input"):
            try:
                device = self.resolve(kind, devices)
                saved[kind].update(id=device["id"], available=True)
            except ModelError:
                saved[kind]["available"] = False
        return {"devices": devices, "routes": saved}

    def save(self, version, output, input):
        if type(version) is not int or version < 0 or any(
                value is not None and not isinstance(value, str) for value in (output, input)):
            raise ValueError("Неверный профиль аудиоустройств.")
        with self.app.guard:
            if self.app.voice.enabled:
                raise RouteConflict("Сначала выключи голос, затем меняй аудиоустройства.")
            if self.saved()["version"] != version:
                raise RouteConflict("Настройки звука уже изменились. Обнови список и повтори выбор.")
            devices = self.inventory()
            saved = {"version": version + 1}
            for kind, target in (("output", output), ("input", input)):
                matches = [d for d in devices if d["id"] == target and not d["ambiguous"]
                           and d[kind + "_channels"] > 0]
                if target is not None and len(matches) != 1:
                    raise RouteConflict("Устройство исчезло или не подходит. Обнови список и повтори выбор.")
                saved[kind] = {k: matches[0][k] for k in ("name", "hostapi")} if target else None
            self.app.voice.cancel_pending()
            self.app.store.set("audio_routes", saved)
            return self.snapshot()

    def stream_args(self, kind, rate, channels):
        device = self.resolve(kind)
        sounddevice = driver()
        try:
            args = {"device": device["index"], "samplerate": rate, "channels": channels, "dtype": "int16"}
            if device["hostapi"] == "Windows WASAPI":
                # Shared-mode OS conversion supports Piper's 22050 Hz on a 48000 Hz cable.
                args["extra_settings"] = sounddevice.WasapiSettings(auto_convert=True)
            check = sounddevice.check_output_settings if kind == "output" else sounddevice.check_input_settings
            check(**args)
            return args
        except (sounddevice.PortAudioError, OSError, RuntimeError, ValueError):
            raise ModelError("Аудиоустройство не поддерживает нужный формат. Выбери другой звуковой интерфейс "
                             "(для Windows можно WASAPI) и проверь настройки драйвера.") from None

    def preflight(self, mode):
        self.resolve("output")
        if mode == "conversation" and self.configured("input"):
            return self.stream_args("input", 48000, 1)
        return None
