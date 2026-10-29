# LifePlus project context

Read `docs/PROJECT_MEMORY.md` before explaining or changing architecture. It records the
2026-10-29 global-goal / domain-execution split and links to the source contracts.

- The global model outputs `GoalPlan`: task query, domain, dependencies and bindings.
  Tool selection and domain slots belong to the domain `understand` node.
- The internal `TaskPlan` still accepts historical action/slots for compatibility;
  do not describe these as fields produced by the current global planner.
- `depends_on` controls readiness; `bind_slots` carries actual predecessor evidence
  into host-owned `bound_slots`, merged after domain understanding.
- The user approved single-goal Jev routing on 2026-10-29. Local settings and
  `.env.example` use `DOMAIN_ROUTE_PROVIDER=jev_cascade`, with the original LLM
  fallback. Bare Settings retains llm for backwards compatibility. Keep
  `INPUT_EVENT_PROVIDER=llm` and `PLANNING_GATE_PROVIDER=rules`; other Jev paths
  remain experiments. Real-provider experiments are authorized; keys stay local.
- The optional front-gate experiment uses `PLANNING_GATE_PROVIDER=jev_front`
  (cardinality and single-goal domain in one Jev request) and event proposal
  auditing `INPUT_EVENT_PROVIDER=jev_gate`. Keep the original defaults, including
  `PLANNING_GATE_PROVIDER=rules`. A clear label alone cannot construct an event.
- Mock-transport tests verify contracts, fallback and budgets, not Jev accuracy,
  production performance or cost savings. Preserve unknown provider token usage.
- Prefer provider `total_tokens` for metering. Some providers report reasoning
  separately from visible output; never omit it or count it twice.
- Keep API keys in local environment settings. The `.env.example` contains placeholders.
- Retain unrelated working-tree changes and historical evaluation artifacts.
