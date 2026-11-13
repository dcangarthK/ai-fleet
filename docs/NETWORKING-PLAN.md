# Networking plan (deadline 08:00, 13 Nov)

Mandate: build deferred phases of docs/SPEC-networking.md §10 on fakes. Nothing applied live; owner steps in
docs/NETWORKING-OWNER-CHECKLIST.md. Owner pushes. No Fable.

| Item | Task | Builder | State |
|---|---|---|---|
| N0 policy matrix + 11435 audit | t-bcdcbe01 | Sonnet | merged |
| N2 posture, /health, Live badge, probe/eval move to runner, ACL narrowing file | t-e03ce060 | Sonnet | merged |
| N3 Windows parity | t-07e37b01 | Codex terra medium | merged |
| N4 gateway adapter (fakes) | t-59e3232c | Sonnet | merged |
| Part D test follow-ups (t-5c1fc50f) | — | — | already done |
| Scope task t-939f6b2a | — | — | close (spec merged) |

Flow per item: gate -> Opus review -> fix -> Haiku delta re-review -> merge -> Codex terra high final over batch.

## Status
- 05:23 four builds spawned.
- 05:28 N0 gate fail (policy module shadowed by controller/policy.py) -> rename fix. N2 capped -> auto-continued t-8e5e0718.
- 05:33 N4 Opus review t-5baacb5a: request_changes (1 HIGH, 3 MED) -> fix round 1.
- 05:36 N0 Opus review t-7f4b8aee: request_changes -> fix round 1.
- 05:37 N3 gate passed but Codex left work uncommitted -> asked to commit.
- 05:39 N2 passed gate; Opus review split in 2 (t-a6c41b8f runner, t-bf08eafe controller/UI).
- 05:39 Read-only posture on this Mac (master): application firewall DISABLED (State = 0); pfctl needs root (collector must say
  unknown); engines on loopback: ollama 127.0.0.1:11434, runner 127.0.0.1:11436, controller 127.0.0.1:8700. -> owner checklist.
- 05:50 N4 merged after 2 fix rounds (Haiku re-review: 3 LOW, no blocker). Main gate 465/512/29/68 OK.
- 05:52 N2 round-1 gate: 2 test_controller failures, pass alone (load flake). Haiku re-review -> fix round 2 sent.
- 05:55 N0 round-2 re-review: non-numeric port accepted -> fix round 3 (last) sent. N3 fix round 1 in progress.
- 06:09 N0 merged (1f4d6f7) after round-3 re-review (only docstring length left). Gate 509/512/29/68 OK.
- 06:13 N2 merged (e15683a). Gate caught onboard test: taking main's onboard copy dropped N2's :11435 retirement.
  Restored branch copy + re-embed (06ed1cf). Gate 563/542/29/68 OK.
- 06:20 N3 merged (1030866) after round-2 fix (dmr helper import from nodes/windows). Haiku APPROVE.
- 06:22 Codex final review t-d6f4be32 (terra, high) over c787f67..1030866. Owner checklist "Start here" written.
- 06:21 Main gate after N3 merge: 573/542/29/68 OK.
- 06:24 Codex final t-d6f4be32: REQUEST_CHANGES. HIGH: Windows onboard still binds Ollama on tailnet IP + 11435
  listener + Defender opens 11434/11435. MED: subnet RemoteIP passes posture; gateway reports rolled_back when
  rollback fails. -> Sonnet fix t-f7e33082.
- 06:38 Fix t-f7e33082 (Sonnet): Windows loopback-only Ollama, 11435 listener retired, Defender 11436 only; subnet
  RemoteIP warns; rollback_failed status. One gate round (test fixture bug). Gate passed.
- 06:41 Codex security delta review t-6e01801a (terra high; re_review kind is Codex-refused by budget): APPROVE, no findings.
  Merged ad8318d.
