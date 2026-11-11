# 0.22.5 runtime candidate

This opt-in branch starts at upstream v0.22.5 (`bf0203a794a0`); it does not promote or replace the fork's default branch, publish a release, or update installed runtimes. Build the checked-out commit, not a moving branch name. `.fork/branches` supplies the packaging base version; existing maintenance refs are unchanged.

The named-profile stop fix from #141203 is omitted: upstream #141903 absorbed it, with subsequent lifecycle follow-ups. The candidate retains remote lifecycle reads, SSH script cron, per-profile desktop displays and skill copies, workbench loopback previews, pinned cron routes, SSH ControlMaster path budgeting, background-process tree termination, lease-fenced session repair, profile initialization status, remote browser/CDP ownership, CUA display propagation, native WebSocket readiness, QQ full-group self mentions, and prebuilt image installer compatibility.

Final-response remote MEDIA delivery is included from fork PR #1. Explicit delivery session keys select/reconnect the remote environment; both streaming and non-streaming paths keep profile scope, perform blocking fetches off-loop, and report rejected or failed attachments. The adapter response extraction lives in a sibling to respect the upstream file-size ratchet.

Compatibility resolutions preserve upstream's background spawn cwd and failure exception, its sandbox temp-root helper, explicit store-open error reporting, and additive database repair. The QQ regression imports follow the bundled adapter's new `plugins.platforms.qqbot.adapter` location. No compatibility shims restore deleted internal module paths.

The config schema remains 51. Database schema moves from 31 to 32; upstream describes v32 as declarative table/column reconciliation with no additional version-gated data backfill. This does not establish arbitrary downgrade safety. Keep a consistent backup of each profile's state and configuration before changing installed programs; binary rollback alone is not a database rollback. A synthetic v31 store retained its conversation and unchanged SSH/config/identity files after v32 open and append, and the previous runtime reopened that fixture successfully. Real channel pairing, large histories, plugin-owned state and live upgrade/reset/recovery still require integration acceptance.

Run `python scripts/check --base v0.22.5` and the relevant `scripts/run_tests.sh` suites. The candidate has no claim of successful remote CI, packaged-runtime validation or live channel delivery until those checks are recorded separately.
