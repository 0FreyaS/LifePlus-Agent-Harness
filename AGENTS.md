# LifePlus project context

Read `docs/PROJECT_MEMORY.md` before explaining or changing architecture. It records the
2026-10-29 global-goal / domain-execution split and links to the source contracts.

- The user subsequently approved `deepseek-flash` with `MODEL_THINKING=disabled`.
  Keep domain Jev enabled, event `llm_direct` and planning gate `rules`. Compare Jev against
  this DeepSeek baseline separately from older Grok evidence; do not enable event
  filters automatically. See `docs/deepseek-rollout.md`. Credentials stay private.
- The global model outputs `GoalPlan`: task query, domain, dependencies and bindings.
  Tool selection and domain slots belong to the domain `understand` node.
- The internal `TaskPlan` still accepts historical action/slots for compatibility;
  do not describe these as fields produced by the current global planner.
- `depends_on` controls readiness; `bind_slots` carries actual predecessor evidence
  into host-owned `bound_slots`, merged after domain understanding.
- The 2026-10-30 bad-case fixes add scalar/export-shape and source-domain checks,
  strict actual-value validation and expiry rejection at dependency binding.
  Event text stays llm_direct; clarify unique cancel versus quoted explanations.
  RAG synchronizes removed IDs on explicit open, refusing missing directories.
  No atomic multi-writer index switching or universal post-binding freshness is claimed.
  Current-source container tests use isolated modules and temporary indexes, not deployment.
- The user approved single-goal Jev routing on 2026-10-29. Local settings and
  `.env.example` use `DOMAIN_ROUTE_PROVIDER=jev_cascade`, with the original LLM
  fallback. Bare Settings retains llm for backwards compatibility. Keep
  `INPUT_EVENT_PROVIDER=llm_direct` (subsequently approved) and `PLANNING_GATE_PROVIDER=rules`; other Jev paths
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
- Maintain `测试回报/测试总文档.md` and its annotated `测试数据集/` folder as the
  DeepSeek-only test entry. `docs/jev-front-ab-2026-10-29.md` is now a redirect;
  its full historical content was archived without deleting frozen evidence.
  Rebuild offline with `python -m evaluations.test_report`. The 2026-10-30 real run
  measured slots 44/44, plans 23/30 and full completion 82/90 with default location
  disabled to match gold; the default-location compatibility audit was 70/90.
  Keep these conditions separate and never carry old model numbers over.
  The later binding-contract fix was tested with thinking disabled: original
  seven failures 0/7 -> 7/7; new targeted long tails 3/30 -> 30/30 in paired runs.
  Original D04 separately passed 30/30. These are planner-only observations,
  not a new E2E completion rate; see report section 0.1. Keep thinking comparisons
  withdrawn from the current report while preserving archived raw evidence.
  Archive original frozen observations under `evaluations/jev/`.
  Record accuracy denominators, P50/P95 and complete/unknown token usage separately.
  Do not label combined-front E2E results as domain-only E2E. Regeneration must
  preserve manual notes outside the generated markers and frozen case/results files.
- `INPUT_EVENT_PROVIDER=llm_direct` was approved after the text reply A/B;
  `jev_resume_filter` remains experimental. Structured cards bypass both policies.
  The filter may skip only pure old-task replies with a full host-owned binding;
  compound replies still require the original LLM. Keep their versioned datasets
  and result section separate from the earlier event-proposal audit experiment.
