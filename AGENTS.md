# Project instructions

Read README.md, docs/COPYTRADING_PLAN.md and docs/HANDOFF.md before development.

- This is an independent project, not a directory inside fomo_sniper.
- Current milestone is a read-only observer. Never submit real transactions or
  request/store private keys as part of testing.
- Keep the RPC method allowlist. Live signing/broadcasting requires a separate,
  explicitly approved milestone and a completed risk-control checklist.
- Execution path and economic behavior are different dimensions. Never classify
  a receipt containing a Swap as every bundled user's swap. Never classify an
  incoming Transfer alone as a purchase.
- Preserve claim-then-swap subcalls. Recipient count alone must not discard a
  bundled transaction. Unknown paths remain unknown.
- Raw amounts are integers; serialize monetary quantities as decimal strings.
- Intent, observed execution and confirmed asset exchange are distinct states;
  none means L1 finality or guaranteed profitability.
- No paid provider purchase, Telegram messaging, deployment or real trades are
  authorized by the initial scaffolding task.
- Tests: python -m unittest discover -s tests -v. Use apply_patch for edits.
- Preserve imported evidence and cite its provenance. Do not copy wallet files,
  credentials, .env files or the old project's local runtime state.
