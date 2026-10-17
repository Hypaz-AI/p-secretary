# Implementation status · scope, not whole-plan completion

The [architecture plan](SECRETARY_ARCHITECTURE_PLAN.md) remains a roadmap.
The current implementation is an **integrated, bounded review secretary**, not
an arbitrary workflow executor or a declaration that the whole plan is complete.

## Integrated startup and runtime

- [x] `db.init()` captures legacy provenance before base migration and applies
	native/jobs/task/media/owner, capabilities, policy, secretary, knowledge,
	runtime, note-provenance, execution and AI-budget migrations in one locked
	transaction. No consent is inferred or granted by migration.
- [x] `main()` calls `db.init()` before flows and starts eight durable workers:
	`analysis`, `media`, `reconcile`, `action`, `owner`, `knowledge`,
	`secretary_review`, `secretary_email_execution`. Each processes one job at a
	time; periodic launchers allow at most one live thread per named activity.
- [x] Native storage/revisions feed knowledge indexing and live observation;
	due reviews and runtime backlog scheduling are wired. Prepared review plans
	survive retries; versions, source revisions, native authority and leases fence
	application. Runtime actions are bounded review, local draft and owner decision
	proposals, not arbitrary tools, delegated sends or dependency execution.
- [x] Worker leases last 15 minutes, heartbeat every 30 seconds, execution deadline
	one hour; ordinary failures back off from 15 seconds to one hour, at most eight
	attempts. Policy-blocked media is terminal, not an automatic retry loop.

## Provenance, consent and bounded owner controls

- [x] New practice/responsibility owner plans use schema v2: server-resolved
	flattened native `input_sources` plus `input_versions` for the actual exported
	snapshot/history, persisted with immutable applied plans and bound receipts.
	Later reads include earlier applied owner inputs and runtime review inputs;
	missing, cyclic or unverifiable derivations fail closed. Legacy v1 plan replay
	is not proof of AI read-egress permission.
- [x] Shared owner-context/task-reconciliation source resolution follows task
	origins and **all applied** `task_changes.message_id` evidence, not merely
	`last_evidence_id`. Pending/rejected proposals do not contribute; missing or
	denied applied sources block that derived context. This is not complete
	transitive input provenance for legacy task/personal-memory model write plans.
- [x] Explicit per-channel email/Telegram/WhatsApp `ai_text`, `ai_media` and
	`ai_history` consents default to deny. Historical read-only context requires
	text + history, and media/document inputs additionally require media consent.
	`can_operate` still denies historical operations; history consent never replays
	tasks, commands, Calendar effects or historical media conversion.
- [x] `POST /api/media/retry-blocked` explicitly recovers eligible consent-blocked
	current media only, after both text/media grants; no automatic recovery on
	consent change. Historical sources and recorded provider attempts are excluded;
	recovery is bounded to 20 attachments/request and three resets/attachment.
- [x] Protected dashboard snapshots, local Italian lexical retrieval and source
	opening; exact-payload approval/revoke and restrictive responsibility stop with
	CAS, receipts and dependent approval invalidation. Dashboard reads never migrate.
- [x] `POST /api/secretary/runtime/links/<id>/accept` accepts the stored proposal
	version/evidence/scope, invalidates stale approvals and queues a review.
	`POST /api/secretary/runtime/practices/<id>/acknowledge` checks the displayed
	practice version + attention hash and suppresses unchanged review attention.
	Neither control grants a delegation or executes an external action.

## Effects and resource limits

- [x] Sealed text-only email request/snapshot, exact approval and separate explicit
	dashboard dispatch are integrated with the email worker. Approval alone never
	sends/enqueues; sender, fixed subject, payload, expiry, versions and receipt are
	rechecked. Attachments are rejected, never silently omitted; acceptance is not
	delivery/read confirmation.
- [x] Calendar v3 grants seal current native owner intent, event/state, explicit
	configured calendar ID, timezone and resolved create start/end/duration/reminder
	values. Expired grants and legacy grants with no target are denied; replay
	cannot renew or rebind them. Task deadlines remain local Calendar proposals,
	not authorization; the old sender-label auto-Calendar policy is inactive.
- [x] Receipted pipeline sends and email dispatch commit an attempt before I/O.
	Attempted/uncertain non-idempotent sends require reconciliation, never blind
	resend. Deterministic Calendar IDs support bounded retry/reconciliation, not
	arbitrary remote drift repair or exactly-once delivery.
- [x] Persistent Worker-context AI caps: 200 calls/day, 500000 reserved/charged
	tokens/day (UTC), two in flight and 65536 maximum output tokens/request by
	default. Retries consume reservations without refunds; DB/ledger/lease loss
	fails closed. Direct/local legacy calls, including periodic digest generation
	outside a job context, lack persistent budget enforcement. These are not a
	global monetary cap or a claim that every legacy AI path is secured.
- [x] Review-only quiet hours, optional invoice/ZIP flows, durable tasks/reminders,
	bounded connector recovery and defensive document extraction remain available.
	Runtime ingress uses `pipeline.ingest`; spam/newsletter mark-as-read survives
	only in the private legacy synchronous classifier, not the active ingest path.

## Remaining roadmap / unsupported guarantees

- [ ] Arbitrary workflow and dependency execution beyond bounded review/proposals.
- [ ] Full typed-memory conflict handling and complete model-input provenance for
	legacy task/memory write plans; existing origin/CAS/applied-evidence checks are
	partial coverage, not the finished memory/provenance model.
- [ ] General owner linking of arbitrary existing evidence to a practice from
	retrieval; accepting a runtime-generated proposal is narrower.
- [ ] Executable general stop conditions; stored prose is not a rule engine.
- [ ] Calendar updates, availability planning and ongoing remote drift detection/
	reconciliation; sealed create/cancel is narrower.
- [ ] Email attachments, threaded replies/send-thread metadata and verified delivery.
- [ ] Global observe/active mode, global quiet hours and monetary spending cap.
- [ ] Coordinated privacy erase/export across DB, derived text, files and backups.
- [ ] Replace trusted dashboard SQL fallbacks with public policy/secretary APIs.
- [ ] Full restored-backup drill, production validation and semantic/quality
	evaluation; reproducible dependency locking and a hard PDF sandbox also remain.

## Validation status

This documentation-only update reads repository source, not user data, and changes
no executable code. No tests, terminal commands, production migrations, restarts
or external calls were performed for it. Current full-validation results are to
be added by the parent run; earlier targeted success counts are not current
validation. CI defines isolated offline discovery and a separate disposable
PostgreSQL job, not proof of a successful run or a restored production backup.
See [OPERATIONS.md](OPERATIONS.md) for isolation, recovery and deployment limits.