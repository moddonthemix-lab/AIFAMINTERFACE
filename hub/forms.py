"""Shared forms: one structured format every agent understands.

A form is a named list of fields (like "Trade setup": symbol, trigger, stop...).
Any message can carry a filled-in form in data.form = {"name", "items", "note"}.
The hub checks it, so the next agent gets exact data instead of prose to
interpret, and the web page shows it as neat cards. Each form also has a JSON
schema (GET /api/forms/<name>) that agents give to their AI, so the AI's answer
is forced into the right shape.
"""

import json
import re
import time

TYPES = ("text", "number", "choice", "numbers", "list", "yesno")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
MAX_ITEMS = 50
MAX_TEXT = 2000

# The trading forms (Amo ➜ Trader). The boss adds more on the Admin page.
BUILTIN = [
    {"name": "trade_setup", "title": "Trade setups", "list": True, "allow_empty": True,
     "description": "Setups for the next session, best first. Empty list + note when there are no good setups.",
     "fields": [
         {"name": "symbol", "type": "text", "required": True, "help": "Plain ticker, e.g. AAPL"},
         {"name": "timeframe", "type": "choice", "required": True,
          "choices": ["monthly", "weekly", "daily", "4h", "1h", "15m"]},
         {"name": "pattern", "type": "text", "required": True, "help": "Strat combo, e.g. 2-1-2 (inside bar)"},
         {"name": "direction", "type": "choice", "required": True, "choices": ["long", "short"]},
         {"name": "trigger", "type": "number", "required": True},
         {"name": "stop", "type": "number", "required": True},
         {"name": "targets", "type": "numbers", "required": True, "help": "Nearest first"},
         {"name": "invalidation", "type": "text", "required": False},
         {"name": "continuity", "type": "text", "required": False},
         {"name": "confidence", "type": "choice", "required": True, "choices": ["high", "medium", "low"]},
         {"name": "why", "type": "text", "required": True, "help": "One sentence, plain language"},
     ]},
    {"name": "setup_scorecard", "title": "Setup scorecard", "list": True, "allow_empty": True,
     "description": "How each setup from the last plan did. Put the day's score, running hit rate and one "
                    "lesson in the note.",
     "fields": [
         {"name": "symbol", "type": "text", "required": True},
         {"name": "pattern", "type": "text", "required": True},
         {"name": "direction", "type": "choice", "required": True, "choices": ["long", "short"]},
         {"name": "outcome", "type": "choice", "required": True,
          "choices": ["hit target", "hit stop", "still open", "not triggered"]},
         {"name": "r_multiple", "type": "number", "required": False, "help": "Result in R (risk units)"},
         {"name": "comment", "type": "text", "required": False},
     ]},
    {"name": "trade_report", "title": "Trade report", "list": True, "allow_empty": True,
     "description": "One row per setup from the plan (and per own trade). PASSED needs a market reason.",
     "fields": [
         {"name": "symbol", "type": "text", "required": True},
         {"name": "outcome", "type": "choice", "required": True,
          "choices": ["TAKEN", "NOT TRIGGERED", "NOT AVAILABLE", "PASSED", "BLOCKED"]},
         {"name": "reason", "type": "text", "required": False, "help": "Required for PASSED / BLOCKED",
          "required_when": {"field": "outcome", "in": ["PASSED", "BLOCKED"]}},
         {"name": "entry", "type": "number", "required": False},
         {"name": "exit", "type": "number", "required": False},
         {"name": "pnl_usd", "type": "number", "required": False, "help": "After fees"},
         {"name": "fees_usd", "type": "number", "required": False},
         {"name": "r_multiple", "type": "number", "required": False},
         {"name": "mode", "type": "choice", "required": False, "choices": ["paper", "live"]},
     ]},
]


def json_schema(form):
    """The JSON schema an AI should answer with: {"items": [...], "note": "..."}.
    Optional fields are nullable but always present (structured outputs need every key listed)."""
    def field_schema(f):
        t = f["type"]
        base = ({"type": "string"} if t == "text" else
                {"type": "number"} if t == "number" else
                {"type": "string", "enum": list(f["choices"])} if t == "choice" else
                {"type": "array", "items": {"type": "number"}} if t == "numbers" else
                {"type": "array", "items": {"type": "string"}} if t == "list" else
                {"type": "boolean"})
        if f.get("help"):
            base["description"] = f["help"]
        if f.get("required_when"):
            rw = f["required_when"]
            base["description"] = (base.get("description", "") + f" (required when {rw['field']} is "
                                   + " or ".join(rw["in"]) + ")").strip()
        return base if f.get("required") else {"anyOf": [base, {"type": "null"}]}

    item = {"type": "object", "additionalProperties": False,
            "properties": {f["name"]: field_schema(f) for f in form["fields"]},
            "required": [f["name"] for f in form["fields"]]}
    return {"type": "object", "additionalProperties": False,
            "properties": {"items": {"type": "array", "items": item},
                           "note": {"type": "string", "description": "Anything that doesn't fit the fields"}},
            "required": ["items", "note"]}


def check_value(f, v):
    """Clean one field's value, or raise ValueError with a plain message."""
    t, name = f["type"], f["name"]
    if v is None or v == "" or v == []:
        if f.get("required"):
            raise ValueError(f"'{name}' is required")
        return None
    if t == "text":
        v = str(v).strip()
        if len(v) > MAX_TEXT:
            raise ValueError(f"'{name}' is too long")
        return v
    if t == "number":
        if isinstance(v, bool):
            raise ValueError(f"'{name}' must be a number")
        try:
            return float(str(v).replace(",", "").replace("$", ""))
        except ValueError:
            raise ValueError(f"'{name}' must be a number, not '{v}'") from None
    if t == "choice":
        match = next((c for c in f["choices"] if c.lower() == str(v).strip().lower()), None)
        if match is None:
            raise ValueError(f"'{name}' must be one of: {', '.join(f['choices'])}")
        return match
    if t == "yesno":
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in ("yes", "true", "1"):
            return True
        if s in ("no", "false", "0"):
            return False
        raise ValueError(f"'{name}' must be yes or no")
    items = v if isinstance(v, list) else [x.strip() for x in str(v).split(",") if x.strip()]
    if len(items) > MAX_ITEMS:
        raise ValueError(f"'{name}' has too many values")
    if t == "numbers":
        return [check_value({"name": name, "type": "number", "required": True}, x) for x in items]
    return [check_value({"name": name, "type": "text", "required": True}, x) for x in items]


def check_filled(form, filled):
    """Validate {"name", "items", "note"} against `form`. Returns the cleaned version."""
    if not isinstance(filled, dict):
        raise ValueError("'form' must be an object: {name, items, note}")
    items = filled.get("items")
    if items is None and "fields" in filled:  # a single item given directly
        items = [filled["fields"]]
    if not isinstance(items, list):
        raise ValueError("'form.items' must be a list")
    note = str(filled.get("note") or "").strip()[:MAX_TEXT]
    if len(items) > MAX_ITEMS:
        raise ValueError(f"at most {MAX_ITEMS} items")
    if not form["list"] and len(items) != 1:
        raise ValueError(f"the '{form['name']}' form takes exactly one item")
    if not items and not (form.get("allow_empty") and note):
        raise ValueError(f"the '{form['name']}' form needs at least one item"
                         + (" (or an empty list with a note saying why)" if form.get("allow_empty") else ""))
    names = {f["name"] for f in form["fields"]}
    out = []
    for n, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f"item {n} must be an object")
        unknown = set(item) - names
        if unknown:
            raise ValueError(f"item {n}: unknown field(s) {', '.join(sorted(unknown))}")
        clean = {}
        for f in form["fields"]:
            try:
                clean[f["name"]] = check_value(f, item.get(f["name"]))
            except ValueError as e:
                raise ValueError(f"item {n}: {e}") from None
        for f in form["fields"]:
            rw = f.get("required_when")
            if rw and clean[f["name"]] is None and clean.get(rw["field"]) in rw["in"]:
                raise ValueError(f"item {n}: '{f['name']}' is required when {rw['field']} is {clean[rw['field']]}")
        out.append(clean)
    return {"name": form["name"], "items": out, "note": note}


def as_text(form, filled):
    """A readable version, for search and for agents that don't read forms."""
    lines = [form["title"].upper()]
    for n, item in enumerate(filled["items"], 1):
        lines.append(f"{n}. " + " · ".join(
            f"{k}: {', '.join(str(x) for x in v) if isinstance(v, list) else v}" for k, v in item.items()
            if v not in (None, [], "")))
    if filled["note"]:
        lines.append(filled["note"])
    return "\n".join(lines)


def check_definition(d):
    """Validate a form definition the boss wrote on the Admin page."""
    if not isinstance(d, dict):
        raise ValueError("a form must be an object")
    name = str(d.get("name") or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError("the form name must be short lowercase letters, numbers and _, like 'weekly_review'")
    title = str(d.get("title") or name.replace("_", " ").title()).strip()[:80]
    fields = d.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ValueError("a form needs at least one field")
    seen, clean = set(), []
    for f in fields[:40]:
        fname = str(f.get("name") or "").strip().lower().replace(" ", "_")
        if not NAME_RE.match(fname) or fname in seen:
            raise ValueError(f"field name '{fname}' is invalid or used twice (use lowercase letters and _)")
        seen.add(fname)
        t = f.get("type") or "text"
        if t not in TYPES:
            raise ValueError(f"field '{fname}': type must be one of {', '.join(TYPES)}")
        cf = {"name": fname, "type": t, "required": bool(f.get("required"))}
        if t == "choice":
            choices = f.get("choices")
            if isinstance(choices, str):
                choices = [c.strip() for c in choices.split(",")]
            choices = [str(c).strip() for c in (choices or []) if str(c).strip()]
            if len(choices) < 2:
                raise ValueError(f"field '{fname}': a choice needs at least two options")
            cf["choices"] = choices[:30]
        if f.get("help"):
            cf["help"] = str(f["help"])[:200]
        if f.get("required_when"):
            cf["required_when"] = f["required_when"]
        clean.append(cf)
    by_name = {f["name"]: f for f in clean}
    for cf in clean:  # "required when <field> is one of ...", e.g. a reason when the outcome is PASSED
        rw = cf.get("required_when")
        if rw is None:
            continue
        other = by_name.get(str((rw or {}).get("field") if isinstance(rw, dict) else ""))
        values = rw.get("in") if isinstance(rw, dict) else None
        if other is None or other["type"] != "choice" or not isinstance(values, list) or \
                not values or any(v not in other["choices"] for v in values):
            raise ValueError(f"field '{cf['name']}': 'required when' must name a choice field and some of its options")
        cf["required_when"] = {"field": other["name"], "in": list(values)}
    return {"name": name, "title": title, "description": str(d.get("description") or "")[:500],
            "list": bool(d.get("list", True)), "allow_empty": bool(d.get("allow_empty")), "fields": clean}


class FormsMixin:
    """Mixed into core.Hub."""

    def _form_row_dict(self, r):
        d = json.loads(r["definition"])
        d["builtin"] = bool(r["builtin"])
        return d

    def forms(self):
        return [self._form_row_dict(r) for r in self.q("SELECT * FROM forms ORDER BY builtin DESC, name")]

    def form(self, name, with_schema=False):
        from .core import HubError
        r = self.q1("SELECT * FROM forms WHERE name=?", (str(name or "").lower(),))
        if r is None:
            raise HubError(404, f"no form '{name}'. Forms: {', '.join(f['name'] for f in self.forms())}")
        d = self._form_row_dict(r)
        if with_schema:
            d["json_schema"] = json_schema(d)
        return d

    def save_form(self, definition, builtin=False):
        from .core import HubError
        try:
            d = check_definition(definition)
        except ValueError as e:
            raise HubError(400, str(e)) from None
        with self.tx() as c:
            c.execute("INSERT INTO forms(name, definition, builtin, updated_at) VALUES (?,?,?,?) "
                      "ON CONFLICT(name) DO UPDATE SET definition=excluded.definition, updated_at=excluded.updated_at",
                      (d["name"], json.dumps(d), int(builtin), time.time()))
            self._event(c, "form", {"name": d["name"]})
        return self.form(d["name"])

    def delete_form(self, name):
        from .core import HubError
        f = self.form(name)
        if f["builtin"]:
            raise HubError(400, "built-in forms can be edited but not deleted")
        with self.tx() as c:
            c.execute("DELETE FROM forms WHERE name=?", (f["name"],))
            self._event(c, "form", {"name": f["name"], "deleted": True})

    def ensure_builtin_forms(self):
        for d in BUILTIN:
            if self.q1("SELECT 1 FROM forms WHERE name=?", (d["name"],)) is None:
                self.save_form(d, builtin=True)

    def check_form(self, filled):
        """Validate data.form on a message. Returns (clean form data, readable text)."""
        from .core import HubError
        if not isinstance(filled, dict) or not filled.get("name"):
            raise HubError(400, "'form' needs a 'name' (see GET /api/forms)")
        f = self.form(filled["name"])
        try:
            clean = check_filled(f, filled)
        except ValueError as e:
            raise HubError(400, f"form '{f['name']}': {e}") from None
        return clean, as_text(f, clean)
