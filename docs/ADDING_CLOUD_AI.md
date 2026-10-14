# Adding cloud AI to Amo and Scholar

A handoff for the chat that builds **Amo** (`github.com/moddonthemix-lab/AMOAI`, port 8765) or **Scholar**
(`github.com/moddonthemix-lab/AI2-researcher`, port 8766). Goal: the agent thinks with **Claude in the cloud**
when it's allowed and online, and with its **local Ollama model** otherwise, within a monthly budget the boss
controls from the Family Hub.

**For the chat reading this:** read this whole file, then look at the agent's code to find where it calls its
AI model today. Show the boss a short plan and wait for their OK, then build in small, tested steps. The boss
is not a professional programmer: explain simply, and give terminal commands one at a time.

Background, if needed: [CONNECTING_AN_AGENT.md](CONNECTING_AN_AGENT.md) (how agents use the hub). This
file only covers the cloud brain.

---

## 1. What already exists (don't rebuild it)

- **The Family Hub** runs on the boss's Chromebook at `http://192.168.1.208:8770`. It holds each agent's
  **monthly cloud budget** (Admin page → ☁️ Cloud AI budgets; $0 = local only) and tracks spending.
- **[`family_brain.py`](../family_brain.py)**, in the hub repo. It makes the whole cloud-or-local decision
  in one call, checks the budget with the hub, calls Claude or Ollama, reports the cost, and handles outages
  and refusals. It's tested.
- **[`hub_client.py`](../hub_client.py)**, in the hub repo: how the agent talks to the hub. `family_brain`
  needs it for budgets.

Copy both files into the agent's project unchanged, so they can be updated later by copying again.

## 2. One-time setup on the Mac (walk the boss through it)

The Mac is an Intel MacBook (16 GB RAM, no GPU) running Ollama.

1. **Python 3.10 or newer.** macOS's built-in `python3` is 3.9, and the Anthropic SDK needs 3.10+.
   Install Python 3.12 from python.org (simplest for a non-programmer) or Homebrew. Then, in the agent's folder,
   create a virtual environment so nothing else on the Mac changes:
   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install anthropic
   ```
   Update how the agent is started (its start script, LaunchAgent, or README) to use `.venv/bin/python`.
2. **The API key.** The boss already has an Anthropic account and a key they made for the "AI family". It goes
   in the agent's `.env` (make sure `.env` is in `.gitignore`), never in code, chats, or the hub:
   ```bash
   ANTHROPIC_API_KEY=sk-ant-...
   LOCAL_MODEL=qwen3:8b
   HUB_URL=http://192.168.1.208:8770
   HUB_TOKEN=hub_amo_...      # from the Chromebook: python3 -m hub agent token amo
   ```
   If the agent already has a hub token, keep it. Running `hub agent token` again replaces the old one.
3. **A budget in the hub.** On the hub's Admin page → ☁️ Cloud AI budgets, give the agent a small budget to
   start (e.g. $1), then **Save budgets**. With $0 the brain always stays local.
4. **A second safety net:** in the Anthropic Console, set a monthly spend limit on the key's workspace, if
   the Console offers it.

## 3. Wiring it in

Find every place the agent currently asks its model for an answer (an Ollama call, a `chat()`/`ask()`
function, etc.) and route it through the brain:

```python
from hub_client import HubClient
from family_brain import FamilyBrain, BrainUnavailable

hub = HubClient()                                    # reads HUB_URL and HUB_TOKEN
brain = FamilyBrain(hub, system=PERSONALITY_PROMPT)  # the agent's existing system prompt / personality

def answer(prompt, job, local_only, history=None):
    try:
        t = brain.think(prompt, job=job, local_only=local_only, history=history)
    except BrainUnavailable as e:   # no cloud allowed AND Ollama not running
        return f"I can't think right now: {e}"
    if t.brain == "wait":
        return None                 # caller parks the work (see below)
    if t.refused:
        return f"I can't help with that one ({t.reason})."
    return t.text                   # t.brain is "cloud" or "local"; t.cost_usd, t.sources are there too
```

**`job` for each kind of work:**

| Agent | Situation | `job` | Model when cloud |
|---|---|---|---|
| Amo | Everyday chat with the boss | `chat` | Claude Haiku |
| Amo | A bigger job the boss asked for (plan, long write-up) | `big` | Claude Opus |
| Scholar | Research, fact-checking, business analysis | `research` | Claude Sonnet + web search |
| Scholar | Quick answers, explaining things to Baby | `chat` | Claude Haiku |
| either | Code | `code` | Claude Sonnet |

Use `big` only for work the boss created or approved: it's the most expensive.

**`local_only`: what may go to the cloud.**
- Messages from the hub carry `"local_only"`. Pass it straight through.
- **The agent's own private conversations with the boss** (e.g. Amo's chat window on port 8765) are not
  hub messages. Add a setting `CLOUD_FOR_PRIVATE_CHATS` in the agent's `.env`, **default `false`**, and pass
  `local_only=not CLOUD_FOR_PRIVATE_CHATS` there. The boss decides if their personal chats may use cloud AI.
  Tell them about this setting.
- The agent's personal memory, if it's included in a prompt, follows the same rule: it only goes to the cloud
  when the conversation it's used in is allowed to.

**When the brain says `"wait"`** (research while offline or out of budget):
- For a hub task: `hub.update_task(task_id, "blocked", "Waiting for internet or cloud budget.")`. Later
  (e.g. every 10 minutes, or when the hub sends an event) check `hub.tasks(assignee=<me>, status="blocked")`
  and retry.
- In the agent's own chat window: tell the boss plainly ("I need the internet for that research. I'll do it
  when we're back online."), and keep a small queue to retry.

**Show which brain answered.** Before posting to the hub, set `hub.brain = t.brain` (shows ☁️ / 🏠). In the
agent's own dashboard, show a small ☁️ or 🏠 next to each answer, and optionally the cost.

**Sources.** For Scholar's research, `t.sources` lists the web pages the cloud model searched. Use them as
`sources` on findings (`hub.post_finding(..., sources=t.sources)`). Only mark a finding `verified` when the
claim is supported by at least one reliable source.

## 4. Don'ts

- Don't call the Anthropic API anywhere except through `family_brain`. Otherwise spending isn't tracked or
  limited.
- Don't change `family_brain.py` inside the agent. If something is missing, tell the boss, so it gets fixed in
  the hub repo for every agent.
- Don't retry a refused request on the local model.
- Don't log or print the API key, or include it in error messages, commits or hub posts.
- The model names and prices in `family_brain.py` were current when written. If Anthropic's docs show
  different ones, tell the boss, and they'll be updated in the hub repo.

## 5. Done when (test with the boss, real API, small budget)

- [ ] With a **$0 budget**, the agent answers using the local model (🏠).
- [ ] With a **$1 budget**, a simple question is answered by the cloud (☁️), and the spend appears on the hub's
      **Today** page under ☁️ Cloud spending.
- [ ] Scholar: a research question uses Sonnet with web search and returns sources.
- [ ] A question in a 🏠 local-only room (e.g. `scholar+baby`) is answered locally even with budget left.
- [ ] Amo: private chat stays local while `CLOUD_FOR_PRIVATE_CHATS=false`.
- [ ] Wi-Fi off (or internet unplugged): chat still works locally; research waits, then resumes when back.
- [ ] Budget used up: the agent switches to local without errors, and the boss got an Inbox note at 80% / 100%.
- [ ] Ollama stopped and cloud not allowed: a clear "can't think right now" message, no crash.
- [ ] The API key appears in no file except `.env`, and `.env` is not committed.
