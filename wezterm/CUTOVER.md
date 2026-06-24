# Fleet control plane — cutover runbook

Everything is built on branch `feat/fleet-control-plane` and is **inert** until the
steps below. Live Tilix sessions keep using the old `tab-title.sh` hook until step 3.

Order: do **1** and **2** anytime (additive, safe). Do **3** only after you've
quiesced sessions (it changes running-session behavior — sounds start, hooks swap).

---

## 1. Installs (sudo — run these yourself via `! …`)

WezTerm — use the **apt repo, NOT flatpak** (flatpak sandboxing breaks `wezterm cli`
↔ GUI and reading ~/.claude):
```
curl -fsSL https://apt.fury.io/wez/gpg.key | sudo gpg --yes --dearmor -o /usr/share/keyrings/wezterm-fury.gpg
echo 'deb [signed-by=/usr/share/keyrings/wezterm-fury.gpg] https://apt.fury.io/wez/ * *' | sudo tee /etc/apt/sources.list.d/wezterm.list
sudo apt update && sudo apt install -y wezterm
```
Browser-window correlation (Phase 3):
```
sudo apt install -y wmctrl xdotool
```

## 2. Wire-up (non-sudo — I can run these)
```
mkdir -p ~/.config/wezterm ~/.claude/fleet
ln -sf ~/.claude/wezterm/wezterm.lua ~/.config/wezterm/wezterm.lua
[ -f ~/.claude/fleet/families.json ] || cp ~/.claude/wezterm/families.example.json ~/.claude/fleet/families.json
# ensure ~/.claude/bin is on PATH (add to ~/.bashrc if missing):
case ":$PATH:" in *":$HOME/.claude/bin:"*) ;; *) echo 'export PATH="$HOME/.claude/bin:$PATH"' >> ~/.bashrc ;; esac
```
Then edit `~/.claude/fleet/families.json` for your real project clusters.

## 3. Flip live config (I apply on your go, after you quiesce)
Two in-place edits — the only changes that touch live config:

**a. `settings.json`** — repoint every `hooks/tab-title.sh '<marker>'` to
`hooks/session-state.sh` (it derives state from the event, no arg); make the
`Notification` hook fire on *all* types (drop the no-op `idle_prompt` matcher; the
script filters by `notification_type`); add `"CLAUDE_CODE_NO_FLICKER": "1"` to `env`
(belt-and-suspenders garble fix; moot under WezTerm's renderer but harmless).

**b. `statusline.sh`** — prepend a wait-escalation glyph read from
`~/.claude/state/<sid>.json` (○ waiting <2m → ● yellow <10m → ● red ≥10m).

`hooks/tab-title.sh` is then unused (kept until you confirm; remove later).

## 4. Validate
```
node ~/.claude/lib/doctor.mjs && echo doctor-ok
wezterm --config-file ~/.claude/wezterm/wezterm.lua show-keys | head    # config loads + keys
```
Then launch WezTerm and, in a Claude session:
- a `state/<sid>.json` appears; the tab shows `project label ●` when idle
- `CTRL+SHIFT+Space` → picker lists sessions; Enter jumps
- `claude-sound test` plays; idling a session plays the waiting sound
- `claude-search <word>` finds live sessions
- `CTRL+SHIFT+O` workspaces; `CTRL+SHIFT+G` launch-family
- tab turns yellow→orange→red the longer a session waits

## 5. Merge
```
cd ~/.claude && git checkout master && git merge --no-ff feat/fleet-control-plane
```

---

## Keys
| Key | Action |
|-----|--------|
| `CTRL+SHIFT+Space` | Session picker (jump to any session) |
| `CTRL+SHIFT+O` | Workspace switcher (clusters) |
| `CTRL+SHIFT+G` | Launch a family of sessions |

## Commands
| Command | Purpose |
|---------|---------|
| `claude-sessions [--json\|--group]` | The fleet registry |
| `claude-search PATTERN [--all]` | Search transcripts (live by default) |
| `claude-sound on\|off\|status\|test [--session SID]` | Waiting-sound toggle |
| `claude-resume [--dry-run]` | Reopen snapshot sessions as WezTerm tabs |
| `claude-open <url>` / `claude-window [sid]` | Tag/focus a session's browser window |

## Per-session metadata (drives picker sort + grouping)
`~/.claude/meta/<sid>.json` → `{ "priority": 10, "group": "demo-app", "label": "auth bug" }`
Higher priority floats up; group shows in the picker and `claude-sessions --group`.

## Known validation points (couldn't test without WezTerm running)
- The `'Space'` keyname — if WezTerm errors at load, swap to a letter or `phys:Space`.
- `MuxPane:activate()` for the jump — pcall'd with a tab-activate fallback.
- `wezterm.mux.spawn_window{workspace=…}` for launch-family.
- `'Tokyo Night'` color scheme name.
All surface immediately in step 4's `wezterm --config-file … show-keys`.
