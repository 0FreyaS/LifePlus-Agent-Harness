# Contributing

Use Python 3.12 and Node.js 22. Install the locked Python dependencies with `uv sync --frozen --extra dev --extra music`; use `npm ci` in `frontend`.

Run `uv run --frozen --extra dev --extra music pytest -q`, `npm test` and `npm run build` before proposing changes. Unit tests must not silently call paid models or providers. Live checks are explicit and need private credentials; keep raw outputs under ignored `data/` or `evaluations/runs/`.

When changing orchestration, cover independent branches, dependency ordering, interruption/resumption, partial failure, cancellation and idempotency. A completed graph alone is not proof that a user goal succeeded: verify the tool receipt and selected parameters.

Document which results are measured, which use controlled test doubles, and which remain unverified. Do not add screenshots generated from invented provider results. Review ownership, licensing and provider terms before distributing derived content.
