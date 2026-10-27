# Security

Do not commit `.env`, provider keys, database dumps, raw traces, private conversations, or server certificates. The release exporter checks known local credentials, but is not a substitute for reviewing the complete Git history and enabling GitHub secret scanning.

Use `APP_MODE=showcase` only for a password-protected, single-owner interview instance. All visitors sharing that account can see the same conversations and memory. Give each interviewer a separate deployment and fresh volume if isolation is needed. This is not enterprise IAM or multi-tenant authorization.

Never expose local/demo identity mode to the Internet. The default container port binds to loopback; expose only an HTTPS reverse proxy. Do not publish container port 8000 directly or trust forwarded identity from an arbitrary proxy. The HTTPS overlay disables the explicit local HTTP exception and trusts only Caddy's configured private IP, not all forwarding peers. Keep databases on the private Compose network. `/healthz` only reports process liveness and does not validate external providers.

Limit provider budgets and concurrent requests before inviting visitors. Rotate the showcase password and revoke provider keys after a public demonstration. Keys previously sent through chat or public logs should be rotated at the provider; do not copy them into an issue or README.

To report a vulnerability before a public security contact is configured, contact the repository owner privately. Do not open a public issue containing credentials, private traces or exploitable personal data.
