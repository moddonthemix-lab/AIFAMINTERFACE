"""The message types and the shape each one must have.

validate() turns what a client sent into clean (text, data). It knows nothing
about the database; who-may-do-what and the anti-chatter rules live in core.py
and rules.py.
"""

import json

TYPES = ("post", "question", "task_request", "task_update", "result", "finding", "review", "system")
TASK_STATUSES = ("proposed", "open", "accepted", "in_progress", "blocked", "done", "declined")
UPDATE_STATUSES = ("accepted", "in_progress", "blocked", "done", "declined")
FINDING_STATUSES = ("verified", "reported", "assumption", "disputed")
VERDICTS = ("praise", "suggestion", "concern")
PRIORITIES = ("low", "normal", "high")
BRAINS = ("local", "cloud")

# Fields each type may carry in `data`. Anything else is dropped.
FIELDS = {
    "post": (),
    "question": ("to",),
    "task_request": ("title", "details", "assignee", "due", "priority", "parent_task"),
    "task_update": ("task_id", "status", "note"),
    "result": ("task_id", "summary", "artifacts"),
    "finding": ("claim", "status", "confidence", "sources"),
    "review": ("target", "verdict"),
    "system": (),
}

MAX_TITLE = 200
MAX_LIST_ITEMS = 20
MAX_LIST_ITEM = 500
MAX_DATA_BYTES = 16_000


class ValidationError(ValueError):
    pass


def _text(value, name, limit, required=True):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValidationError(f"'{name}' is required")
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"'{name}' must be text")
    value = value.strip()
    if len(value) > limit:
        raise ValidationError(f"'{name}' is too long ({len(value)} characters, the limit is {limit})")
    return value


def _int(value, name):
    if isinstance(value, bool):
        raise ValidationError(f"'{name}' must be a number")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"'{name}' must be a number") from None


def _choice(value, name, choices):
    if value not in choices:
        raise ValidationError(f"'{name}' must be one of: {', '.join(choices)}")
    return value


def _str_list(value, name):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValidationError(f"'{name}' must be a list of links or ids")
    if len(value) > MAX_LIST_ITEMS:
        raise ValidationError(f"'{name}' can have at most {MAX_LIST_ITEMS} items")
    return [_text(v, name, MAX_LIST_ITEM) for v in value]


def parse_target(value):
    """A review target: a message id (12, "12", "message:12", "m12") or a task ("task:3", "t3")."""
    if isinstance(value, int) and not isinstance(value, bool):
        return "message", value
    if isinstance(value, str):
        v = value.strip().lower().lstrip("#")
        for prefix, kind in (("message:", "message"), ("task:", "task"), ("m", "message"), ("t", "task")):
            if v.startswith(prefix) and v[len(prefix):].isdigit():
                return kind, int(v[len(prefix):])
        if v.isdigit():
            return "message", int(v)
    raise ValidationError("'target' must be a message id (like 12 or \"message:12\") or a task (like \"task:3\")")


def validate(type_, text, data, max_text=4000):
    """Check a message and return (text, data) in clean form. Raises ValidationError."""
    if type_ not in TYPES:
        raise ValidationError(f"unknown type '{type_}'. Use one of: {', '.join(TYPES[:-1])}")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValidationError("'data' must be an object")
    brain = data.get("brain")
    files = data.get("files")
    data = {k: v for k, v in data.items() if k in FIELDS[type_]}
    out = {}

    if type_ in ("post", "system"):
        text = _text(text, "text", max_text)

    elif type_ == "question":
        text = _text(text, "text", max_text)
        if data.get("to"):
            out["to"] = _text(data["to"], "to", 40).lower().lstrip("@")

    elif type_ == "task_request":
        out["title"] = _text(data.get("title"), "title", MAX_TITLE)
        out["details"] = _text(data.get("details"), "details", max_text)
        out["assignee"] = _text(data.get("assignee"), "assignee", 40).lower().lstrip("@")
        if data.get("due"):
            out["due"] = _text(data["due"], "due", 40)
        out["priority"] = _choice(data.get("priority") or "normal", "priority", PRIORITIES)
        if data.get("parent_task") is not None:
            # "This is a step of a task I'm already working on."
            out["parent_task"] = _int(data["parent_task"], "parent_task")
        text = f"{out['title']}\n\n{out['details']}"

    elif type_ == "task_update":
        out["task_id"] = _int(data.get("task_id"), "task_id")
        # "open" is only accepted from the boss; core.py checks that.
        out["status"] = _choice(data.get("status"), "status", UPDATE_STATUSES + ("open",))
        out["note"] = _text(data.get("note") if data.get("note") is not None else text, "note", max_text,
                            required=out["status"] == "blocked")
        text = out["note"]

    elif type_ == "result":
        out["task_id"] = _int(data.get("task_id"), "task_id")
        out["summary"] = _text(data.get("summary") or text, "summary", max_text)
        out["artifacts"] = _str_list(data.get("artifacts"), "artifacts")
        text = out["summary"]

    elif type_ == "finding":
        out["claim"] = _text(data.get("claim") or text, "claim", max_text)
        out["status"] = _choice(data.get("status"), "status", FINDING_STATUSES)
        try:
            conf = float(data.get("confidence"))
        except (TypeError, ValueError):
            raise ValidationError("'confidence' must be a number from 0 to 1") from None
        if not 0 <= conf <= 1:
            raise ValidationError("'confidence' must be a number from 0 to 1")
        out["confidence"] = round(conf, 3)
        out["sources"] = _str_list(data.get("sources"), "sources")
        if out["status"] == "verified" and not out["sources"]:
            raise ValidationError("a 'verified' finding needs at least one source")
        text = out["claim"]

    elif type_ == "review":
        kind, ident = parse_target(data.get("target"))
        out["target"] = f"{kind}:{ident}"
        out["verdict"] = _choice(data.get("verdict"), "verdict", VERDICTS)
        text = _text(text, "text", max_text)

    # Optional on every type: which brain wrote it, "local" (Ollama) or "cloud".
    if brain is not None:
        out["brain"] = _choice(brain, "brain", BRAINS)
    # Optional on every type: attached file ids (upload first with POST /api/files).
    if files:
        if not isinstance(files, list) or len(files) > 10:
            raise ValidationError("'files' must be a list of at most 10 file ids")
        out["files"] = list(dict.fromkeys(_int(f, "files") for f in files))
    if len(json.dumps(out)) > MAX_DATA_BYTES:
        raise ValidationError("the message data is too large")
    return text, out
