# Ready POV blocked by unrelated acquisition — fixed 2026-10-28

The listener's synchronous `create_backlog` pass ran before its render-queue
drain. On October 28, a queued ZywOo Inferno and Anubis waited while acquisition
of Vitality–NAVI (2399747) ran corpus utility/input-overlay extraction through
`tmp/_ingest_worker.py`. The ingest worker was actively using CPU; elapsed time
alone did not establish a hang.

The stale-day explanation was incorrect. `_daily()` already ran at the start
of `poll_once`, rolling the day in memory; the file still showed October 27
because the daemon had not saved it before entering the long subprocess.

Fix: save the day rollover and queue order at cycle start, then drain ready
HLTV cards with available slots before any network discovery or acquisition.
The existing drain implementation is shared by this fast path and the normal
post-discovery path. FACEIT-only queues still undergo the live HLTV-window
check, and pipeline lock, daily cap, hard-failure and upload bookkeeping stay
in the same drain. Acquisition continues on a later poll when ready jobs no
longer have render priority. This makes ingestion optional background progress
relative to the selected local jobs rather than a prerequisite for rendering.

Regression `tests/test_listener_ready_queue.py` invokes the real `poll_once`
with yesterday's full ledger, a ready local card, and a discovery function that
fails if reached. It verifies render starts first and reads today's persisted
empty ledger. Additional cases exercise HLTV precedence, pipeline lock and
daily-cap guards. Together with listener/trend tests: 78 passed; Ruff passed.

Recovery: restarted only the known listener controller and wrapper, preserving
the saved queue and leaving the ingest subprocess untouched. New daemon PID
9744 launched Inferno pipeline PID 21128, then render worker PID 29640.
The saved pipeline advanced to step 2; CSDM launched CS2 PID 30748 on the
Inferno demo and logged `Recording in progress...`. Anubis remained second.
The saved daily ledger now reads October 28, zero completed. The new controller
uses the documented three-attempt hook cap. PIDs describe this observation only.
