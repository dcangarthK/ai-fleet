# Laptop 2 joins the fleet: owner checklist

Laptop 2 = MacBook Pro M1, 16 GB. It joins over Tailscale as `tag:llm-node`, serves Ollama models to the master,
and can run Claude Code / Codex jobs later. The master (this Mac, 48 GB) keeps the controller, LiteLLM, its own
runner and Ollama.

Every step here is yours to run; nothing in it is done automatically for you. Approving the node is always your
action in the console. `<master>` below means this Mac's Tailscale name, e.g. `duncans-mbp.tail1234.ts.net`
(shown after step M3).

## 0. Before you start

- [ ] The onboarding fixes on branch `fleet/onboarding` are merged into `~/ai-fleet`. The master serves
      `/enroll/onboard.sh` and runs `setup.sh` from that checkout, so the old scripts would be used otherwise.
- [ ] Both Macs are on power, with Homebrew installed (`brew --version`), and Laptop 2 has about 25 GB free disk
      (two 14B models are about 9 GB each).
- [ ] You have about 45 minutes. The enrollment code (step M4) expires after 30 minutes, so make it just before
      step L3.

## Master side

### M1. Tailnet policy (Tailscale admin console, once)
1. Admin console > **Access controls**: paste `tailscale/policy.hujson` and save. It makes you the owner of
   `tag:master` and `tag:llm-node`, allows the existing master → node model/inventory/runner traffic, and allows
   workers to push signed heartbeats to master TCP 8443. Worker → worker remains denied.
2. Admin console > **DNS**: MagicDNS on, and **HTTPS Certificates enabled** (`tailscale serve` needs them).

Success: the policy saves without test failures (the file has its own `tests` block).

### M2. Install Tailscale on the master and sign in
```bash
brew install --cask tailscale && open -a Tailscale     # sign in from the menu bar with your account
/Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4   # → a 100.x.y.z address
```

### M3. Run the full setup once
```bash
cd ~/ai-fleet && bash master/setup.sh
```
This is the one time the **full** setup runs (normally only `--services-only`). What it does that matters here:
- tags this Mac `tag:master` with `tailscale set --advertise-tags=tag:master` (or `tailscale up
  --advertise-tags=tag:master` on an older CLI). No `--reset`: your exit node, routes, DNS and Tailscale SSH settings
  are kept. If Tailscale refuses because other flags differ, setup stops and says so: run the command Tailscale
  prints (keeping `--advertise-tags=tag:master`) and re-run, or re-run `bash master/setup.sh --reset-tailscale` to
  accept resetting those settings. Setup never resets them without that flag;
- `tailscale serve --bg --https=8443 http://127.0.0.1:8701` (the token-protected port; never 8700);
- writes `FLEET_PUBLIC_HOST=<master>:8443` into the controller's launchd file, so the console prints the right
  enrollment command;
- re-bootstraps the LiteLLM gateway and re-registers the `codex` and `local-llm` MCP servers.

Success checks:
```bash
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
$TS status --json | python3 -c 'import json,sys; s=json.load(sys.stdin)["Self"]; print(s["DNSName"], s.get("Tags"))'
#   → duncans-mbp.tail1234.ts.net. ['tag:master']
$TS serve status                                     # → https://<master>:8443 proxy http://127.0.0.1:8701
curl -s http://127.0.0.1:8700/api/me                 # → {"scope":"admin","label":"local","local":true}
curl -s -o /dev/null -w '%{http_code}\n' https://<master>:8443/enroll/onboard.sh   # → 200 (public script)
curl -s https://<master>:8443/api/fleet              # → 403 "sign in: ..." (tokens required: good)
launchctl print gui/$(id -u)/local.ai-fleet.controller | grep FLEET_PUBLIC_HOST   # → <master>:8443
```
(If the two `https://<master>:8443` checks fail from the master itself, try them from your phone or another of
your devices: that is the path Laptop 2 uses.)
If you also set `FLEET_PUBLIC_HOST` yourself in `master/litellm/.env`, that value wins: it must end in `:8443`.

### M4. Make an enrollment code (just before L3)
Console http://127.0.0.1:8700/app/ > **Nodes** > **Add node**, or:
```bash
curl -s -X POST http://127.0.0.1:8700/api/enroll -H 'Content-Type: application/json' -H 'X-Fleet: 1' \
  -d '{"name":"laptop2"}' | python3 -m json.tool
```
You get `code` (10 characters, single use, 30 minutes) and `command`. The command should start with
`curl -fsS https://<master>:8443/enroll/onboard.sh` and end in `--enroll --master https://<master>:8443`; the
code is not in it (the console shows it separately), so it never lands in Laptop 2's shell history. If it says
`https://<master>/...` (no `:8443`) or a literal `<master>`, step M3's public host isn't set: add `:8443` yourself
when you type the address in L3.

## Laptop 2 side

### L1. Tailscale, signed in as you (not tagged yet)
```bash
brew install --cask tailscale && open -a Tailscale     # menu bar > Log in, same account as the master
tailscale ip -4 || /Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4   # → 100.x.y.z
curl -s -o /dev/null -w '%{http_code}\n' https://<master>:8443/enroll/onboard.sh  # → 200
```
Order matters: the policy lets *your* devices reach the master's port 8443, but not `tag:llm-node` machines.
The script therefore enrolls first and tags the machine afterwards.

### L2. Download the script to a file
```bash
curl -fsSo ~/onboard.sh https://<master>:8443/enroll/onboard.sh && bash -n ~/onboard.sh && echo ok
```
Running from a file is better than `curl ... | bash`: you can read it first and re-run it later. (The console's
piped command also works: prompts, including the hidden code prompt, read the terminal.)

### L3. Run it with the code
```bash
bash ~/onboard.sh --enroll --master https://<master>:8443 --models "qwen2.5-coder:14b phi4:14b"
#   Enrollment code from the console (input hidden): <paste the code, Enter>
```
The code is never typed on the command line, so it stays out of shell history and the process list.
Without a terminal to prompt on, put the code in a private file instead and pass `--enroll-file`; the script
refuses the file unless it is yours and mode `0600` or `0400` (no group or other access):
```bash
(umask 077; pbpaste > ~/.fleet-enroll-code)    # the code is on the clipboard; the file is created 0600
bash ~/onboard.sh --enroll-file ~/.fleet-enroll-code --master https://<master>:8443 \
  --models "qwen2.5-coder:14b phi4:14b"
rm -f ~/.fleet-enroll-code
```
`--enroll <CODE>` still works but prints a warning: the code lands in your shell history.
- Leave out `--allow-root` for now: Laptop 2 starts as a model node. Agents can be added later (section A).
- Add `--yes` to accept every default without prompts; add `--remote-models` if you want the console's Models
  page to install/remove models here (off by default).
- It asks for your password once if Homebrew needs it.

What success looks like in its output:
1. `Enrolling <laptop2-name> with https://<master>:8443` → `Enrolled as '<laptop2-name>'; it will show as pending`.
2. `Joining the tailnet as tag:llm-node` (a login URL may appear: open it and approve).
3. `Configuring Ollama to listen on the tailnet only (100.x.y.z:11434)`, then the model downloads.
4. The inventory summary (`Model servers: ollama :11434 2 models [fleet-ready]`).
5. `Installing the agent runner on 100.x.y.z:11436 (max mode: auto-edit)`, then `Done`.

Exit code 2 means enrollment did not complete, and the script stopped **before** tagging the machine or
installing any services (Tailscale, python3 and `~/.ai-fleet/runner.token` may already exist), so it can still
reach the master: make a new code (M4) and re-run L3. It also prints the manual token steps (Troubleshooting, row
"enrollment") if you would rather join without enrollment.

Local checks on Laptop 2:
```bash
IP=$(tailscale ip -4 2>/dev/null || /Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4)
launchctl list | grep ai-fleet          # → local.ai-fleet.ollama, .info, .scan, .runner
curl -s http://$IP:11434/api/tags | python3 -c 'import json,sys; print([m["name"] for m in json.load(sys.stdin)["models"]])'
curl -s http://$IP:11435/fleet.json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["hardware"])'
# the runner needs its token even for /health; this passes it as a header file, not in argv or on screen
curl -s -H @<(printf 'X-Runner-Token: %s\n' "$(cat ~/.ai-fleet/runner.token)") http://$IP:11436/health \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["ok"], d["max_jobs"], d["power"], d["agents"])'
python3 -m json.tool ~/.ai-fleet/runner.json   # never print ~/.ai-fleet/runner.token
```
Expected `runner.json` on a 16 GB Mac: `max_jobs: 1`, `max_mode: "auto-edit"`, `allowed_roots: []`,
`power.min_battery_pct: 30`, `models: {remote_management: false, max_loaded: 1, disk_reserve_gb: 8,
ram_reserve_gb: 4}`, gate profiles `pytest` and `unittest`.

### L4. Docker Model Runner smoke check (optional, owner-run)

After you install Docker Desktop and enable Docker Model Runner yourself, run the repository's read-only check:

```bash
cd ~/ai-fleet
bash nodes/check_dmr.sh
```

It saves three fact files under a timestamped `dmr-fixtures/` directory: `docker model ls`,
`GET http://127.0.0.1:12434/engines/v1/models`, and `lsof` output for TCP 12434. Any wildcard, LAN, or tailnet
listener is a `FAIL`; do not continue with DMR until Docker Desktop exposes it on loopback only. The script does
not change Docker, Tailscale, or firewall state.

To enable the adapter after that check passes, add this node-local block to `~/.ai-fleet/runner.json` and restart
the runner:

```json
"engines": {
  "dmr": {
    "enabled": true,
    "base_url": "http://127.0.0.1:12434/engines/v1"
  }
}
```

Only `127.0.0.1`, `localhost`, or `::1` base URLs are accepted. DMR's model port is never added to Tailscale;
catalog and inference go through the token-checked runner on TCP 11436.

### L5. 16 GB tuning (optional, yours to run)
- The Ollama service is set to one loaded model, one request at a time, 16k context and q8 KV cache.
- More GPU memory for a 14B model with 16k context (resets on reboot):
  `sudo sysctl iogpu.wired_limit_mb=12288`
- System Settings > Battery > Options > *Prevent automatic sleeping on power adapter when the display is off*.
- If Ollama.app is in Login Items, remove it (the fleet runs its own `ollama serve`; two servers clash on 11434).

## Back on the master: approve and assign

### N1. See it pending
Console > **Nodes**: Laptop 2 appears within about 30 s as **pending**, with hardware (16 GB, Apple Silicon), its
models, and a runner. Or:
```bash
curl -s http://127.0.0.1:8700/api/fleet | python3 -c '
import json,sys
for n in json.load(sys.stdin)["nodes"]:
    print(n["id"], n["name"], n["status"], n.get("runner_ok"), n.get("models"))'
```
`runner_ok` true means the master holds this node's runner token (from enrollment) and the runner answered.

### N2. Approve (you, in the console)
Nodes > Laptop 2 > **Approve**. Claude and the fleet never approve nodes. Then check the node's mode ceiling on the
same page (default `auto-edit`; with no agents installed and no allowed roots it doesn't matter yet).

### N3. Roles for a 16 GB node
Only one 14B model fits in memory at a time, and switching costs a 9 GB reload (10 to 30 s). So give Laptop 2
**one hot model**:

| Role | Laptop 2 | Master | Why |
|---|---|---|---|
| `coder` | `qwen2.5-coder:14b` | `qwen2.5-coder:14b` | Second replica: LiteLLM spreads load and fails over between the two |
| `fast` | `qwen2.5-coder:14b` (same model: no swap) | `qwen2.5-coder:14b` | Same loaded model serves both roles |
| `reviewer` | none | `glm-4.7-flash` (derived, 32k) | glm needs 19 GB: does not fit on 16 GB |
| (spare) | `phi4:14b` installed, not assigned to a busy role | | For evals or as a manual fallback; assigning it next to qwen makes the node thrash |

Set them on the console's **Roles** page (or ask Claude to "assign laptop2's qwen2.5-coder:14b to coder and fast").
Keep any model settings at `num_ctx` 16384 or below on this node. Success: the Roles page shows both nodes under
`coder` as **Live** (routed), and `/api/fleet` shows the assignment with `routed: true`.

### N4. Smoke test
```bash
curl -s -X POST http://127.0.0.1:8700/api/tasks -H 'Content-Type: application/json' -H 'X-Fleet: 1' -d '{
  "agent":"ollama","model":"qwen2.5-coder:14b","mode":"read-only","worktree":false,"node":"<laptop2-name>",
  "title":"Laptop 2 smoke test","prompt":"Reply with the single word: ready"}'
```
Then watch it on the Tasks page; it should finish with `ready` (the first call loads the model, about 20 s).

## A. Agents on Laptop 2 later (Claude Code / Codex)

The runner already starts agents with their **own profiles**, separate from your logins on that Mac:
`CLAUDE_CONFIG_DIR=~/.claude-fleet` and `CODEX_HOME=~/.codex-fleet`. To enable them:
1. Install the CLIs, then sign the fleet profiles in once, as your user on Laptop 2:
   `CLAUDE_CONFIG_DIR=~/.claude-fleet claude` (then `/login`) and `CODEX_HOME=~/.codex-fleet codex login`.
   They use the same account limits as your own sessions.
2. Clone the repos there and allow them. For a roots-only change, edit `allowed_roots` in
   `~/.ai-fleet/runner.json` and restart the runner (`launchctl kickstart -k gui/$(id -u)/local.ai-fleet.runner`).
   Re-running `bash ~/.ai-fleet/onboard.sh --allow-root ~/ai-fleet` also works, but `--allow-root` **replaces**
   the whole roots list (pass every root you want), and a re-run regenerates the LaunchAgent plists: edits you
   made to the Ollama service's environment are lost, and the services restart.
3. For pipelines, add the gate profile the master uses: `"fleet-gate": ["bash", "scripts/gate.sh"]`.
4. `power.min_battery_pct` (30) keeps Claude/Codex jobs off the battery below 30%; keep it on a laptop.
5. **Pin repo tasks to a node** (`"node": "master"` or Laptop 2) until the controller checks repo paths: it picks
   the least-busy node that has the agent, and does not check that the repo path exists there or is allowed.
   A task for `/Users/duncansynthesis.co.za/ai-fleet` sent to Laptop 2 fails if that path isn't there.
   Note the runner also finds Claude Code bundled with the Claude desktop app, so installing that app is enough
   for Laptop 2 to start advertising `claude`.

## Rollback / leave the fleet

Laptop 2:
```bash
for j in runner info scan ollama; do
  launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/local.ai-fleet.$j.plist 2>/dev/null
  rm -f ~/Library/LaunchAgents/local.ai-fleet.$j.plist
done
rm -rf ~/.ai-fleet ~/.claude-fleet ~/.codex-fleet    # runner token, policy, job logs, fleet agent logins
ollama rm qwen2.5-coder:14b phi4:14b                  # optional: frees ~18 GB
brew services start ollama                            # optional: back to a normal localhost Ollama
```
Then Tailscale admin console > Machines > Laptop 2 > **Remove** (or *Edit ACL tags* to drop `tag:llm-node`).

Master:
1. Roles page: remove Laptop 2's assignments first (or `DELETE /api/roles` per assignment).
2. Nodes page: **Drain** Laptop 2 (`POST /api/nodes/<name>/drain`): no new tasks or routes.
3. `uv run --script master/controller/controller.py runner-token clear <laptop2-name>`.

Undo the master side of this checklist (rarely needed): `tailscale serve --https=8443 off`; the `tag:master` tag
stays (removing it breaks the policy's master → node rules).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `setup.sh`: "Could not tag this Mac as tag:master" | Policy not pasted, so you don't own the tag; or Tailscale wants your other non-default flags repeated | Step M1, then re-run `bash master/setup.sh`; or run the command Tailscale printed, keeping `--advertise-tags=tag:master`; or re-run with `--reset-tailscale` (resets exit node, routes, DNS, SSH) |
| `setup.sh`: "tailscale serve failed" | HTTPS certificates off | Admin console > DNS > enable, then run the printed `serve` command |
| Console's enroll command has no `:8443` or says `<master>` | `FLEET_PUBLIC_HOST` unset or set without port | Re-run `bash master/setup.sh --services-only` after Tailscale is up; fix `.env` if you set it there; or just type `https://<master>:8443` |
| onboard: "Could not reach https://<master>:8443" | Laptop 2 not signed in, already tagged, wrong URL, or `serve` not running | L1 checks; `tailscale serve status` on the master |
| onboard: "already tagged tag:llm-node, so it cannot reach the master" | Tagged earlier (by hand, or by an older script after a failed enrollment) | Use the manual token steps it prints (row below), or untag it in the admin console, then re-run with a new code |
| onboard: "refused the enrollment code" / exit 2 (enrollment) | Code wrong, used, or older than 30 minutes | The machine is still untagged: new code (M4), re-run L3. Or manual: re-run without `--enroll`, then on Laptop 2 `cat ~/.ai-fleet/runner.token`; on the master, once the node shows, `uv run --script master/controller/controller.py runner-token set <laptop2-name>` and paste it at the hidden prompt |
| onboard: "`--enroll-file` ... is readable by others" or "must be owned by" | The code file is not private | `chmod 600 <file>` (and create it as yourself), then re-run |
| onboard: "`--enroll` without a code needs a terminal" | Run with no terminal (e.g. over a non-interactive ssh) | Use `--enroll-file` (L3) |
| onboard: "Too many failed enrollment attempts" (429) | 5 failures in 10 minutes (shared by all devices, because behind `tailscale serve` every client is 127.0.0.1) | Wait 10 minutes, new code |
| onboard stops with "Could not tag this machine as tag:llm-node" | Policy missing, or Tailscale wants existing flags repeated | Step M1; or run the command Tailscale suggests, keeping `--advertise-tags=tag:llm-node`. If enrollment already succeeded, re-run without `--enroll`; the token is already registered, so ignore the manual steps |
| Node never appears in Nodes | Not tagged `tag:llm-node`, offline, or the master's Tailscale is down | `tailscale status` on the master should list Laptop 2 with `tag:llm-node` |
| Node appears, models empty / "LOCAL ONLY" | Ollama bound to localhost (Ollama.app also running) or macOS firewall blocking | Quit Ollama.app and remove it from Login Items; System Settings > Network > Firewall: allow `ollama` and `python3` incoming |
| `runner_ok` false | Token not stored (enrollment failed) or runner down | Manual token steps above; on Laptop 2 `tail ~/.ai-fleet/runner.log` and `launchctl kickstart -k gui/$(id -u)/local.ai-fleet.runner` |
| Node shows "update available" (right after onboarding, or after the master updates) | Old script: it installed `runner.py` with build markers, so digests never matched; or the master's runner is newer | First copy a fresh `onboard.sh` to Laptop 2 (e.g. `scp ~/ai-fleet/nodes/onboard.sh laptop2:~/onboard.sh` from the master; the policy grants your admin devices tcp:22), then run `bash ~/onboard.sh` there. Re-running the saved `~/.ai-fleet/onboard.sh` reinstalls its own embedded runner, so it doesn't update the runner |
| Re-run fails with "Bootstrap failed: 5: Input/output error" | Old script reloaded launchd jobs too fast | Fixed (waits and retries); with the old script, run it again |
| `--yes` run dies with "ALLOW_ROOTS[@]: unbound variable" | Old script under macOS's bash 3.2 | Fixed; with the old script pass `--allow-root <dir>` |
| Role not **Live**, or calls to Laptop 2 time out | LiteLLM (in Docker) can't reach the tailnet IP, or the node is asleep | From the master: `curl -s http://<laptop2-ip>:11434/api/tags`; keep Laptop 2 awake on power |
| Slow answers, model reloads every call | Two models assigned on Laptop 2 and used in turn | One hot model per 16 GB node (N3) |
| Claude/Codex tasks refused "on battery (23%)" | `power.min_battery_pct` | Plug in, or lower it in `runner.json` (not recommended on a laptop) |
