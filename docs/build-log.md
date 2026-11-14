# Build log

How this solution was built and how it changed along the way: what was discussed, what was
decided, and why. Entries are in order. The design itself is in
[`designs/weather-alerts.md`](designs/weather-alerts.md); this file is the history behind it.

**Who did what.** Andrés (the author) made every decision recorded here. An AI coding agent
(Claude) wrote the plan drafts, the design doc and the code, and ran the reviews. Review agents
started with a fresh context and saw only the documents they were given. Commits made with the
agent carry a `Co-Authored-By: Claude` line.

---

## 1. Planning, before any code (12–13 November 2026)

The plan was written in a separate conversation between the author and the agent, before any code.
That conversation's own account was merged here.

**First draft (agent).** An API for alerts and a separate worker. Evaluation as one SQL statement
that cannot create duplicates. Delivery separated from detection, with retries, and WhatsApp
replaced by a log. A simple data model: one owner per field, one coordinate per field, forecasts
stored per field, event types as a fixed list in code.

**First review.** A fresh reviewer saw only the brief and the plan. Changes:
*   One message per alert, listing the dates, instead of one message per date (up to eight in a
    rainy week).
*   Three database-library details that would have failed at runtime were pinned.
*   Pending notifications that are no longer true are discarded instead of sent late.
*   Some peripheral items were cut.

The reviewer judged the scope at two to three times the expected effort. That concern stayed open.

**Two hints from Agustin**, an engineer on FieldData's tech team who ran the author's third
interview.
*   **An article on Python's GIL.** It did not change the architecture, which already did all
    waiting work with async code and the heavy matching inside the database. It made the
    reasoning explicit: a concurrency model with rules and a lint gate against blocking calls.
*   **EGM, a spec-driven development tool.** A design doc became a deliverable written before the
    code, reviewed by fresh agents, and checked against the code before delivery.

**The author's hand-drawn data model.** The author drew the entity-relationship model by hand to
understand the problem and compare it with the plan. He first discussed it with another AI
assistant (Gemini, in Antigravity) and brought that summary to the planning.

His model brought three things the plan lacked, all adopted:
*   users and fields are many-to-many, with a role;
*   an alert belongs to the user who created it, and only that user is notified;
*   "the user must have access to the field" is a rule the database enforces.

It lacked three things, which he accepted adding:
*   a location for forecasts;
*   a link from a notification to the forecast that triggered it;
*   a delivery state rich enough for retries.

He also settled four points:
*   **Notification to forecast.** A second binary relation, chosen over a ternary relation and over
    the agent's suggestion.
*   **Event types.** A table, not a fixed list.
*   **Message text.** Not stored.
*   **Deleting an alert.** Deletes its notifications, against keeping them as history.

**Where forecasts live, driven by the author's questions.**
*   He objected to forecasts stored per field, because weather comes from an outside service.
    That led to zones, meaning cells of the provider's grid.
*   He asked for real weather APIs to be checked instead of answering from memory. They all take
    a point and answer for a grid cell.
*   He asked about large fields. Farms range from tens to tens of thousands of hectares, so one
    field can span several cells.
*   He rejected the single-coordinate shortcut. That first led to a polygon outline and a zone
    resolver, later cut (section 5).
*   He proposed alerting only when a meaningful part of the field is affected. That became the
    minimum-affected-area rule.
*   He decided not to choose a weather provider, because the brief says to mock the data.

**Second review.** It covered the changed parts only and found defects, all fixed in the plan:
*   missing links between alerts, users and fields in the code layer;
*   API responses that would have broken once event types became a table;
*   coverages that could add up to slightly less than 1;
*   text left over from the old model.

**Decided by the author during planning:**

| Decision | Chosen | Chosen against |
|---|---|---|
| Background job | A small hand-written async worker, database only | A task queue with Redis |
| Authentication | A header that stands in for login, with real ownership checks | Real login; database row-level security (a next step) |
| Languages | README in Spanish; design doc, code and commits in English | One language for everything |
| Spec tool | Design review before code and drift report before delivery | Drift report only |
| Users per field | Several, with roles | One owner per field |
| Weather provider | None; mocked | Integrating a real one |
| Attribution | AI co-author line on commits; this log and the README say how it was built | Saying nothing about the process |

**What the agent proposed and the author had not examined.** At the end of planning, the author
said he had mainly worked through the data model himself. These were the agent's proposals, which
he had seen but not examined in depth:
*   the details of the notification rule;
*   the delivery and retry design;
*   the API shape;
*   the async rules;
*   several smaller technical defaults.

He went back over these in the review rounds below (sections 6 and 7), and changed the retry
design as a result.

## 2. Plan check at the start of implementation

The agent read the plan end to end and flagged inconsistencies before writing anything. The
author fixed them in the plan:

*   **Commits go to `main`, not through a pull request.** The repository itself is the
    deliverable. The same checks still run before delivery.
*   **Success criterion.** Two wordings of the success criterion were aligned.
*   **Cancelled sends.** A send cancelled mid-flight no longer counts as a failed delivery
    attempt. It is rolled back, as after a crash.
*   **Running the EGM review.** The EGM skills can only be started by a person, not by the
    agent. The author asked the agent to run them by following each skill's instructions with
    the plugin's own reviewer agents, which is what the plugin documents for this case.

## 3. Bootstrap

*   Project tooling (Python 3.13 with uv, ruff, mypy strict, pytest) and a local PostgreSQL
    database in Docker Compose, with a separate test database. Lint, type check and a
    placeholder test were green before the first commit.

## 4. Design doc and MER diagrams

*   The design doc (`docs/designs/weather-alerts.md`) was written in EGM's layout:
    *   overview, problem, goals and non-goals;
    *   critical user journeys;
    *   the technical plan;
    *   one rejected alternative per decision;
    *   the detailed implementation plan.
*   The hand-drawn MER was redrawn as two SVG diagrams in Chen notation with the Spanish names:
    a simple one and a complete one with every attribute. Both were rendered and checked
    visually before use.

## 5. EGM review, Stage 5 (initial zoom-out)

**Reviewer context.** The reviewers received the challenge brief, the recruiter's email, a short
factual summary of the Real Python GIL article, and the two diagrams. The article site blocks
automated downloads, so a summary was used instead of the page. The brief's file name broke the
reviewers' file reader, so both reviewers were run again on a renamed copy. Only the complete
runs count.

**Findings and decisions:**

| Finding | Decision | Why |
|---|---|---|
| Claims were per row and messages per alert, so an alert's dates could be split into several messages, which is the burst the design meant to avoid | **Fixed.** The dispatcher now claims whole alerts | A real defect against a stated goal |
| A message could still go out after the user raised the alert's threshold | **Fixed.** Such notifications are discarded | Cheap, and the farmer should not get stale warnings |
| The transaction stays open while messages are sent | **Kept**, with one added sentence: exactly-once delivery needs the provider to deduplicate | Already a considered trade-off (bounded to one batch) |
| Implementation details in the architecture sections | **Fixed.** Moved to the detailed plan | Keeps the overview readable |
| Existing files listed as "create" | **Fixed** | Accuracy |
| `forecast_id` stored but not exposed | **Fixed.** Exposed in the API | It is a relation in the author's MER |
| Membership role drives no rule; redundant foreign keys | **Kept**, with the reasons written in the doc | The role mirrors Agrobot's model; the extra keys give the code its join paths |
| Simplify retries (no jitter); fewer settings | **Rejected** | Both answer the grading criteria (production readiness) |
| Skip forecast dates that already fired before evaluating; soft-delete alerts to keep history | **Rejected** | An optimisation not needed yet; keeping history contradicts an earlier decision |
| One digest per user instead of one message per alert | **Recorded** as a future improvement | Reasonable, not needed now |

**The main change: the zone resolver was cut.** The first design computed, inside this system,
which forecast squares each field touches:

*   a background job;
*   the field's outline;
*   sample points;
*   one weather-service call per point.

Both reviewers objected, for three reasons:

*   the brief says the weather data is already ingested, so this is not asked for;
*   one call per point would not work against a real weather service;
*   it did not fit the 4–5 hour budget.

The author agreed with cutting it but kept the calculation and its granularity:

*   Fields still link to zones with a coverage percentage.
*   The "at least 10% of the field" rule stays.
*   The design now **assumes Agrobot already has that field-to-zone mapping**, the same way it
    already has the forecasts. The demo data writes it.
*   The doc explains how the mapping would be computed if it were missing, using the reviewer's
    better method: learn the provider's grid and do the arithmetic locally, with at most one
    call per distinct square instead of one per point, and no database lock held while waiting
    on the provider.

**A plain-language summary was added** at the top of the design doc. It covers the data that
exists, the data the system creates, and how the two jobs use it. The author found this
explanation the clearest way to agree on the solution, so it will also open the README.

## 6. EGM Stage 5 re-audit, and the retry policy redesign

**Re-audit.** EGM allows one re-audit per stage after edits. Both reviewers ran again on the
revised doc.

*   **One zone per field.** The zoom-out reviewer still wanted to drop coverage and give each
    field exactly one zone (simpler, but wrong for large fields that span several squares). The
    author **kept coverage**: it models the real case, and he chose it deliberately. Recorded as
    an accepted trade-off.
*   **Time budget.** The reviewer also said the scope exceeds the brief's 4–5 hours. The author's
    view: those hours are the time to define the solution (about one hour of planning plus three to
    four hours of iterating and reviewing). Implementation by the agent comes after the plan is
    approved.
*   **Consistency fixes.** Small fixes applied without discussion: the message example now matches
    the worked example; "next 8 days" is spelled out as today through today + 7; alternatives are
    in order; there is a reason for not using APScheduler; leftover resolver text was removed.
*   **Other suggestions.** Already-decided points raised again (`forecast_id`, `role`, the
    concurrency rules) are kept as decided. "Check a new alert immediately in the API" is
    recorded as a future improvement.

**The author asked to review what he had not decided himself.** He noticed that the retry logic
was a large part of the reviewers' comments, yet he had never weighed in on it. The agent explained
it in plain terms. The reviewers had found four problems:

1.  Retries gave up after 5 tries, about 15 minutes in total, so a longer WhatsApp outage lost
    every pending alert for good.
2.  A day detected while its alert was waiting to retry triggered the retry early. It could also
    inherit the tries already used and be given up after a single attempt.
3.  A normal restart (a deploy) re-sent messages that had already gone out.
4.  A message delayed by an outage could still show old numbers.

**Decided by the author:**

*   Retry **for as long as the warning is still useful**: until the forecast date passes, with waits
    growing up to 30 minutes. A date that passes undelivered is marked failed and logged once. This
    replaces "5 tries".
*   A new date **joins its alert's retry schedule**, so it goes out in the same message.
*   On a normal stop the worker **finishes the batch it is sending**, then exits. Only a crash can
    still cause a duplicate.
*   Just before sending, every date is **re-checked against the current alert and forecasts** with
    the same rule evaluation uses. Dates that no longer qualify are dropped; the rest show current
    numbers.

**Other choices the author had not discussed** were explained in one line each and approved as
they are:

*   safety when several worker copies run at once (tested);
*   matching done in one database query;
*   the endpoints that list fields and event types (a real client needs them);
*   the production extras: Docker, CI, health checks, logs, the coverage bar;
*   tests against a real PostgreSQL.

## 7. EGM Stage 6 (comprehension test)

A fresh reader got the design doc alone and explained it back correctly, with no blockers. Four
things confused them. The author decided:

*   **"The brief" was never defined.** A section now names the source of the requirements and
    lists its key points.
*   **The diagram's Spanish names were not linked to the tables.** The author chose to redraw the
    conceptual model **in English**. He had used Spanish to match the domain, but the diagram is
    for the team and the readers, and English reads better next to the tables. The code never
    refers to it. The doc now has a diagram-to-table mapping.
*   **The notification had no forecast date in the diagram**, although "one notification per
    alert and date" depends on it. Added as `forecastDate`. The author asked whether it would be
    part of the key. Answer: the uniqueness is a unique key on (alert, forecast date) in the
    database, and the notification keeps its own identifier as primary key.
*   **Two sentences contradicted the new retry rules.** The doc now says that "notified" means
    *sent*, and that a notification's values are refreshed while it is pending and frozen once
    it is sent or failed.

Small wording fixes:
*   "a date has passed" is measured in the forecast timezone;
*   the overview says "waiting to be sent";
*   the threshold in a message is the same for every date;
*   two vague steps in the mapping section were made concrete;
*   an unexplained provenance comment was removed.

**Goals ranked.** The author approved splitting the goals into three groups:
*   **required:** what the brief asks literally;
*   **graded:** what the brief says it grades (production-ready code, data model, async);
*   **extra:** built because it makes the result more complete.

**Scope and time.** The author's view: the brief's 4–5 hours is the time to define the solution.
Implementation by the agent comes after approval, so nothing is cut for time.

**Stage 6 re-audit.** EGM allows one re-audit. The fresh reader explained the revised design
back correctly. It found one more real gap in the retry rule:
*   A date detected while another worker was sending its alert copied an already-due retry time,
    and could trigger an early retry.
*   The fix: when a send fails, the new retry time is written to every pending notification of
    the alert.

The other points were wording and were fixed:
*   synchronous log writes are stated as the one accepted blocking exception;
*   one word ("deleted") is used for removing a pending date;
*   the build log is linked;
*   the optional link from a notification to its forecast is stated.

## 8. EGM Stage 7 (critic review), round 1

Three fresh critics reviewed the doc (design, structure, writing). Every finding was accepted.

**Real defect.** With two worker copies, the same alert could be sent twice, and two failed sends
could deadlock each other. The dispatcher locked an alert's notifications but not the alert
itself. The author asked why this was not caught when the "claim whole alerts" fix was written.
The agent's answer: it had reasoned about one worker only. The fix: the dispatcher now locks the
**alert**. Each worker copy then gets different alerts, and a new date always joins the message
being sent.

**Fixes accepted:**

*   The doc no longer promises a message is sent only once. A notification is marked *sent* at
    most once, but a message can reach the user twice after a crash, or when WhatsApp delivered it
    but answered too late.
*   **Missing database checks** on a notification's probability, threshold and attempts. The
    column type alone allows values such as 150%. The author asked why the type was not enough.
*   A **Risks and limits** section. EGM's feature-spec standard asks for limits and mitigations;
    the table layout is the agent's choice.
*   Database time limits, so a stuck lock cannot block shutdown.
*   The provider's error text is never stored, because it can contain a phone number.
*   The API is reachable only from the machine it runs on, because the identity header is a stub.
*   A rare race returns 404 instead of an error page.
*   A **worker health check** (a heartbeat file that Docker watches). This is new scope; the
    author approved it.
*   Many wording and structure fixes.

**The author's other decisions in this round:**

*   **When a warning arrives.** As early as possible: the moment a forecast within the next 7
    days crosses the threshold (up to 7 days ahead, or on the same day if it crosses late). The
    7-day window, the 10% minimum, the event types and the roles stay as proposed.
*   **Alerting engineers.** When a warning cannot be delivered, the error log line is the signal.
    In production it would feed an error tracker or dashboard (Sentry, Grafana) that pages an
    engineer.
*   **Demo data that tells stories.** One field per case: one message for two dates; a field
    split 60/40 between zones; a 5% sliver that does not fire; two members of one field with
    different thresholds.
*   **A visual demo page.** The author asked for one. It is being designed as a mock first and
    iterated with him before it is built.
*   **Credit.** Agustin is named for the two hints.

## 9. EGM Stage 7 round 2, and the multi-worker audit

**The author's question.** After the double-send bug, the author asked whether anything else was
broken because the agent had been reasoning about a single worker. A fresh reviewer was given
only the design doc. It assumed three API copies, three worker copies, the ingestion job and users
editing alerts, all at the same moment. Together with the round's design critic, it found more:

*   **Sending could stop detection (a blocker).**
    *   The problem: while a send was in progress, evaluation's duplicate check waited on rows the
        sender had changed, timed out, and failed the whole run. This happened even with one
        worker, because its two jobs run side by side, and most during a WhatsApp outage.
    *   The fix: evaluation first skips any notification it can already see. That plain read never
        waits.
    *   This is the "optimisation" rejected in Stage 5 (section 6). It turned out to be needed for
        correctness.
*   **A second worker could resend right after a failure.**
    *   The fix: an alert is due only when all its pending dates are due, and this is re-checked
        when the alert is claimed.
*   **Editing or deleting an alert while it was being sent returned an error page.**
    *   The fix: answer 503 "try again in 10 seconds".
    *   The author chose this over making the request wait about 20 seconds, and over a different
        locking scheme.
*   **A worker that died without closing its connection could freeze its alerts for hours.**
    *   The fix: the database ends an idle transaction after 30 seconds.
*   **Deleting an alert during an evaluation aborted the whole run.**
    *   The fix: retry the run once.
    *   The author chose this over locking every alert on each run, and over evaluating in chunks.
*   **Two workers evaluating at once could make each other fail.**
    *   The fix: only one worker evaluates at a time, and the others skip their turn.
*   **The ingestion job's write pattern matters, and is now a stated assumption.**
*   **Smaller fixes:**
    *   one health heartbeat per job;
    *   every worker uses the database's clock, so they agree on "today";
    *   a faster claim query;
    *   two new risks: the connection budget, and WhatsApp's template rule outside the 24-hour
        window.

**Why several workers at all** (the author asked):
*   **Availability.** One worker can die or be redeployed while another keeps sending.
*   **Sending throughput.** Sending waits on WhatsApp, so a second worker helps during an outage.

Evaluation does not get faster with more workers, because it runs in the database. The demo runs
one worker; the design is safe with several.

**Also decided:**
*   The doc now matches the build log: the four story fields, and the demo page as an Extra goal.
*   "Rejected by author" was removed from alternatives the author never discussed. The build log
    records the decisions that were discussed.
*   **The demo page.**
    *   It went through two mock versions with the author: a map of the fields over the forecast
        zones with a date bar, a phone chat in a WhatsApp-like look (without WhatsApp's name or
        logo), and buttons to replay the delivery and to simulate an outage. It is in Spanish.
    *   The author wants richer visuals for each event type, in a minimal style, later.

## 10. EGM Stage 7 round 3

No blocker remained, and the structure critic passed the document. The author approved all fixes.
The ones that change behaviour:

*   **A health check that would cry wolf.**
    *   The problem: the sender reported "alive" only after working through its whole queue. During
        a long outage that takes minutes, so the worker would look unhealthy and could be
        restarted in a loop.
    *   The fix: it reports after every batch.
*   **A trap for the implementation.**
    *   The problem: the pre-send re-check reuses evaluation's matching logic. Copied whole, it
        would include evaluation's "skip what is already notified" filter. That filter would make
        every waiting date look invalid, so nothing would ever be sent.
    *   The fix: the doc now says exactly which part is shared.
*   **Paging an engineer for nothing.**
    *   The problem: if a user turned an alert off during an outage, its expired dates were logged
        as errors.
    *   The fix: they are now deleted quietly. Only warnings that were still wanted, and
        undelivered, are errors.
*   **One new alternative: evaluate only when something changes.**
    *   It was weighed and rejected for now. A full run is cheap (about a second for 10,000
        alerts) and cannot miss a change.
    *   Change-driven evaluation would couple this system to the ingestion job's internals, and it
        would still need a periodic run as a safety net.

**The demo page.** The author asked for visuals for each event type now rather than later. Version
3 of the mock adds a minimal line icon for frost, rain, extreme heat and strong wind. The icons
appear on the event buttons, on the map zones (stronger with higher probability), on the
fired-alert badges, on the alert chips and in the chat messages.

## 11. EGM Stage 7 round 4

No blocker remained, and the structure critic passed the document again. The design critic found
three ways the system could fail without anyone noticing. The author approved fixing all three.

*   **A field with no zone mapping never alerts.**
    *   The problem: if Agrobot has not yet linked a new field to its zones, an alert on it was
        accepted but could never fire.
    *   The author was offered two options: reject such alerts, or accept them and flag them. He
        chose to accept them. The alert response says `zones_mapped: false`, so the bot can tell
        the user, and each evaluation logs a warning. The alert starts working once the mapping
        arrives.
*   **Old forecasts look like calm weather.**
    *   The problem: if the ingestion job stops, nothing noticed.
    *   The fix: a warning when the newest forecast is older than 24 hours. This adds a new setting.
*   **New warnings queued behind an outage's retries.**
    *   The fix: first attempts are now claimed before retries.
    *   The doc now states the estimated limit honestly: about 1,800 alerts with waiting warnings
        per worker.

**A factual correction.** The doc said three weather APIs had been "checked" and that all describe
a zone only by its centre. Not quite: the US service returns the zone's outline. The doc now says
the design is "modelled on" them, and states what each returns.

**Smaller fixes:**
*   The health check that may restart the worker watches only the sender. A failing evaluation
    therefore alerts an engineer instead of restarting the sender in a loop.
*   A cap on the retry backoff exponent.
*   The sender rereads the clock every batch.
*   Wording.

**The demo page.** The author approved the visuals in version 3 of the mock, so the doc has no open
questions left.

## 12. EGM Stage 7 round 5, and closing Stage 7

Round 5 was the last round EGM's review loop allows. It had no blocker and found four real but
small issues. The author chose to fix them and close the stage.

*   **A missing index.** Without an index on the notification's link to its forecast, deleting old
    forecasts gets slower as notifications accumulate. The index was added.
*   **An over-promise about duplicates.** The doc said the notification ids could serve as a
    provider idempotency key. But a retried message can carry a different set of dates, so the
    claim was narrowed. "Freeze a message's dates after a timeout" became a future improvement.
*   **A stale-data check that only caught a total stop.** It now checks each zone that has alerts,
    so a job that silently skips some zones is noticed.
*   **A migration that treated Agrobot's tables as this feature's.**
    *   The one migration became two: one for the stand-ins of Agrobot's tables (demo and tests
        only), and one for this feature's three tables.
    *   A real deployment would apply only the second, and rolling it back removes only what the
        feature owns.
*   **Bookkeeping.**
    *   The backlog was completed.
    *   The demo mock is now in the repository (`docs/designs/demo-mock.html`) so the doc can link
        to it.
    *   The heartbeat is now written on idle runs too.

**What Stage 7 achieved.** Five rounds of three fresh critics each, plus one concurrency audit the
author asked for, found and fixed:
*   a double-send race;
*   a blocker where sending could stop detection;
*   a dead-worker lock that could last hours;
*   several silent failure modes;
*   a missing index;
*   a migration boundary.

The design held from round 2 onward. Later rounds found smaller and smaller issues.

## 13. EGM Stage 8 (intermediate zoom-out), and closing the review

A fresh reviewer read the whole doc again, focused on what changed since Stage 5. **No blocker:**
the architecture was judged ready to build. Its main point: many of the fixes added in the review
rounds patch three earlier choices. It proposed changing those choices instead. The agent explained
each one in plain terms (in Spanish, at the author's request) before he decided.

| Finding | Decision | Why |
|---|---|---|
| Sending while the batch's transaction is open causes most of the later fixes (503 on a busy alert, idle timeouts). Use a "lease" instead: mark the alert reserved until a time, commit, send, record. | **Kept as is.** The costs are now listed honestly in Alternative T | If a worker dies, the database releases its alerts by itself: no cleanup code, no lease length to tune (too short sends twice, too long delays retries). The cost is bounded to 10 seconds per batch |
| The retry schedule belongs to the alert but is stored on every notification, kept in step by three rules. Store it once per alert. | **Kept, as an accepted minor trade-off** (new Alternative W, future improvement) | At most eight dates per alert; each rule has a test; changing it would redo reviewed design. A contained change if wanted later |
| The notification's link to its forecast is a foreign key to a table Agrobot owns, which constrains how Agrobot deletes. Make it a plain number, or drop it. | **Kept**, with its costs written next to it | The author asked whether the app would crash if Agrobot deleted things. No: deleting a forecast only clears the link (the notification keeps its date, probability and threshold), and the database refuses to drop the whole table while links exist. It is the "triggered by" relation of the author's model |
| The demo page computed the alert rule and the message text again in JavaScript, so it could disagree with the server | **Fixed.** The outlook endpoint returns which alerts fire on which dates, computed by the server's own matching rule; the chat shows the real notifications; the simulated outage is labelled as an illustration | The author: the demo is just for fun, so it should not carry its own copy of the logic |
| The two-migration split was described in a way Alembic's linear chain does not allow | **Fixed wording**: in Agrobot the feature's migration would be attached to Agrobot's own migrations | Accuracy |
| Detail crept back into the overview sections | **Fixed**: numbers, cancellation rules, the idempotency-key reasoning and log policy moved to the details | Keeps the overview readable |
| Duplicate guards spelled out as several rules | **Fixed**: stated once ("advisory lock for efficiency; unique key for correctness"), the rest as implementation notes | Clarity |
| Drop the evaluation heartbeat and the coverage-sum warning | **Kept** | Cheap, approved in round 4 |

This closes the EGM review of the design doc ("Feature spec only" preset: Stages 5 to 8). The doc
is marked Approved and committed before any application code.

## 14. Implementation

The design was closed and committed; from here the agent builds the plan's steps in order, one
commit per step, and records here every choice the design doc left open.

### Step 1: configuration, logging, database plumbing, health checks

**TL;DR:** settings, logs, database connections and the two health checks exist. Nothing in the
design changed.

*   **Configuration.** One settings object reads the environment (and a local `.env` file, for
    convenience) and refuses bad values at startup: numbers must be positive, the timezone must
    exist, the minimum affected area must be above 0 and at most 1, and the database address must
    be a PostgreSQL address for the asynchronous driver and name a database. Interval and timeout
    settings accept fractions of a second, so tests can run the worker quickly.
*   **`.env.example` rewritten.** It still listed settings from the cut zone-resolver design and
    the dropped attempt limit; it now matches the design doc's configuration table exactly.
*   **Connection timeouts.** Every connection sets the 5-second lock timeout and 30-second
    statement timeout. Worker connections add the idle-in-transaction timeout and TCP keepalives.
    The database driver has no keepalive options of its own, so the keepalives are set as
    PostgreSQL session settings, which have the same effect. A test reads them back.
*   **The API is built by a factory** (`create_app`), started with `uvicorn --factory`, so tests
    build their own app against the test database. The engine is created by the factory, not at
    startup, because the test client does not run startup hooks.
*   **Readiness** answers 503 for any database failure and logs one warning; it does not raise,
    because "not ready" is an expected answer of a probe.
*   **Logs.** Our handler is the only one replaced when logging is set up again, so the test
    runner's log capture keeps working.

### Step 2: models and migrations

**TL;DR:** the nine tables exist, with every constraint the design lists, in two migrations that
go up, down and up again. One detail the doc left open was settled.

*   **A dependency between the two migrations.** The forecasts table (an Agrobot stand-in, first
    migration) points to the event types table (this feature's, second migration). A table cannot
    point to one that does not exist yet, so the first migration creates the forecasts' event type
    column without the link, and the second migration adds the link after creating the event
    types. Rolling the feature back removes that link and the feature's three tables, and nothing
    else. Rejected: moving event types into the first migration (the doc says the feature owns
    them) and moving forecasts into the second (the doc says they are Agrobot's).
*   **Checked against the models.** `alembic check` confirms the hand-written migrations match the
    models exactly; a test checks that rolling back the feature leaves only Agrobot's six tables,
    on a throwaway database so it never touches the test data.
*   **Tests use a real PostgreSQL**, with the schema created by the migrations once per run and the
    tables emptied after every test (and once at the start, in case a previous run was cut short).
    Every constraint has a test that breaks it on purpose.

### Step 3: demo data (seed)

**TL;DR:** `python -m app.seed` writes the four field stories, idempotently. Two small choices.

*   **The walkthrough's own pair is kept calm too.** The README walkthrough creates a frost alert
    on La Esperanza. If that zone's background frost values were fully random, the walkthrough
    could produce more dates than the two pinned ones, and the "two dates, one message" story
    would break. So the zone and event type the walkthrough will watch get the same ceiling as
    the ones the seeded alerts watch (below 50).
*   **Forecasts are written in one batch**, with the upsert the ingestion job is assumed to use
    (it changes only the probability and its timestamp); pinned values replace the background
    ones before writing, which has the same result as writing them afterwards.
*   **The story check** (evaluation creates exactly the notifications the stories predict) needs
    the evaluation service, so it is added with step 5.

### Step 4: alerts, fields and event types API

**TL;DR:** the alert endpoints, the fields and event types listings, and the error answers work
as the design's API table says. A few small choices on how.

*   **Strict input types.** The threshold must be a JSON number, not the text `"70"`; the active
    flag must be `true` or `false`. Loose conversion would accept input the API does not promise.
*   **Unexpected errors are logged once.** The web framework's own catch-all answers 500 but
    passes the error on, so the server logs it a second time. A small wrapper around the app logs
    it once, at ERROR, and answers 500 with no internals.
*   **A busy alert answers 503.** Changing or deleting an alert waits for its lock; when the
    5-second lock timeout expires (a dispatcher is sending it), the database's "lock not
    available" error becomes 503 with `Retry-After: 10`. A test holds the lock from another
    connection and checks the answer arrives after about five seconds.
*   **Membership removed mid-request.** The database rejects the insert through the composite
    foreign key, and the API answers 404, as when the membership was missing from the start.
*   **`Location`** is the path of the new alert (`/api/v1/alerts/{id}`).
*   **Coverage measurement** is told about the database library's greenlets; without that it
    reported lines that run as not covered.

### Step 5: evaluation service

**TL;DR:** the evaluation statement is built exactly as the design's SQL, and the matching rule
lives in one function that the dispatcher's re-check and the demo outlook will reuse. One bug
from step 2 was found and fixed on the way.

*   **One matching rule, three users.** `firing_matches` builds the join of alerts, field zones
    and forecasts, the threshold, the window, the area sum and the "highest probability" pick.
    Evaluation adds its "not notified yet" filter and the insert; the re-check and the outlook
    will add their own filters. So the three can never disagree about when an alert fires.
*   **Data checks** (`alerts_without_zones`, `forecasts_stale`) are written here and called by the
    worker's evaluation job in step 8. Missing forecasts are checked per zone and event type an
    active alert watches, so a zone missing only its rain forecasts is noticed too.
*   **The database clock.** A small helper reads `now()` from the database; jobs derive "today"
    from it in the forecast timezone, as the design says.
*   **Bug found: doubled constraint names.** The migrations created check constraints as
    `ck_zones_ck_zones_latitude_range`: Alembic applies the models' naming convention to names
    that were already complete. The constraints worked (the tests that break them passed), so
    only the names were wrong. Fixed by marking the names as final, and a new test compares every
    constraint and index name in the database with what the models expect. Its own commit.
*   **Story check** from step 3 added: after seeding, evaluation creates exactly the four
    notifications the stories predict, and the README walkthrough's alert adds exactly the two
    pinned dates.
