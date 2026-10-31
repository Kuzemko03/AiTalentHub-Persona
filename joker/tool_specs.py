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
    "game.come": ("Approach ONLY the configured owner. Never use for a different named player. Stops on observed arrival.", obj(), True),
    "game.approach": ("Approach a specific currently visible player by their exact observed nickname; stop within 3 blocks. "
                      "Read game.observe to resolve names. Never substitutes the owner for a missing player.",
                      obj({"player": string(16)}), True),
    "game.say": ("Send one public Minecraft chat message and voice it if voice is enabled. request is a short "
                 "description of what to say, or a creative social brief when the owner delegates wording. "
                 "Do not copy private facts into the brief. A separate writer receives the brief, persona "
                 "and public game observations, never private history. Submission is not proof of delivery.",
                 obj({"request": string(1000)}), True),
    "game.walk": ("Walk to a chosen nearby coordinate to explore the world. Read game.observe for your position "
                  "and surface map first. One leg is at most 32 horizontal blocks and 8 vertical blocks. "
                  "Arrival is checked in the game; never claim a submitted path is completed.",
                  obj({"x": {"type": "integer", "minimum": -30000000, "maximum": 30000000},
                       "y": {"type": "integer", "minimum": -64, "maximum": 319},
                       "z": {"type": "integer", "minimum": -30000000, "maximum": 30000000}}), True),
    "game.collect": ("Gather an exact quantity into Joker's inventory. Does not hand items over.", RESOURCE, True),
    "game.deliver": ("Gather and hand the exact quantity to the configured owner. Pickup is not verified.", RESOURCE, True),
    "game.release": ("Stop only the current movement so you can continue this request with another action. "
                     "Does not cancel the dialogue or pause autonomous life. Cannot resolve uncertain resource delivery.", obj(), True),
    "game.stop": ("Full STOP requested by owner: cancels the current request and pauses autonomous life. "
                  "Never use as preparation for walking or socializing; use game.release or life.start instead.", obj(), True),
    "life.start": ("Start autonomous exploration/social activity from the owner's delegated goal. Choose this for "
                   "ongoing requests such as walking around, exploring buildings and meeting people without step-by-step orders. "
                   "The model will choose actions using current observations and current persona. Not for hypothetical requests.",
                   obj({"goal": string(1000)}), True),
    "channel.post": ("Prepare a post for the owner's configured Telegram channel. Use actual implementation facts "
                     "for technical topics. publish=true when the owner requests posting to the channel; false "
                     "when only a draft is requested. Only private owner conversations may use this tool.",
                     obj({"topic": string(1000), "publish": {"type": "boolean"}}), True),
    "channel.publish": ("Publish an existing draft by ID on the owner's explicit request. Private owner only. "
                        "Never retry uncertain delivery or enable scheduled posting.", obj({"id": string(40)}), True),
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
    elif kind == "boolean":
        if type(value) is not bool:
            raise ValueError("Неверный флаг инструмента.")
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
