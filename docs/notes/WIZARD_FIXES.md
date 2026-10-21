# Wizard fixes and offline verification

## Telegram personal account

Inspected the installed Telethon **1.45.0** source in the dependency environment
`/home/ale/OLD_pi-secretary/.venv`: `client/auth.py`, `_start` and
`send_code_request`. `start()` already calls `send_code_request`, discards its
delivery metadata, and retries expired codes as invalid codes. `force_sms` is
deprecated and ignored. Existing sessions bypass code requests; with the old
callable phone argument even the library's best-effort phone mismatch warning
was bypassed. None of these facts proves why a particular code did not arrive.

The setup now connects explicitly, reuses authorized personal sessions, refuses
a saved-phone mismatch without deleting the session, confirms/edits the phone,
reports returned delivery metadata without hashes or email patterns, supports
bounded invalid/expired-code retries, explicit resend after the returned timeout,
cancellation and 2FA. Flood waits are surfaced without automatic sleeping or
repeated requests. The shared owner lock and private session permissions remain.

## WhatsApp

Read-only inspection found `SingletonLock -> f627a00a2c41-504` in the launcher's
data directory. The owner lock belongs to the current user with mode 0600.
No PID 504 or running Docker containers were listed during inspection. A PID
missing from this namespace does **not** establish that a foreign owner is dead.

After a bridge failure, explicit `RESET` consent may clear only `wa_session`
contents, preserving `.owner.lock` and its inode, then retry once for a fresh QR.
Media, pending messages and all other data remain untouched. Reset is refused
for an active PID, visible profile process, unverified ownership, symlinked root
or profile, or inadequate process visibility. A foreign Docker owner can be
verified only by host-side inspection of a stopped matching container and the
absence of any running container sharing its data mount. An unavailable Docker
daemon, removed owner container or container-only PID visibility fails closed.
Consequently the old-host case may require host-side operator verification;
the Docker wizard does not silently delete foreign singleton locks. Revoke the
old linked device on the phone independently; deleting local files cannot do so.

## Calendar / Microsoft / database

- Calendar accepts hidden compact/multiline JSON strings only, terminated by
  `FINE`; no wizard file import. Existing valid keys can be reused. Replacement
  keys use lock + atomic 0600 write under `DATA_DIR`, persisted by the Docker
  bind mount, and the saved credential path is updated only after writing.
- Microsoft OAuth runs immediately after email configuration when Microsoft
  providers exist. The separate process loads fresh account settings; the
  standalone script, per-account labels and valid-token reuse are retained.
  Existing non-edited accounts no longer repeat provider-specific guides.
- After successful wizard completion, the launcher starts only the selected
  local DB, automatically probes the selected DB with SELECT 1, and still
  requires explicit consent before launching the app. External selection never
  starts the local DB. Section-specific configure modes remain available.

## Verification

Offline suites: `test_wizard_fixes`, `test_installer`, `test_setup_gcal`,
`test_setup_ui`, `test_setup_safety`, `test_telegram_account`,
`test_onboarding_regression`, `test_config_portability`,
`test_whatsapp_ingestion`, `test_gcal`, `test_agent_identity`: **119 passed**.

An expanded run including `test_durable_pipeline` ran 135 tests with one failure:
`test_calendar_identity_stable_and_conflict_recovered`; it also fails in isolation
in the unchanged runtime Calendar event/conflict path. It is not reported as a
passing test. No live provider authentication, real-session reset, Docker build,
restart or app launch was performed.