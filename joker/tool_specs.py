"""Tool contracts, not rules for choosing a character's response."""


def obj(fields=None):
    fields = fields or {}
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


def string(limit):
    return {"type": "string", "maxLength": limit}


RESOURCE = obj({"item": string(80), "count": {"type": "integer", "minimum": 1, "maximum": 256}})
STATE = obj({"mood": string(160), "activity": string(240), "focus": string(500)})
ERRAND_STEP = obj({"action": {"type": "string", "enum": ["come", "collect", "deliver"]},
                  "item": string(80), "count": {"type": "integer", "minimum": 1, "maximum": 256},
                  "note": string(300)})
ERRAND_CREATE = obj({"title": string(200), "steps": {"type": "array", "items": ERRAND_STEP,
                                                   "minItems": 1, "maxItems": 8}})
ERRAND_CONTROL = obj({"id": string(40), "version": {"type": "integer", "minimum": 1, "maximum": 1000000},
                     "action": {"type": "string", "enum": ["resume", "pause", "cancel"]}})
TOOLS = {
    "game.observe": ("Read connection, current task, inventory and nearby players. Does not move.", obj(), False),
    "game.follow": ("Start following the configured owner; an ongoing task, not a completed arrival.", obj(), True),
    "game.come": ("Approach the configured owner, stopping when arrival is observed.", obj(), True),
    "game.collect": ("Gather an exact quantity into Joker's inventory. Does not hand items over.", RESOURCE, True),
    "game.deliver": ("Gather and hand the exact quantity to the configured owner. Pickup is not verified.", RESOURCE, True),
    "game.stop": ("Cancel the current game task and pending turns; report the actual stop acknowledgement.", obj(), True),
    "queue.create": ("Save 1-8 ordered Minecraft errands, initially paused for owner review. Only come, collect, deliver. "
                     "For come use item='' and count=1. Never invent quantities. Follow is open-ended, not a queue step.", ERRAND_CREATE, True),
    "queue.control": ("Resume, pause or cancel the owner's saved queue by its current ID and version. "
                      "Resume only on an explicit owner request. Cannot confirm or retry an uncertain step.", ERRAND_CONTROL, True),
    "memory.search": ("Retrieve relevant memories within this conversation's access scope.", obj({"query": string(500)}), False),
    "instructions.read": ("Read one workplace guide from the provided index.", obj({"guide": string(40)}), False),
    "role.select": ("Switch to an existing persona by its ID, preserving memories and the game task.",
                    obj({"id": string(48)}), True),
    "role.create": ("Create and adopt the requested fictional persona. description must be an exact excerpt of the "
                    "current owner's message. A separate profile builder receives only this excerpt and current persona.",
                    obj({"description": string(2000)}), True),
}
SCHEMA = obj({"reply": string(8000), "remember": {"type": "array", "items": string(300), "maxItems": 3},
    "tool": {"anyOf": [{"type": "null"}] + [obj({"name": {"type": "string", "enum": [name]},
        "arguments": spec[1]}) for name, spec in TOOLS.items()]},
    "state": {"anyOf": [{"type": "null"}, STATE]}})


def validate(value, schema):
    """Small strict validator for the contracts above, including API-backend answers."""
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict) or set(value) != set(schema["properties"]):
            raise ValueError("Неверные поля инструмента.")
        for key, definition in schema["properties"].items():
            validate(value[key], definition)
    elif kind == "string":
        if not isinstance(value, str) or len(value) > schema.get("maxLength", 8000):
            raise ValueError("Неверная строка инструмента.")
    elif kind == "integer":
        if type(value) is not int or not schema["minimum"] <= value <= schema["maximum"]:
            raise ValueError("Неверное число инструмента.")
    elif kind == "array":
        if not isinstance(value, list) or not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            raise ValueError("Неверное число элементов списка.")
        for item in value:
            validate(item, schema["items"])
    else:
        raise ValueError("Неизвестный тип схемы инструмента.")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Неверное значение инструмента.")
