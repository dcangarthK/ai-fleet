CHANGES

HIGH

- `model="auto"` can rank a model as eligible using rules that differ from budget admission: ranking matches the candidate key, while auto-pick later assigns Codex `gpt-5.6-terra`. A model-specific Codex rule, or an exact-vs-glob precedence case, can therefore be selected then refused by `budget.admit`. [intel.py](master/controller/intel.py:62) [tasks.py](master/controller/tasks.py:1506) [tasks.py](master/controller/tasks.py:1697)

- Codex is not hard-blocked for `fix` or `review`; both are absent from `CODEX_NEVER`. A permissive budget-rule edit can let auto-pick select Codex for either, violating the stated “only final/security/build/lead” constraint. [intel.py](master/controller/intel.py:33) [intel.py](master/controller/intel.py:94)

- Cleanup can remove dirty or newly-unmerged worktrees. The merged check neither verifies a pristine worktree nor rechecks immediately before removal; runner cleanup commits dirty changes and force-removes the worktree. The existing task-cleanup endpoint also bypasses merged-branch validation entirely. [intel_routes.py](master/controller/intel_routes.py:173) [tasks.py](master/controller/tasks.py:2280) [runner.py](nodes/runner.py:3060)

The explicitly described H1/H2/H3 fixes are present: normalized keys, real 5-hour usage, and empty/no-commit repo rejection. I did not run the gate, as requested.
