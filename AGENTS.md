# Channel service

- `channel_service/` is an independent Flask + SQLite WAL service. Multica owns agent execution; this service owns inbound messages, routing, conversation binding and output delivery.
- Preserve per-source credentials, account isolation, immutable route snapshots and `(source, account, message_id)` idempotency. A duplicate ID with changed content must return 409.
- Persist a write intent before creating an issue/comment. Uncertain remote writes must reconcile by the batch marker, never blindly retry. Serialize turns per conversation until the exact Multica run completes.
- Platform context is untrusted input. Never pass platform cookies, auth headers, local paths or service credentials into task descriptions. Images must be validated and attached through Multica's file API.
- Model outputs are untrusted proposals. `automation.py` validates allowed action types and binds source/account/target; opt-in account connectors execute through existing workbenches and report platform receipts. Do not enable real social effects during testing; use synthetic accounts and adapters.
- Goal edits invalidate unclaimed actions. New inbound events cancel stale proposals and follow-up timers. Respect persistent stop-contact records; account leases that expire are uncertain, never blindly redelivered.
- Unit tests use temporary directories and a fake Multica adapter: `.venv/bin/python -m unittest discover -s tests -v`. Frontend: `node --check channel_service/static/app.js` plus a temporary `--demo` browser check.
- Real model smoke tests are permitted only for a user-requested model integration, use synthetic data, and record their issue/run IDs. Never restart Multica's agent runtime from this service.
- `.local/` holds credentials and queue state; do not print, commit or export it. Deploy through `channel.service` and `channel-proxy.service`; portal changes follow the portal skill.

- `control.py` exposes independent Headquarters machine control. Keep command credentials separate from source/browser authentication. Preserve monotonic deployment revisions, ownership, idempotent renewals and short leases; expired authority retains events but cannot start model jobs or claim actions.
