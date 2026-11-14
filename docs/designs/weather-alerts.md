# Weather Alerts for Fields

*   **Author:** Andrés Siciliani
*   **Status:** Approved
*   **Date:** 2026-11-14

--------------------------------------------------------------------------------

## Overview

Agrobot is a farm-management assistant that farmers use over WhatsApp. It is
built with Python, FastAPI and PostgreSQL. An existing ingestion job already
stores weather forecasts: for each future date and each weather event (frost,
rain, …), a probability.

This design adds **weather alerts**. A user picks one of their fields, an event
type and a probability threshold ("tell me when frost reaches 70%"). A
background worker periodically compares the stored forecasts with every active
alert, records a notification whenever an alert is met, and delivers it to the
user. The real WhatsApp integration is out of scope: delivery goes through a
notifier port whose only implementation writes the message to the log.

The system is two processes built from one codebase and one container image:
an HTTP API where users manage their alerts and read their notifications, and a
worker that runs two periodic jobs. PostgreSQL is the only infrastructure.

### How it works, in short

**Data that already exists** (in Agrobot; mocked here by a seed script):

*   **Users and fields.** A field can have several users, each with a role.
*   **Zones.** A zone is one cell of the weather provider's forecast grid. The
    demo data uses a fake grid of 0.25-degree cells, about 25 km wide.
*   **Which zones each field touches, and how much.** For example, "La
    Esperanza" is 60% in zone A and 40% in zone B. Agrobot is assumed to
    provide this mapping (see [Assumption 3](#assumptions)).
*   **Forecasts.** One probability per zone, event type and date, written by
    the existing ingestion job.

**Data this system creates:**

*   **Alerts.** A user says "frost at 70% or more on La Esperanza" through the
    API.
*   **Notifications.** Created by the worker; each one records what was
    detected and whether it was delivered.

**How it runs** (two background jobs in the worker):

1.  **Evaluation** (every 5 minutes). For every active alert and each of the
    dates from today through today + 7: which zones of the field have a
    forecast at or above the threshold, and do they add up to at least 10% of
    the field? If so, it records a notification for that date; there is at
    most one notification per alert and date at any time.
2.  **Dispatch** (every 15 seconds). For each alert whose waiting
    notifications are all due (none is waiting for a later retry), it sends
    **one** message listing all its dates (for now written to the log instead
    of WhatsApp). Just before sending, it re-checks each date against the
    latest forecasts and drops the dates that no longer qualify. If sending
    fails, it tries again later, waiting longer each time (see
    [Delivery (overview)](#delivery-overview)), until each date passes; a date
    that passes without being delivered is marked failed.

The user lists their notifications through the API to see what was sent.

## The Problem

A farmer needs to know, a few days ahead, when an event that threatens a crop
becomes likely on one of their fields, so they can act (cover plants, delay a
spraying, move machinery). Today the forecasts exist in the database but nobody
is told about them.

Four facts shape the solution:

1.  **Forecasts are not per field.** Weather providers answer for the zone that
    contains the point asked about. In the demo data's 0.25-degree grid, a zone
    is about 25 km wide. Neighbouring fields share a zone, and a large ranch
    can span several zones.
2.  **Fields are shared.** A field can have several people (owner, agronomist,
    manager), and each of them may want different alerts on it.
3.  **The channel is WhatsApp.** Each extra message costs the farmer
    attention, so near-identical messages for one alert are merged.
4.  **Delivery fails sometimes.** A messaging outage must not lose alerts or
    stop new ones from being detected, and a crashed worker must not send the
    same alert twice in a loop.

[How it works, in short](#how-it-works-in-short) summarises how the design
responds to them.

## Source of the requirements

The requirements come from a take-home challenge brief from FieldData, written
in Spanish, for a weather-alerts feature of Agrobot. Its relevant points:

*   Agrobot is a farm-management assistant over WhatsApp, built with Python,
    FastAPI and PostgreSQL.
*   An ingestion job already stores weather data: for each future date there is
    one entry per weather event with its probability. The table is to be
    defined and its data mocked.
*   Users configure alerts on their fields: an event type and a probability
    threshold.
*   A background job periodically evaluates the forecasts against the
    thresholds and generates notifications; the user must be notified.
*   No WhatsApp integration is needed: the endpoints and the evaluation job are
    enough.
*   Preferred stack: FastAPI, SQLAlchemy, PostgreSQL, Alembic; other tools need
    a justification.
*   Deliverables: a repository, a README (how to run it, decisions taken),
    working migrations and tests. Missing parts may be documented.
*   The brief asks for production-ready code, a solid data model, and
    asynchrony. The recruiter's email adds that the solution is judged on
    quality, production readiness and the soundness of the technical
    decisions.

## Assumptions

1.  No weather provider is chosen. The design is modelled on how forecast
    APIs such as Open-Meteo, OpenWeather and the US National Weather Service
    answer: you send a point and get the forecast for the area around it.
    Open-Meteo answers for the grid cell it used (it returns that cell's
    coordinates), the US National Weather Service returns the grid cell's
    outline, and OpenWeather answers for the requested point. This design
    stores a zone by its centre, which every grid provider can supply.
2.  The existing ingestion job fetches forecasts for every known zone and
    upserts one row per zone, event type and date, updating it as forecasts
    refresh. Its upsert changes only `probability` and `updated_at` (so it
    never takes the row lock that would block a foreign-key check from a
    notification), it writes in short transactions, and it purges only
    forecasts for dates before today in the same forecast timezone.
3.  A field is an area (in Agrobot it has an outline). Agrobot already knows
    every zone a field touches and the fraction of the field in each zone (its
    *coverage*), in the same way it already has the forecasts: the brief says
    the weather data is already ingested, so the data that makes it usable per
    field is assumed to come with it. The coverages of one field add up to
    100%. Here the seed writes this mapping. If Agrobot does not have it, it
    is computed as described in
    [Appendix: computing the field-to-zone mapping](#appendix-computing-the-field-to-zone-mapping).
4.  Every event is expressed as a probability, so a threshold is a probability
    in percent, from 1 to 100.
5.  The threshold is inclusive: an event counts when its probability is greater
    than or equal to the threshold.
6.  A field can have several users, each with a role. An alert belongs to the
    user who created it, and only that user is notified.
7.  Users and fields already exist in Agrobot. Minimal stand-in tables are
    created and seeded here; user and field management is not built.
8.  Forecast dates are local calendar dates. "Today" is computed in one
    configured timezone (default `America/Argentina/Buenos_Aires`), not in UTC.

## Goals

The goals are grouped. **Required** is what the challenge brief (see
[Source of the requirements](#source-of-the-requirements)) asks for literally.
**Graded** is not asked for literally, but answers what the brief says it looks
for (production-ready code, a solid data model, asynchrony) and what the
recruiter's email says the solution is judged on (quality, production
readiness, and the soundness of the technical decisions, which
[Alternatives Considered](#alternatives-considered) answers). **Extra** is
built because it makes the result more complete, not because anything asks for
it.

**Required**

*   A user can create, list, read, change and delete alerts on the fields they
    are a member of, through an HTTP API, and cannot see or change anyone
    else's alerts or notifications.
*   A background job periodically evaluates the stored forecasts against every
    active alert and records a notification when an alert is met; the user's
    notifications can be listed through the API.
*   A data model with working migrations, tests, and a README that explains how
    to run the system and the decisions taken.

**Graded**

*   An alert has at most one notification per forecast date at any time, and
    at most one per date ever reaches the *sent* state, no matter how often the
    evaluation runs, how many worker processes run it, or how often it is
    retried. Delivery of the message itself is at least once (see
    [Delivery (overview)](#delivery-overview)).
*   An alert fires for a date only when the zones whose forecast reaches its
    threshold cover at least a configured share of the field (default 10%).
*   All pending notifications of one alert are delivered as one message that
    lists the dates.
*   A failed delivery is retried with exponential backoff and jitter for as
    long as its forecast date has not passed; a notification whose date passes
    undelivered is marked failed. Detection never waits for delivery.
*   All I/O is asynchronous, with the one exception listed under
    [Concurrency model (details)](#concurrency-model-details); the worker
    shuts down cleanly on SIGTERM (see
    [Delivery (overview)](#delivery-overview)).
*   The schema enforces the data rules it can express (foreign keys with
    explicit delete behaviour, uniqueness, value ranges; rules 1–5, 7, 9 and 10
    under [Conceptual model](#conceptual-model)), so code that bypasses the API
    cannot store data that breaks them. Rules 6 and 8 are enforced by the code
    that writes the data and by tests.
*   Configuration from environment variables, structured logs, health checks,
    type checking, a Docker Compose stack started by one `docker compose up`
    (migrations and demo data included), CI, and tests against a real
    PostgreSQL with at least 90% line coverage.

**Extra**

*   The numbers in a message are current as of the re-check just before
    sending: each date is re-checked against the current alert and forecasts.
*   Endpoints that list the caller's fields and the event types, and pagination
    on every list.
*   A single-cycle mode of the worker for external schedulers.
*   Concurrency tests: two evaluations started at once must give one run, one
    skip and no duplicate notifications; two dispatchers started at once must
    claim different alerts.
*   The conceptual model diagrams and a build log of how the design changed.
*   A demo page at `/demo`, in Spanish, served by the API: a map of the seeded
    fields over their forecast zones with a date bar, a simulated phone chat
    showing the messages (with a simulated channel outage), and per-field
    forecast tables, with a minimal line icon per event type. The page never
    re-implements the alert rule or the message format: whether an alert
    fires on a date, and with what affected area, comes from the server
    (computed with the same matching rule as evaluation), and the chat shows
    the caller's real notifications. The simulated outage is a browser-only
    illustration and the page says so. It reads the API, plus one read-only
    endpoint for a field's outlook. Field outlines on the map are
    illustration data shipped with the page, not stored in the database.

## Non-Goals

*   A real WhatsApp client. The notifier port has one implementation that logs.
*   A real weather-provider adapter. Forecast rows are mocked by a seed script.
*   Computing which zones a field touches, and with what coverage (see
    [Assumption 3](#assumptions)).
*   Hail alerts are left out; adding them later is one row.
*   PostGIS. There is no spatial query in this design.
*   Per-alert settings for the minimum affected area (the share of the field
    that must be affected, see [Alert semantics](#alert-semantics)) or for the
    evaluation window (how many days ahead are checked); a per-field timezone.
*   Notifying other members of the field; only the alert's owner is notified.
*   Keeping notification history after an alert is deleted.
*   Storing the rendered message text for audit.
*   Real authentication, and user or field management (both already exist in
    Agrobot; this design uses minimal stand-in tables).
*   Re-notification rules: re-arming when a probability drops and rises again,
    escalation, or an "all clear" message.
*   PostgreSQL row-level security as a second layer under the ownership checks.
*   Lease-based claiming of notifications.
*   Metrics, tracing and rate limiting.
*   Retention and partitioning for the notification and forecast tables.
*   Keyset pagination and total counts.
*   Re-queueing notifications that ended as failed.
*   Non-blocking log writes, and several server processes per container
    (replicas are the scaling unit).

## Open Questions

None.

## Critical User Journeys (CUJs)

### CUJ 1: A farmer is warned about frost on their field

*   **Given**: a user who is a member of the field "La Esperanza"; the field
    lies in one zone; today is 13/11; the stored forecasts for that zone give
    frost 85% two days from today and 72% three days from today.
*   **When**: the user creates a frost alert with threshold 70% on that field,
    and the worker's next evaluation and delivery cycles run.
*   **Then**: two notifications are recorded, one per date, and the user
    receives **one** message:

    ```text
    ⚠️ Alerta de helada en "La Esperanza" (umbral 70%): 15/11 85%, 16/11 72%.
    ```

    Listing the user's notifications shows both as sent. Further evaluation
    cycles create nothing new for those dates.

### CUJ 2: A new date qualifies later in the week

*   **Given**: CUJ 1 has completed.
*   **When**: the ingestion job raises the frost probability for five days from
    today to 90%, and the worker runs again.
*   **Then**: one new notification is recorded and one new message is sent,
    listing only that date.

### CUJ 3: The messaging channel is down

*   **Given**: a pending notification and a notifier that fails.
*   **When**: the delivery cycle runs repeatedly.
*   **Then**: each failure is recorded on the notification (attempt count, last
    error) and the next attempt is scheduled with exponential backoff and
    jitter (see
    [Evaluation and delivery (details)](#evaluation-and-delivery-details));
    evaluation keeps creating new notifications meanwhile, and a new date of
    the same alert waits for the same next attempt instead of triggering an
    early one. When the channel recovers, the alert's pending dates go out in
    one message. A date that passes before the channel recovers is marked
    failed, and an error is logged once per message.

### CUJ 4: A large ranch with a sliver in a neighbouring zone

*   **Given**: a field whose outline is 95% in zone A and 5% in zone B; a rain
    alert at 60%.
*   **When**: only zone B's forecast reaches 60% for tomorrow.
*   **Then**: no notification is created, because 5% is below the default
    minimum affected area of 10%. If zone A's forecast also reaches 60%, one
    notification is created for that date, with an affected area of 100% and
    the higher of the two probabilities.

### CUJ 5: Two members of the same field

*   **Given**: two users who are both members of one field.
*   **When**: each creates a frost alert on it, with thresholds 60% and 80%,
    and the forecast for a date is 70%.
*   **Then**: only the first user is notified for that date. Neither user can
    read, change or delete the other's alert, and each sees only their own
    notifications.

## Technical Plan (Architecture)

### Processes

```mermaid
flowchart LR
    client([Client]) -->|HTTP| api["api<br/>(FastAPI, async)"]
    api --> db[(PostgreSQL)]
    subgraph worker["worker (asyncio)"]
        evaluate["evaluation ⟳"]
        dispatch["dispatch ⟳"]
    end
    evaluate --> db
    dispatch --> db
    dispatch -->|send| notifier["Notifier port<br/>(log stand-in for WhatsApp)"]
    agrobot["Agrobot: ingestion job and field-to-zone mapping<br/>(exist, mocked by the seed)"] --> db
```

*   **api**: the HTTP interface. Users manage alerts on their fields, list the
    fields they belong to and the event types, and read their notifications.
*   **worker**: a separate process with two independent periodic jobs, each
    on its own interval: evaluation and dispatch. Each job's state lives in the
    database, so the worker can be restarted, replicated or run once by an
    external scheduler.
*   Both processes are the same container image with different commands and
    share the data model and the business services.
*   No message broker. The job is a periodic batch over database state: a
    unique constraint makes detection idempotent and an outbox table makes
    delivery durable, so a broker would add a moving part without adding a
    guarantee.

### Data flow

```mermaid
sequenceDiagram
    participant U as User
    participant A as API
    participant DB as PostgreSQL
    participant E as Evaluation
    participant D as Dispatch
    participant N as Notifier
    Note over DB: Agrobot provides fields, their zones with coverage,<br/>and forecasts per zone (mocked by the seed)
    U->>A: create alert (field, event type, threshold)
    A->>DB: insert alert (ownership checked)
    E->>DB: one statement: matching (alert, date) pairs<br/>become pending notifications, duplicates ignored
    D->>DB: claim alerts with due notifications, all their pending rows<br/>(skip rows locked by others)
    D->>N: one message per alert, all alerts concurrently
    D->>DB: mark sent, or schedule a retry, or mark failed
    U->>A: list notifications
    A->>DB: the caller's notifications, newest first
```

### Conceptual model

The conceptual model is an entity-relationship diagram in Chen notation:
rectangles are entities, diamonds are relationships, ovals are attributes (keys
underlined), and the letters on the lines are cardinalities. It describes the
domain, so FIELD keeps its `outline`, which is where the field-to-zone mapping
comes from; the tables do not store it, because nothing in this system reads it.
The first diagram shows entities, relationships and cardinalities; the second
adds every attribute, including the attributes of relationships.

![Conceptual model: entities and relationships](er-simple.svg)

![Conceptual model, complete with attributes](er-complete.svg)

| Diagram | Table or column |
|---|---|
| USER | `users` |
| FIELD | `fields` (`outline` is not stored) |
| ZONE | `zones` |
| EVENT TYPE | `event_types` |
| FORECAST (one probability per zone, event type and date) | `weather_forecasts` |
| ALERT | `alerts` |
| NOTIFICATION | `notifications` (`detectedThreshold` → `threshold`, `detectedProbability` → `probability`) |
| MEMBER OF (with `role`) | `field_members` |
| LIES IN (with `coverage`) | `field_zones` |
| APPLIES TO, IS OF, DEFINED BY, ON, WATCHES, BELONGS TO, TRIGGERED BY | foreign keys: `weather_forecasts.zone_id`, `weather_forecasts.event_type_id`, `alerts.user_id`, `alerts.field_id`, `alerts.event_type_id`, `notifications.alert_id`, `notifications.forecast_id` |

Rules the diagram cannot express:

1.  In ALERT, the triple (user, field, event type) is unique: one alert per
    user, field and event type.
2.  The user who defines an alert on a field must be a MEMBER OF that field.
3.  In FORECAST, (zone, event type, forecast date) is unique.
4.  In ZONE, (latitude, longitude) is unique.
5.  In NOTIFICATION, there is at most one per alert and forecast date.
6.  A notification's forecast has the alert's event type, and its zone is one
    of the zones of the alert's field.
7.  `threshold` is between 1 and 100; `probability` and `detectedProbability`
    between 0 and 100; `coverage` and `affectedArea` greater than 0% and at
    most 100%.
8.  The `coverage` values of one field add up to 100%.
9.  A notification whose `status` is *sent* has `sentAt`.
10. TRIGGERED BY is optional on the notification's side: the link is cleared
    when the ingestion job purges old forecasts, while the notification keeps
    its recorded values.

`createdAt` is drawn only on NOTIFICATION because it is part of what the user
sees in the notification history. In the tables, every table with a surrogate
key has `created_at`, and `fields`, `weather_forecasts`, `alerts` and
`notifications` also have `updated_at` (see [Schema](#schema)).

Rules 1 to 5, 7, 9 and 10 are enforced by the database schema (rule 5 is a
unique key on alert and forecast date; the notification keeps a surrogate
identifier). Rule 2 is a composite foreign key. Rule 6 is enforced by the
evaluation and dispatch queries, the only writers of notifications, and is
covered by a planned test. Rule 8 is a property of the field-to-zone mapping
that Agrobot provides (Assumption 3); the seed respects it and a planned test
checks it.

This document gives coverage and affected area as percentages (for example
60%, or the 10% minimum); the database stores them as fractions between 0
and 1.

### Relational model

The relational form of the conceptual model, as created by the migrations.
The diagram leaves out the direct foreign keys from `alerts` to `users` and
`fields`, which exist alongside the composite key to `field_members` (see
[Schema](#schema)):

```mermaid
erDiagram
    users ||--o{ field_members : "is member"
    fields ||--o{ field_members : "has member"
    fields ||--o{ field_zones : "lies in"
    zones ||--o{ field_zones : "covers"
    zones ||--o{ weather_forecasts : "has"
    event_types ||--o{ weather_forecasts : "of type"
    field_members ||--o{ alerts : "defines"
    event_types ||--o{ alerts : "of type"
    alerts ||--o{ notifications : "produces"
    weather_forecasts |o--o{ notifications : "triggered by"

    users {
        bigint id PK
        text first_name
        text last_name
        text phone_number UK
    }
    fields {
        bigint id PK
        text name
    }
    field_members {
        bigint user_id PK, FK
        bigint field_id PK, FK
        varchar role
    }
    zones {
        bigint id PK
        numeric latitude
        numeric longitude
    }
    field_zones {
        bigint field_id PK, FK
        bigint zone_id PK, FK
        numeric coverage
    }
    event_types {
        bigint id PK
        varchar code UK
        text name
    }
    weather_forecasts {
        bigint id PK
        bigint zone_id FK
        bigint event_type_id FK
        date forecast_date
        smallint probability
    }
    alerts {
        bigint id PK
        bigint user_id FK
        bigint field_id FK
        bigint event_type_id FK
        smallint threshold
        boolean is_active
    }
    notifications {
        bigint id PK
        bigint alert_id FK
        bigint forecast_id FK
        date forecast_date
        smallint threshold
        smallint probability
        numeric affected_area
        varchar status
        smallint attempts
        timestamptz next_attempt_at
        text last_error
        timestamptz sent_at
    }
```

Every table with a surrogate key also has `created_at`; `fields`,
`weather_forecasts`, `alerts` and `notifications` also have `updated_at`. All
timestamps carry a time zone.

Design points of the model:

*   **Users and fields** are many-to-many through a membership with a role
    (owner, agronomist, manager). No rule here depends on the role; it is
    kept because it mirrors Agrobot's real membership, which these stand-in
    tables model, and the fields listing returns it.
*   **A zone** is identified by its centre, stored at the same fixed precision
    the provider uses so that lookups by coordinates always match.
*   **A field's zones** carry a coverage: the fraction of the field inside that
    zone. A field's coverages add up to 100%. The mapping is provided by
    Agrobot (Assumption 3).
*   **Event types** are a table (code, Spanish label), not a fixed list in
    code, so a new type is a row, not a deploy, and the label used in messages
    lives with the type. Four types are created by the migration: frost
    (helada), rain (lluvia), strong wind (viento fuerte) and extreme heat
    (calor extremo).
*   **Forecasts** belong to a zone and an event type, one row per zone, type and
    date, which is also the ingestion job's upsert key. Probabilities are whole
    percentages from 0 to 100, stored as small integers so the comparison with
    the threshold is exact.
*   **An alert** belongs to a membership: the database refuses an alert whose
    user is not a member of its field, and removing a membership removes that
    user's alerts on the field. One alert per user, field and event type.
*   **A notification** always belongs to an alert and is deleted with it; the
    recipient is the alert's user. It records the date, threshold, probability
    and affected area it reports. While it is pending these values are
    refreshed just before each send attempt (see
    [Delivery (overview)](#delivery-overview)); once it is sent or failed they
    are frozen, so later changes to the alert or the forecast do not rewrite
    what the user was told. It keeps an optional link to the forecast that
    triggered it, which is cleared if the ingestion job purges old forecasts.
    The link is a real foreign key to a table the ingestion job owns, kept
    on purpose for integrity (it never points to a missing forecast). Its
    costs are accepted: the purge also clears the link on notifications
    (served by an index), the purge and a dispatch batch can briefly wait on
    each other, and Assumption 2 states how the ingestion job writes.
    It also carries its delivery state: pending, sent or failed, the number of
    attempts, the time of the next attempt, the last error and the time it was
    sent.
*   **Identifiers** are 64-bit integers generated by the database. Ownership
    checks answer "not found" for other users' resources, so sequential
    identifiers do not reveal whether another user's resource exists.

### Alert semantics

For one alert and one date inside the window (today to today plus 7 days, both
included):

1.  Take the zones of the alert's field whose forecast for the alert's event
    type reaches the threshold.
2.  The **affected area** is the sum of their coverages.
3.  The alert fires for that date if the affected area is at least the
    configured minimum (default 10%).
4.  The notification records the affected area and points to the affected
    forecast with the highest probability; its probability is that one.

At most one notification exists per alert and date. A date for which a
notification was **sent** is never notified again for that alert, even if its
probability drops and rises again. A pending notification that stops
qualifying before it is sent is deleted, and the date can be detected again
later.

Delivery groups notifications: all pending notifications of one alert go out as
**one message listing the dates**, in ascending order. The share of the field
is shown only when its rounded percentage is below 100.

**Worked example.** "La Esperanza" lies 60% in zone A and 40% in zone B. Its
owner has a frost alert at 70%. Today is 13/11. Forecasts:

| Date | Zone A | Zone B | Zones at or above 70% | Affected area | Fires? |
|---|---|---|---|---|---|
| 15/11 | 85% | 50% | A | 60% | yes, 85% |
| 16/11 | 72% | 75% | A, B | 100% | yes, 75% |
| 17/11 | 40% | 8% | none | 0% | no |
| 18/11 | 65% | 71% | B | 40% | yes, 71% |

The first evaluation creates three notifications and the next dispatch sends
**one** message:

```text
⚠️ Alerta de helada en "La Esperanza" (umbral 70%): 15/11 85% (60% del campo), 16/11 75%, 18/11 71% (40% del campo).
```

If the next day's ingestion raises zone A on 19/11 to 90%, the next cycle sends
one more message for 19/11 only. Over a whole week the user receives at most
one message per alert per dispatch cycle in which a new date qualifies, apart
from the at-least-once duplicates described under
[Delivery (overview)](#delivery-overview).

### Evaluation (overview)

The evaluation job turns forecasts into pending notifications with **one
set-based SQL statement**: it joins alerts to their field's zones and to those
zones' forecasts, keeps the forecasts at or above the threshold within the
window, sums coverages per alert and date, picks the highest-probability
forecast per alert and date, and inserts one notification per qualifying alert
and date. A unique key on alert and date guarantees at most one notification
per alert and date, however often and by however many workers the statement
runs; that key is what correctness rests on. Only one worker evaluates at a
time, to avoid repeated work; the others skip their turn. The statement
only inserts; it never sends anything. "Today" is computed in the
configured forecast timezone.

The rules in full are under
[Evaluation and delivery (details)](#evaluation-and-delivery-details).

### Delivery (overview)

Detection and delivery are decoupled by the notifications table, which acts as
an outbox:

1.  **Claim alerts.** The dispatch job locks up to 20 alerts whose pending
    notifications are **all** due (oldest first), skipping alerts another
    dispatcher has locked, and then takes all pending notifications of the
    alerts it locked. A lock tells other dispatchers "someone is working on
    this alert"; it is released when the batch is saved. The alert is the unit
    of claiming because it is the unit of a message: an alert is handled by
    one dispatcher at a time, its dates are never split across two messages,
    and each dispatcher replica gets different alerts. The job keeps taking
    batches until a batch is not full or a shutdown is requested.
2.  **Expire.** A claimed notification whose date is before today (in the
    forecast timezone, like every date in this system) can no longer be
    useful. If its alert is still active and its threshold is not above the
    recorded probability, it is marked failed and an error is logged (the user
    wanted it and did not get it); otherwise it is deleted quietly (the user
    turned the alert off or raised the threshold). A notification for today
    can still be sent today.
3.  **Re-check.** The remaining dates are re-checked against the current alert
    and the current forecasts, with the same rule evaluation uses. A date that
    no longer qualifies (the alert was deactivated, its threshold was raised,
    or the forecast dropped) is deleted: it was never sent, so no history is
    lost, and if it qualifies again later, evaluation detects it again. A date
    that still qualifies gets its recorded values (threshold, probability,
    affected area, triggering forecast) refreshed, so the message shows current
    numbers.
4.  **Group** the rest by alert: one message per alert, addressed to the
    alert's user.
5.  **Send** all messages of the batch concurrently, each with a timeout.
6.  **Record.** A success marks every notification of the group sent. A
    failure counts one attempt for the whole group, stores the error, and
    schedules the group's next attempt with exponential backoff and random
    jitter, at most 30 minutes apart. The new next-attempt time is written to
    every pending notification of the alert, including one that evaluation
    inserted while this batch was being sent. There is no fixed attempt
    limit: retries continue until the dates pass (step 2 ends them).

**Retry schedule belongs to the alert.** When evaluation detects a new date for
an alert whose older dates are waiting for a retry, the new date takes the
same next-attempt time, so it neither triggers an early retry nor waits apart
from them; it goes out in the same message.

**Shutdown drains.** When the worker is asked to stop, the dispatch job takes
no new batch, finishes the batch it is sending, saves the results and exits.
Only a second stop signal or a hard kill interrupts a batch: the batch is then
rolled back and its notifications stay pending, as after a crash.

The batch's transaction stays open during the sends, bounded to one batch for
at most one send timeout (see Alternative T for what this costs). Delivery to
the channel is **at least once**: a message can reach the user twice after a
crash, a failed final save, or a send that timed out after the provider had in
fact delivered it, at most one batch's worth. A normal stop causes no
duplicate. Exactly-once delivery needs the provider to deduplicate (see
[Evaluation and delivery (details)](#evaluation-and-delivery-details)).

The rules in full are under
[Evaluation and delivery (details)](#evaluation-and-delivery-details).

### API (overview)

All endpoints except health live under `/api/v1`. The caller identifies
themselves with an `X-User-Id` header, a stand-in for real authentication: a
missing, malformed or unknown identity is rejected as unauthenticated, and any
resource that belongs to another user is answered as not found, even when both
users are members of the same field. How real authentication replaces the
stand-in is described under [API (details)](#api-details).

| Operation | Purpose |
|---|---|
| Liveness and readiness | Liveness always succeeds while the process runs; readiness checks the database |
| List my fields | Fields the caller is a member of, with the caller's role |
| List event types | The four types, with code and Spanish label |
| Create, read, list, change, delete my alerts | Change only the threshold and the active flag; field and event type are fixed for the alert's life |
| List my notifications | Newest first, filterable by status, field and alert |
| Outlook of my field (demo page) | The field's zones with coverage, its forecasts for the window, and per date whether each of the caller's alerts on it fires |

Lists are paginated with a limit (1 to 100, default 50) and an offset. Input
errors (out-of-range threshold, empty change) are rejected before reaching the
database; an unknown event type is rejected with 422 after a lookup. A
duplicate alert is reported as a conflict, detected by the database's
uniqueness rule rather than by a prior read.

Paths, bodies and status codes are under [API (details)](#api-details).

### Concurrency model (overview)

The workload is I/O-bound: almost all the time is spent waiting for PostgreSQL
or for the messaging provider.

| Work | Kind | Where it runs |
|---|---|---|
| HTTP requests, database queries, notifier sends | I/O-bound (waiting) | asyncio, one event loop per process |
| Matching alerts against forecasts | CPU and I/O, grows with data | Inside PostgreSQL (one set-based statement) |
| Grouping a batch, rendering text, validating input | CPU, small and bounded | Inline on the event loop |
| Using more than one core | Parallelism | More processes: API and worker replicas |

More cores are used by running more processes, not threads, because the GIL
lets only one thread execute Python at a time. The rules that make this hold,
and the one accepted exception, are listed under
[Concurrency model (details)](#concurrency-model-details) in the
[Detailed Implementation Plan](#detailed-implementation-plan).

Replicas coordinate only through the database: one replica evaluates at a
time (the others skip their turn), and dispatchers each lock different alerts.
More worker replicas add availability and sending throughput, not evaluation
speed.

## Alternatives Considered

The main decisions, and how they were discussed, are recorded in the
[build log](../build-log.md).

### Alternative A: A task queue (Celery, arq, Taskiq)

*   **Pros**: well-known scheduling, retries and monitoring built in.
*   **Cons**: Celery, arq and Taskiq need Redis or RabbitMQ, an extra service
    the brief's stack does not include and that would need a justification;
    the state already lives in PostgreSQL.
*   **Reason for Rejection**: A periodic batch over database state needs no
    broker. The unique constraint gives idempotency, the outbox gives
    durability, and a small asyncio runner keeps signal handling and shutdown
    in this codebase, where they get their own tests.

### Alternative B: A scheduler library (APScheduler)

*   **Pros**: well-known scheduling built in; needs no broker.
*   **Cons**: adds a dependency whose job stores and executors this design
    does not use.
*   **Reason for Rejection**: The runner needed here does three things (run,
    wait for the rest of the interval or a stop signal, never overlap) and its
    shutdown behaviour is explicit and gets its own tests; a dependency
    whose job stores and executors go unused does not pay for itself.

### Alternative C: One message per date

*   **Pros**: simplest delivery; one notification is one message.
*   **Cons**: a rainy week produces a burst of up to eight messages per alert.
*   **Reason for Rejection**: Grouping by alert sends one message per cycle.

### Alternative D: An edge-triggered state machine per alert

*   **Pros**: can express re-arming and "all clear".
*   **Cons**: more state, more transitions to test and explain.
*   **Reason for Rejection**: "At most one per alert and date" is a single rule,
    enforced by a unique constraint, and is idempotent under retries and
    concurrent workers. Re-notification rules are listed under
    [Future improvements](#future-improvements).

### Alternative E: Send inside the evaluation loop

*   **Pros**: no outbox table.
*   **Cons**: a messaging outage blocks or loses detection; a crash between
    sending and recording causes repeated sends.
*   **Reason for Rejection**: Detection must not depend on delivery.

### Alternative F: Evaluate alerts in Python

*   **Pros**: logic in the application language, where a Python debugger can
    step through it.
*   **Cons**: loads every alert and forecast, N+1 queries, CPU-bound Python on
    the event loop.
*   **Reason for Rejection**: One set-based statement is one round trip, atomic,
    and idempotent by constraint.

### Alternative G: User id in the path, no identity

*   **Pros**: simpler to call.
*   **Cons**: ownership rules are neither enforced nor testable.
*   **Reason for Rejection**: A header stub lets ownership be enforced and
    tested; how real authentication replaces it is described under
    [API (details)](#api-details).

### Alternative H: Event types as a fixed list in code

*   **Pros**: no lookup to validate a type.
*   **Cons**: a new type needs a deploy; the Spanish label lives in code.
*   **Reason for Rejection**: A table gives a real foreign key and makes a new
    type a row.

### Alternative I: Probability as a fraction or a float

*   **Pros**: matches some provider APIs.
*   **Cons**: inexact comparisons (float), or a format farmers do not use.
*   **Reason for Rejection**: A small integer percentage is exact and matches
    how a farmer says it ("70%").

### Alternative J: Keep notifications after the alert is deleted

*   **Pros**: history survives.
*   **Cons**: a nullable alert link, and a direct link to the user, which is a
    redundant path to the recipient.
*   **Reason for Rejection**: Every notification has exactly one alert, enforced
    by the schema, and the recipient is reached through it. History disappears
    with the alert; keeping it is listed under
    [Future improvements](#future-improvements).

### Alternative K: UUID identifiers

*   **Pros**: not guessable; can be generated by clients.
*   **Cons**: larger indexes.
*   **Reason for Rejection**: Every row is created by the server, so
    client-generated identifiers are not needed; ownership checks answer "not
    found" for other users' resources (see
    [Relational model](#relational-model)).

### Alternative L: SQLite or rollback-per-test fixtures for tests

*   **Pros**: faster, no server needed.
*   **Cons**: conflict handling, row-lock skipping and check constraints are
    PostgreSQL behaviour; rollback fixtures prevent tests that use several
    connections at once.
*   **Reason for Rejection**: Tests run against a real PostgreSQL with the
    schema created by the migration, and tables emptied between tests.

### Alternative M: Synchronous code with threads, or the free-threaded build

*   **Pros**: familiar synchronous libraries.
*   **Cons**: a thread per wait; because of the GIL, CPU-bound threads do not
    run Python in parallel; the free-threaded build is not used, for the
    reasons under [Concurrency model (details)](#concurrency-model-details).
*   **Reason for Rejection**: The workload is I/O-bound, so one event loop per
    process handles it; parallelism comes from processes.

### Alternative N: One owner per field

*   **Pros**: fewer tables.
*   **Cons**: ranches have several people.
*   **Reason for Rejection**: Memberships with roles let a field have several
    users, each with a role.

### Alternative O: One coordinate per field

*   **Pros**: fewer tables.
*   **Cons**: a large field spans several zones.
*   **Reason for Rejection**: Zones with coverage record every zone a field
    touches and the fraction of the field in each.

### Alternative P: Forecasts keyed by field

*   **Pros**: fewer tables.
*   **Cons**: neighbouring fields would duplicate forecasts.
*   **Reason for Rejection**: Forecasts keyed by zone keep weather data
    independent of fields and keep the evaluation join a plain equality.

### Alternative Q: Fire when any zone reaches the threshold

*   **Pros**: simpler.
*   **Cons**: a sliver of a field in a neighbouring zone would trigger alerts.
*   **Reason for Rejection**: A global minimum affected area, summing coverages,
    ignores slivers below the minimum and adds up several small affected zones.

### Alternative R: A per-alert minimum affected area

*   **Pros**: more control.
*   **Cons**: a per-alert setting adds API surface nobody asked for.
*   **Reason for Rejection**: One global minimum affected area applies to every
    alert; a per-alert setting is listed under
    [Future improvements](#future-improvements).

### Alternative S: Integrate a real weather API

*   **Pros**: real data.
*   **Cons**: the brief asks for mocked data; choosing a vendor is a business
    decision.
*   **Reason for Rejection**: The brief says forecasts are already ingested; the
    seed mocks them in the shape the ingestion job is assumed to write.

### Alternative T: Lease-based claiming instead of a transaction held during sends

*   **Pros**: short transactions even with a slow provider.
*   **Cons**: a lease length to tune (too short and a second dispatcher sends
    the same message while the first is still sending; too long and a crashed
    worker's alerts wait longer); claim and record become two separate
    transactions, so edits and new dates between them need their own rules.
*   **Costs of the chosen design** (accepted): while a batch is sending, its
    alerts' locks and one connection are held for up to one send timeout, so
    a PATCH or DELETE of such an alert answers 503 with `Retry-After`;
    connections set lock, statement and idle-in-transaction timeouts and TCP
    keepalives so that a dead worker's locks are freed; a waiting API request
    holds a pooled connection; the forecast purge and dispatch can briefly
    wait on each other (see [Risks and Mitigations](#risks-and-mitigations)).
*   **Reason for Rejection**: With the transaction held, a crash rolls the
    batch back and PostgreSQL releases its locks with no cleanup code and no
    expiry to tune; the cost is bounded to one batch for one send timeout. A
    lease for slow providers is listed under
    [Future improvements](#future-improvements).

### Alternative U: Compute the field-to-zone mapping in this system

*   **Pros**: the system works for a field whose zones nobody has computed yet.
*   **Cons**: a third periodic job, a weather-provider port with a fake, outline
    storage and validation, and sampling code, none of which the brief asks
    for; the mapping belongs to the side that ingests forecasts and owns
    fields.
*   **Reason for Rejection**: The mapping is assumed to exist in Agrobot (see
    [Assumption 3](#assumptions)). The way to compute it is described in
    [Appendix: computing the field-to-zone mapping](#appendix-computing-the-field-to-zone-mapping)
    and listed under [Future improvements](#future-improvements).

### Alternative V: Evaluate only when something changes

*   **Pros**: less work at very large scale; a changed forecast or a new alert
    is evaluated at once instead of within 5 minutes.
*   **Cons**: it needs a trigger or `LISTEN/NOTIFY` on a table this system
    does not own; it needs exact "this forecast
    changed, so these alerts are affected" logic; a missed event is a missed
    alert, so a periodic full run is still needed as a safety net.
*   **Reason for Rejection**: A full run is one statement and, as an
    estimate, takes about a second for 10,000 alerts; it cannot miss a change.
    Change-driven evaluation is listed under
    [Future improvements](#future-improvements) for scale.

### Alternative W: Store the retry schedule once per alert

*   **Pros**: the retry state (attempts, next attempt time, last error) lives
    in one place, so the three rules that keep an alert's pending
    notifications on the same schedule go away and the claim query is
    simpler.
*   **Cons**: either the alert mixes the user's settings with delivery
    state, or a tenth table is added; the evaluation and claim statements
    would be redesigned after the concurrency review that covers the current
    ones.
*   **Reason for Rejection**: Accepted trade-off. An alert has at most eight
    pending dates (one per day of the window), the three rules are each
    covered by a test, and each notification keeps its own delivery history.
    Moving the schedule to the alert is a contained change, listed under
    [Future improvements](#future-improvements).

--------------------------------------------------------------------------------

## Non-Functional Requirements

*   **Security & Privacy**: identity is a stub header (documented shortcut):
    anyone who can reach the API can act as any user, so the Compose stack
    publishes the API only on `127.0.0.1`, and the README says the stub must
    never face a network. Every API query on user-owned resources is scoped to
    the caller. Phone numbers are never logged or stored in error text.
    Database credentials come from the environment.
*   **Performance & Latency**: evaluation is one statement served by the
    forecast table's unique index; the claim query is served by a partial index
    on pending notifications. A send is bounded by its timeout.
*   **Reliability**: detection is idempotent; delivery is at least once (see
    [Delivery (overview)](#delivery-overview) for the duplicate cases);
    failures are retried with backoff until the forecast date passes.
*   **Operability**: configuration comes from environment variables with
    defaults, validated at startup; logs are JSON with static messages and
    identifiers as fields, structured so an error tracker or dashboard such
    as Sentry or Grafana can alert on `notification_expired` or `job_failed`;
    the API has liveness and readiness endpoints; the worker writes one
    heartbeat per job, and its container is marked unhealthy when the
    dispatch heartbeat is stale (evaluation
    failures alert instead of restarting the sender); the worker stops cleanly on
    SIGTERM (see [Delivery (overview)](#delivery-overview)) and can run a
    single cycle for external schedulers.
*   **Rollout & Rollback**: a new system, on by default. This feature owns
    three tables (`event_types`, `alerts`, `notifications`), created by their
    own migration; the other six are stand-ins for Agrobot's tables, in a
    separate, earlier migration used only for the demo and tests (in Agrobot
    the feature's migration would be attached to Agrobot's own migrations
    instead). Both are reversible and checked up, down and up again in CI;
    rolling back the feature drops only its own tables.

## Risks and Mitigations

Figures are orders of magnitude, to show where each limit starts to matter.

| Risk or limit | Where it starts to matter | Mitigation now | Next step |
|---|---|---|---|
| Evaluation cost grows with active alerts × zones per field × 8 dates, every 5 minutes | Estimate: about 10,000 alerts × 2 zones × 8 dates is 160,000 index lookups per run, about a second. The run is all or nothing: if it passes its 240-second statement timeout it fails every time, for every alert; that timeout would be reached only orders of magnitude above this scale (an estimate, not measured) | One set-based statement on the forecast table's unique index; one replica evaluates at a time (advisory lock) | Evaluate only alerts whose forecasts changed, or in chunks of alerts |
| Dispatch throughput while the channel is down | Estimate: one dispatcher handles about 20 alerts per 10-second timeout, about 120 alerts a minute; with retries about 15 minutes apart on average, above about 1,800 alerts with pending dates one dispatcher cannot keep up, retries come less often than the backoff says, and one slow recipient holds its batch for the full timeout | First attempts are claimed before retries, so new warnings do not wait behind the backlog; replicas claim different alerts, so throughput adds up | A circuit breaker for channel-wide outages; lease-based claiming (Alternative T) |
| An alert on a field with no zone mapping never fires | A field Agrobot has not yet linked to zones, or one whose coverages add up to less than 100% | The alert response says `zones_mapped: false`; each evaluation logs `alerts_without_zones` | A field-mapping check in Agrobot |
| Forecasts stop arriving | The ingestion job fails or skips zones; old forecasts look like calm weather | Each evaluation logs `forecasts_stale` with the zones (linked to active alerts) that miss a date in the window or are older than 24 hours | A readiness signal on forecast age |
| The ingestion job does not write as Assumption 2 says | It refreshes by delete and insert, changes key columns, or holds long transactions | Locks and timeouts make evaluation fail loudly (`job_failed`) instead of waiting | Confirm the write pattern with the Agrobot team; a test against its upsert |
| A long outage keeps the sender busy for minutes | A backlog of hundreds of alerts while the channel times out | The dispatch heartbeat is written after every batch, so a busy sender stays healthy | A circuit breaker for channel-wide outages |
| A message reaches a user twice | After a crash, a failed final save, or a send that timed out after delivery | At most one batch; a normal stop drains | A provider idempotency key in a real client, with the message's dates frozen after an ambiguous failure so a retry is recognisable |
| Database connections | Each process opens up to pool size + overflow (15 by default); 3 API + 3 worker processes would use 90 of PostgreSQL's default 100, before ingestion and Agrobot. A request waiting for a lock (a PATCH or DELETE on an alert being sent or evaluated) holds a pooled connection for up to 5 seconds | Pool size is configurable; the demo runs 1 + 1; `lock_timeout` bounds a wait to 5 seconds | A connection pooler (PgBouncer), or smaller pools, before adding replicas |
| WhatsApp rules for business messages | A message sent more than 24 hours after the user last wrote must use a template approved in advance; providers also limit sending rates | The message has a fixed shape (event, field, threshold, list of dates) that fits a parameterised template | Register the template; respect the provider's rate limits in the real client |
| Tables grow without limit | Notifications grow by about one row per alert per qualifying date; forecasts by zones × 4 types per day; the first workload to slow down is the purge of old forecasts, which clears the link on notifications | Indexes serve the hot queries, including `notifications(forecast_id)` for the purge | Retention and partitioning (Non-Goals) |
| The forecast purge and dispatch wait on each other | The purge clears the forecast link on a past-date notification that a dispatcher holds, or the dispatcher's claim waits on a purge in progress | Both waits last at most one send timeout (10 seconds) or one short purge transaction; lock timeouts fail either side instead of hanging | Purge only forecasts older than the window and not linked to pending notifications |
| The identity header can be spoofed | As soon as the API is reachable from a network | API published on `127.0.0.1` only; documented shortcut | Real authentication replaces one FastAPI dependency |
| Synchronous log writes on the event loop | Under heavy logging or a slow log sink | Logs go to standard output; one line per event | `QueueHandler` to write logs off the loop |
| Coverage is an estimate | Fields with a small share in a zone, near the 10% minimum | The minimum affected area filters slivers | Provider cell borders, if a provider publishes them |
| Offset pagination | Deep pages of a very long notification list | Limit of 100 per page | Keyset pagination |
| One timezone for every field | Fields outside Argentina's timezone | Configurable timezone | A per-field timezone (Non-Goals) |
| A new alert waits for the next evaluation | Up to 5 minutes before its first notification is created | Interval is configurable | Evaluate a new alert immediately in the API |

The items in the "Next step" column are listed in
[Future improvements](#future-improvements).

--------------------------------------------------------------------------------

## Detailed Implementation Plan

### Configuration

All settings are environment variables with defaults, validated at startup
(numeric values must be positive; the timezone must exist; the minimum affected
area is a decimal greater than 0 and at most 1).

| Setting | Default |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://agrobot:agrobot@localhost:5432/agrobot` |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | 5 / 10 |
| `FORECAST_TIMEZONE` | `America/Argentina/Buenos_Aires` |
| `FORECAST_MAX_AGE_HOURS` | 24 |
| `EVALUATION_INTERVAL_SECONDS` | 300 |
| `EVALUATION_HORIZON_DAYS` | 7 |
| `ALERT_MIN_AFFECTED_AREA` | 0.10 |
| `DISPATCH_INTERVAL_SECONDS` | 15 |
| `DISPATCH_BATCH_SIZE` | 20 (alerts per batch) |
| `NOTIFIER_TIMEOUT_SECONDS` | 10 |
| `NOTIFICATION_BACKOFF_BASE_SECONDS` / `NOTIFICATION_BACKOFF_MAX_SECONDS` | 60 / 1800 |
| `WORKER_HEARTBEAT_DIR` | `/tmp` |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` (`console` for local use) |

### Schema

Common to every table with a surrogate key: `id BIGINT GENERATED ALWAYS AS
IDENTITY` primary key and `created_at timestamptz NOT NULL DEFAULT now()`.
Tables whose rows change also have `updated_at timestamptz NOT NULL DEFAULT
now()`, set on every update: by the ORM for single-row writes and explicitly
in every bulk statement this system runs (the ingestion job sets its own,
Assumption 2). Constraint names follow one naming
convention (`pk_`, `fk_`, `uq_`, `ix_`, `ck_` prefixes with table and column
names). The two code-owned value sets (notification status and member role) are
stored as short strings with a CHECK listing the lowercase values.

| Table | Columns and constraints |
|---|---|
| `users` | `first_name TEXT NOT NULL`, `last_name TEXT NOT NULL`, `phone_number TEXT NOT NULL UNIQUE` (E.164) |
| `fields` | `name TEXT NOT NULL`; `updated_at` |
| `field_members` | `user_id` → `users` ON DELETE CASCADE; `field_id` → `fields` ON DELETE CASCADE; PK `(user_id, field_id)`; `role` CHECK in (`owner`, `agronomist`, `manager`) |
| `zones` | `latitude NUMERIC(7,4) NOT NULL` CHECK −90..90; `longitude NUMERIC(7,4) NOT NULL` CHECK −180..180; UNIQUE `(latitude, longitude)` |
| `field_zones` | `field_id` → `fields` ON DELETE CASCADE; `zone_id` → `zones` ON DELETE CASCADE; PK `(field_id, zone_id)`; index on `zone_id`; `coverage NUMERIC(5,4) NOT NULL` CHECK `> 0 AND <= 1` |
| `event_types` | `code VARCHAR(32) NOT NULL UNIQUE`; `name TEXT NOT NULL`; rows `frost`/helada, `rain`/lluvia, `strong_wind`/viento fuerte, `heat`/calor extremo inserted by the migration |
| `weather_forecasts` | `zone_id` → `zones` ON DELETE CASCADE; `event_type_id` → `event_types` ON DELETE RESTRICT; `forecast_date DATE NOT NULL`; `probability SMALLINT NOT NULL` CHECK 0..100; `updated_at`; UNIQUE `(zone_id, event_type_id, forecast_date)` (upsert key and evaluation index) |
| `alerts` | `user_id` → `users` ON DELETE CASCADE; `field_id` → `fields` ON DELETE CASCADE (implied by the composite FK; declared so the ORM has plain many-to-one paths to the user and the field); composite FK `(user_id, field_id)` → `field_members` ON DELETE CASCADE; `event_type_id` → `event_types` ON DELETE RESTRICT; `threshold SMALLINT NOT NULL` CHECK 1..100; `is_active BOOLEAN NOT NULL DEFAULT true`; `updated_at`; UNIQUE `(user_id, field_id, event_type_id)`; index on `field_id` |
| `notifications` | `alert_id BIGINT NOT NULL` → `alerts` ON DELETE CASCADE; `forecast_id BIGINT NULL` → `weather_forecasts` ON DELETE SET NULL; `forecast_date DATE NOT NULL`; `threshold SMALLINT NOT NULL` CHECK 1..100; `probability SMALLINT NOT NULL` CHECK 0..100; `affected_area NUMERIC(5,4) NOT NULL` CHECK `> 0 AND <= 1`; `status` CHECK in (`pending`, `sent`, `failed`) DEFAULT `pending`; `attempts SMALLINT NOT NULL DEFAULT 0` CHECK `>= 0`; `next_attempt_at timestamptz NOT NULL DEFAULT now()`; `last_error TEXT NULL`; `sent_at timestamptz NULL`; `updated_at`; UNIQUE `(alert_id, forecast_date)`; CHECK `status <> 'sent' OR sent_at IS NOT NULL`; partial index on `next_attempt_at` WHERE `status = 'pending'`; partial index on `forecast_id` WHERE `forecast_id IS NOT NULL` (serves the `SET NULL` when the ingestion job purges old forecasts) |

Every default on `notifications` is a database-side default, because its rows
are created by an `INSERT … SELECT`.

### Key SQL statements

**Evaluation** (parameters: `today`, `end_date = today + horizon`, computed in
the application, and the minimum affected area):

```sql
WITH matches AS (
  SELECT a.id AS alert_id, a.threshold, wf.id AS forecast_id, wf.forecast_date,
         wf.probability, fz.coverage
  FROM alerts a
  JOIN field_zones fz ON fz.field_id = a.field_id
  JOIN weather_forecasts wf
    ON wf.zone_id = fz.zone_id AND wf.event_type_id = a.event_type_id
  WHERE a.is_active
    AND wf.forecast_date BETWEEN :today AND :end_date
    AND wf.probability >= a.threshold
    AND NOT EXISTS (SELECT 1 FROM notifications x
                    WHERE x.alert_id = a.id AND x.forecast_date = wf.forecast_date)
), ranked AS (
  SELECT m.*,
         LEAST(SUM(coverage) OVER (PARTITION BY alert_id, forecast_date), 1) AS affected_area,
         ROW_NUMBER() OVER (PARTITION BY alert_id, forecast_date
                            ORDER BY probability DESC, forecast_id) AS rn
  FROM matches m
)
INSERT INTO notifications (alert_id, forecast_id, forecast_date, threshold, probability,
                           affected_area, next_attempt_at)
SELECT alert_id, forecast_id, forecast_date, threshold, probability, affected_area,
       COALESCE((SELECT max(p.next_attempt_at) FROM notifications p
                 WHERE p.alert_id = ranked.alert_id AND p.status = 'pending'), now())
FROM ranked
WHERE rn = 1 AND affected_area >= :min_affected_area
ORDER BY alert_id, forecast_date
ON CONFLICT (alert_id, forecast_date) DO NOTHING
RETURNING id;
```

**Dispatcher claim** (parameters: `now` in UTC and the batch size, counted in
alerts). An alert is due when **all** its pending notifications are due.
First lock due alerts, starting from the partial index on pending
notifications; then take their pending notifications, re-checking on a fresh
snapshot that the alert is still due (another dispatcher may have recorded a
failure in between):

```sql
SELECT a.id
FROM alerts a
JOIN (SELECT alert_id, min(next_attempt_at) AS oldest, max(attempts) AS tries
      FROM notifications
      WHERE status = 'pending'
      GROUP BY alert_id
      HAVING max(next_attempt_at) <= :now) due ON due.alert_id = a.id
ORDER BY (due.tries > 0), due.oldest, a.id
LIMIT :batch_size
FOR NO KEY UPDATE OF a SKIP LOCKED;

SELECT notifications.*
FROM notifications
WHERE status = 'pending'
  AND alert_id IN (SELECT alert_id FROM notifications
                   WHERE status = 'pending' AND alert_id = ANY(:locked_alert_ids)
                   GROUP BY alert_id
                   HAVING max(next_attempt_at) <= :now)
ORDER BY alert_id, forecast_date
FOR UPDATE OF notifications;
```

Alerts with no rows in the second result are released when the batch ends.
The second statement can wait on a row only while the ingestion job purges an
old forecast linked to it (the link is cleared), which is why the purge runs
in short transactions.

The claimed rows' alerts, with their users, fields and event types, are loaded
by separate queries after the claim, because PostgreSQL rejects `FOR UPDATE`
on the nullable side of an outer join.

### Evaluation and delivery (details)

*   **Duplicates: the advisory lock is for efficiency; the unique key on
    (alert, forecast date) is for correctness.** Evaluation runs inside a
    transaction-scoped advisory lock (`pg_try_advisory_xact_lock`); a replica
    that cannot take it skips the run and logs INFO `evaluation_skipped`. The
    unique key prevents duplicates even if the lock is bypassed (for example
    SQL run by hand).
*   Implementation notes on the statement: it leaves out every (alert, date)
    pair it can already see a notification for (`NOT EXISTS`, a plain read
    that never waits on a dispatch batch holding those rows), and lets
    `ON CONFLICT DO NOTHING` catch the rest; it inserts in a fixed order
    (`ORDER BY alert_id, forecast_date`) so that two runs, if the lock is ever
    bypassed, wait for each other instead of deadlocking.
*   The evaluation statement gives a new notification the next-attempt time of
    its alert's existing pending notifications, if there are any, and `now()`
    otherwise.
*   The evaluation transaction sets `statement_timeout = 240s` (overrides the
    30-second connection default; below the 5-minute interval). If the
    statement fails with a foreign-key violation (an alert was deleted while
    it ran), it is retried once and logged at INFO; a second failure is an
    error. On a stop signal an evaluation in progress is cancelled; it is safe
    to repeat.
*   Evaluation reads `now()` from the database once per run, and dispatch once
    per batch (a dispatch run can last minutes during an outage); "today" is
    derived from it in the forecast timezone, so replicas with different
    clocks agree. Tests pass explicit values.
*   First attempts are claimed before retries (`ORDER BY (tries > 0), …` in
    the claim), so a new warning does not wait behind an outage's backlog of
    retries.
*   The re-check reuses the evaluation's matching rule: one shared query
    builder for the join of alerts, zones and forecasts, the threshold, the
    window and the affected-area rule, restricted to the claimed alerts and
    dates. The "already notified" filter (`NOT EXISTS`) and the insert belong
    to evaluation only; with that filter the re-check would find every claimed
    date "no longer qualifying" and delete it.
*   A group's attempt count is the highest among its rows; after a failed send
    every row of the group gets that count plus one and the same next attempt
    time, so the group stays together.
*   Backoff with full jitter: the delay after attempt *n* is a random value
    between 0 and `min(NOTIFICATION_BACKOFF_MAX_SECONDS,
    NOTIFICATION_BACKOFF_BASE_SECONDS × 2^min(n−1, 30))`. The exponent is
    capped because retries have no attempt limit.
*   `last_error` stores only the error type and code reported by the notifier
    port (truncated to 500 characters), never the provider's raw text, which
    can contain the recipient's phone number; the warning log carries the same
    fields.
*   Every database connection sets `lock_timeout = 5s` and
    `statement_timeout = 30s`, so a stuck lock fails a batch instead of
    holding it. Worker connections also set
    `idle_in_transaction_session_timeout = 30s` and TCP keepalives (idle
    10 s, interval 5 s, count 3), so PostgreSQL ends the transaction of a
    worker that died without closing its connection about 30 seconds after
    the last activity plus up to 25 seconds of keepalive detection, and frees
    its alerts. Estimated (not measured) duration of one batch: claim and
    re-check a few milliseconds, one send of at most 10 seconds, record a few
    milliseconds; the 30-second stop grace period covers it.
*   A PATCH or DELETE of an alert that a dispatcher holds waits up to 5 seconds
    for the lock; if the batch is still sending, the API answers **503** with
    `Retry-After: 10` (the alert is busy being sent; a retry after the batch
    is saved is no longer blocked by it). A DELETE can also wait on a running
    evaluation, which holds a key-share lock on the alerts it is inserting
    notifications for (estimated at about a second for 10,000 alerts). A
    waiting request holds a pooled connection while it waits (see
    [Risks and Mitigations](#risks-and-mitigations)).
*   The alert lock is `FOR NO KEY UPDATE`: it does not block evaluation's
    inserts of new notifications for that alert (which only need the alert to
    exist). A row that evaluation commits during a send keeps the old retry
    time, so the alert is not due again until every pending row is due (the
    rule above): no early retry. A success marks only the claimed rows sent;
    a row inserted during the send waits for the alert's next message.
*   Shutdown signals (the drain behaviour is described under
    [Delivery (overview)](#delivery-overview)): SIGTERM or SIGINT sets a stop
    flag that the loops check between batches and ticks; the dispatch batch in
    flight is not cancelled, an evaluation in progress is. A second signal
    cancels the running tasks. A cancelled send
    (`asyncio.CancelledError` among the results) is re-raised: the transaction
    rolls back and every claimed row is left unchanged; a cancelled send is
    never counted as a failed attempt. A drained batch takes at most one send
    timeout (10 seconds), below the 30-second stop grace period.
*   Exactly-once delivery to an external channel is not possible from this
    side alone. A provider idempotency key built from the notification ids
    would let a provider drop a repeated message only when the retry carries
    the same dates; a retry can carry a different set (a new date joined, or
    one stopped qualifying), and no provider is chosen yet, so whether it
    accepts such a key is still to be confirmed.
*   Log levels follow consequences: a failed send is a WARNING (a retry
    follows); a notification that expires undelivered is an ERROR, logged once
    per message. Phone numbers never appear in logs; notifications are
    identified by their ids and the user by id.

### Concurrency model (details)

1.  Every route, dependency and job is asynchronous and only awaits
    asynchronous I/O, apart from the logging exception in rule 2. The design
    does not rely on the web framework running synchronous handlers in a
    thread pool.
2.  No blocking I/O on the event loop: inside asynchronous code there is no
    sleeping, no synchronous HTTP client, no synchronous database driver, no
    synchronous file or subprocess I/O in the application. Ruff's `ASYNC`
    rules catch the common blocking calls in CI and code review is the check
    for the rest, because one blocking call stalls every request and job on
    that process. The one accepted exception: log records are written
    synchronously to standard output by the standard library. Each write is a
    short local write, not a wait on the network; moving it off the loop
    (`QueueHandler`) is listed under
    [Future improvements](#future-improvements).
3.  No Python loop over alerts or forecasts; Python handles at most one batch
    at a time.
4.  If a blocking library is ever needed (for example a WhatsApp SDK without an
    asynchronous client), it goes behind the notifier port and runs in a
    thread (`asyncio.to_thread`). Threads suit blocking I/O because the GIL is
    released while a thread waits.
5.  If CPU-bound Python is ever needed, it goes to another process, never to
    threads: the GIL lets one thread execute Python at a time, so CPU-bound
    threads do not run Python in parallel.
6.  One database session per request or task, never shared between concurrent
    tasks.
7.  Each process has its own interpreter, GIL, event loop and connection pool.
    Total database connections are processes × (pool size + overflow) and must
    stay below the server's limit.

The free-threaded CPython build is not used: nothing here is CPU-bound in
Python, and the interpreter turns the GIL back on for C extensions that do
not declare support for it.

### API (details)

Prefix `/api/v1` except health. Identity header `X-User-Id`: missing,
non-integer or unknown → 401. Other users' resources → 404. Errors use the
`{"detail": …}` shape. Real authentication later replaces one FastAPI
dependency (the function that resolves the current user).

| Method and path | Body / query | Success | Errors |
|---|---|---|---|
| `GET /health/live` | | 200 `{"status":"ok"}` | |
| `GET /health/ready` | | 200 after `SELECT 1` | 503 if the database is unreachable |
| `GET /fields` | `limit`, `offset` | 200 page of fields (caller is a member), each with `id`, `name` and `role` | 401 |
| `GET /fields/{id}/outlook` | | 200 the field's zones (`id`, `latitude`, `longitude`, `coverage`), forecasts per event type, zone and date for the window, and for each of the caller's alerts on the field the dates on which it fires with their affected area and probability, computed by the shared matching query builder (used by the demo page) | 401, 404 |
| `GET /event-types` | | 200 list of `{code, name}` | 401 |
| `POST /alerts` | `{field_id, event_type, threshold, is_active?}`; `event_type` is a type code | 201 alert + `Location` header | 401; 404 field missing or caller not a member (also when the membership is removed between the check and the insert: the composite foreign key's violation maps to 404); 409 duplicate for this user; 422 invalid input or unknown event type |
| `GET /alerts` | `field_id?`, `event_type?`, `is_active?`, `limit`, `offset` | 200 page of alerts | 401; 422 (an unknown `event_type` filter is 422, not an empty page) |
| `GET /alerts/{id}` | | 200 alert | 401, 404 |
| `PATCH /alerts/{id}` | `{threshold?, is_active?}`; unknown keys rejected; at least one key; explicit `null` rejected | 200 alert | 401, 404, 422; 503 + `Retry-After: 10` while the alert is being sent |
| `DELETE /alerts/{id}` | | 204 | 401, 404; 503 + `Retry-After: 10` while the alert is being sent |
| `GET /notifications` | `status?`, `field_id?`, `alert_id?`, `limit`, `offset` | 200 page of notifications, newest first (`id DESC`) | 401, 422 |

A page is `{items, limit, offset}` with `limit` 1..100 (default 50) and
`offset` ≥ 0. A 503 means the alert is locked: a dispatcher is sending it, or,
for a DELETE, a running evaluation is inserting notifications for it (see
[Evaluation and delivery (details)](#evaluation-and-delivery-details)); a
retry succeeds once that work is saved. An alert is returned as `id`,
`field_id`, `event_type` (code), `threshold`, `is_active`, `zones_mapped`
(false while Agrobot has not yet linked the field to any zone: the alert is
accepted but cannot fire until it does), `created_at`, `updated_at`. A
notification is returned as `id`, `alert_id`, `forecast_id` (null once the
forecast is purged), `field_id`, `event_type`, `forecast_date`, `threshold`,
`probability`, `affected_area`, `status`, `sent_at`, `created_at`.

### Worker jobs

*   **Periodic runner**: runs a job, logs its duration, logs any exception as
    an error with its traceback and keeps going, then waits for the rest of the
    interval or until shutdown is requested. Runs of one job never overlap.
    After each successful evaluation run (or skipped run) and after each
    dispatch batch, including a run that finds nothing to send (not only at
    the end of a run, which can last minutes
    during an outage), it writes the current time to that job's heartbeat
    file in `WORKER_HEARTBEAT_DIR` (default `/tmp`): `evaluation.heartbeat`
    and `dispatch.heartbeat`. The file is written with `asyncio.to_thread`, so
    it does not block the event loop (concurrency rule 4). The container's
    health check, which an orchestrator may use to restart the worker, looks
    only at `dispatch.heartbeat` (older than three dispatch intervals means
    unhealthy). Evaluation failures do not restart the sender: they alert
    through the `job_failed` log, and a stale `evaluation.heartbeat` is an
    alerting signal.
*   **Data checks** (each evaluation run): a WARNING `alerts_without_zones`
    with the count of active alerts whose field has no zones, and of fields
    whose coverages do not add up to 100%; a WARNING `forecasts_stale` with
    the count of zones linked to active alerts that have no forecast for some
    date in the window, or whose forecasts are older than
    `FORECAST_MAX_AGE_HOURS` (checked per zone, so a job that skips some zones
    is noticed, not only one that stops).
    The container command is exec form, so SIGTERM reaches Python.
*   **Evaluation**: one transaction running the evaluation statement; logs
    `evaluation_completed` with the number created and the duration.
*   **Dispatch**: one transaction per batch; repeats while a batch claims as
    many alerts as the batch size and no shutdown was requested; logs
    `dispatch_completed` with counts when anything was processed;
    `notification_send_failed` (warning) and `notification_expired` (error, a
    date passed undelivered).
*   **Entry point**: `python -m app.worker` runs the two loops until SIGTERM
    or SIGINT; `--once` runs evaluation once and then dispatch until a batch is
    not full, and exits non-zero if either failed. `--once` takes the
    evaluation lock too: if another replica holds it, it skips the evaluation
    (logging `evaluation_skipped`), still runs dispatch, and that skip alone
    exits 0.

### Message format

Spanish, dates ascending as `dd/mm`, the alert's threshold (equal on every
row of a group, since the re-check refreshes it), the share of the field only
when its rounded percentage is below 100:

```text
⚠️ Alerta de helada en "La Esperanza" (umbral 70%): 15/11 85% (60% del campo), 16/11 75%, 18/11 71% (40% del campo).
```

This is the worked example in [Alert semantics](#alert-semantics); a field in
a single zone shows no share, as in CUJ 1.

### Target Files to Create/Modify

Some files already exist from the repository's first commit (the bootstrap:
Python tooling, lint and type-check configuration, a Compose file with only the
database, and a Makefile). They are marked Modify; every other file, including
the `Dockerfile` and the CI workflow, is created.

#### 1. `pyproject.toml`, `uv.lock`, `.python-version` (Modify)

*   **Type / Scope**: build configuration.
*   **Proposed Changes**: Python 3.13; runtime dependencies FastAPI, Uvicorn,
    SQLAlchemy (asyncio), asyncpg, Alembic, pydantic-settings; development
    dependencies pytest, pytest-asyncio, pytest-cov, pytest-timeout, httpx,
    Ruff, mypy; Ruff rules including the asynchronous-blocking and timezone
    rules; mypy strict for the application.

#### 2. `app/config.py` (Create)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: one settings object with the configuration table
    above, read from the environment and validated at startup.

#### 3. `app/logging.py` (Create)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: logging setup with a JSON formatter that emits the
    static message plus structured fields, or a console format for local use.

#### 4. `app/db.py` (Create)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: builders for the async engine and the session
    factory (objects stay usable after commit).

#### 5. `app/enums.py` (Create)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: notification status and field role value sets.

#### 6. `app/models.py` (Create)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: declarative base with the constraint naming
    convention and the nine tables of the schema.

#### 7. `app/schemas.py` (Create)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: request and response models and the page wrapper.

#### 8. `app/errors.py` (Create)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: domain errors (not found, alert already exists,
    unknown event type) and the handlers that map them to 404, 409 and 422; a
    catch-all handler that logs unexpected errors once and returns 500 without
    internals.

#### 9. `app/main.py` (Create)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: application factory that builds the engine and
    session factory, registers routers and error handlers, and disposes the
    engine on shutdown.

#### 10. `app/api/deps.py` (Create)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: session dependency (rolls back on error), current-user
    dependency (from the identity header), pagination parameters.

#### 11. `app/api/routes/health.py`, `fields.py`, `event_types.py`, `alerts.py`, `notifications.py` (Create)

*   **Type / Scope**: Python modules.
*   **Proposed Changes**: the endpoints of the
    [API (details)](#api-details) table; routes commit after the service
    returns.

#### 12. `app/services/alerts.py` (Create)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: alert create, read, list, change and delete, scoped to
    the caller; membership checked before create; duplicates detected through
    the database's uniqueness rule.

#### 13. `app/services/evaluation.py` (Create)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: the evaluation statement, returning the number of
    notifications created; never commits.

#### 14. `app/notifiers.py` (Create)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: outbound message type, the notifier port, its error
    type, and the logging notifier.

#### 15. `app/services/dispatch.py` (Create)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: one dispatch batch (claim, expire, re-check, group,
    send, record), the backoff function with full jitter, and the message
    renderer.

#### 16. `app/worker.py` (Create)

*   **Type / Scope**: Python module and entry point.
*   **Proposed Changes**: the periodic runner, the two jobs, signal handling
    and the single-cycle mode.

#### 17. `app/seed.py` (Create)

*   **Type / Scope**: Python entry point.
*   **Proposed Changes**: idempotent demo data standing in for what Agrobot
    already has, one field per case: "La Esperanza" (one zone; two cold dates
    give one message), "Los Álamos" (60% / 40% across two zones; fires with
    "40% del campo" when only one zone is cold), "El Rincón" (95% / 5%; rain in
    the 5% sliver does not fire), "La Compartida" (shared by two users with
    thresholds of 60% and 80% on a 70% forecast; only the first is notified).
    Two users with roles, zones and coverages that add up to 100% per field,
    the alerts of the last three fields, and forecasts for every zone, event
    type and date: fixed values for the cases above and seeded random values
    elsewhere, kept below the watched thresholds.

#### 18. `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_agrobot_stand_ins.py`, `alembic/versions/0002_weather_alerts.py` (Create)

*   **Type / Scope**: database migrations.
*   **Proposed Changes**: asynchronous migration environment reading the
    database URL from the settings, and two reversible revisions:
    `0001_agrobot_stand_ins` creates the tables that stand in for Agrobot's
    (`users`, `fields`, `field_members`, `zones`, `field_zones`,
    `weather_forecasts`), for the demo and tests only; `0002_weather_alerts`
    creates this feature's own tables (`event_types` with its four rows,
    `alerts`, `notifications`). Here `0002` follows `0001` in one linear
    chain. In a real Agrobot deployment `0001` would not exist: `0002` would
    be re-parented onto Agrobot's own latest migration, with its foreign
    keys pointing at Agrobot's real tables, and rolling it back drops only
    the feature's tables.

#### 19. `tests/conftest.py`, `tests/factories.py`, `tests/test_*.py` (Create)

*   **Type / Scope**: unit, integration, API and end-to-end tests.
*   **Proposed Changes**: tests against a real PostgreSQL database whose schema
    is created by the migration and whose tables are emptied after each test;
    small factories for every entity; tests for configuration, constraints,
    the seed, every API endpoint and error, evaluation (thresholds, window,
    areas, idempotency, concurrent runs), dispatch (grouping, retries, expiry,
    deletion of dates that stopped qualifying, concurrent dispatchers), the
    worker loops, and one end-to-end path from a new alert to a sent
    notification.

#### 20. `Dockerfile` (Create); `docker-compose.yml`, `docker/initdb-test.sql`, `.env.example` (Modify)

*   **Type / Scope**: deployment.
*   **Proposed Changes**: a multi-stage image running as a non-root user; a
    compose stack of database, one-shot migration, one-shot seed, API with a
    readiness health check, and worker with a 30-second stop grace period; a
    second database for tests.

#### 21. `.github/workflows/ci.yml` (Create); `Makefile` (Modify)

*   **Type / Scope**: developer tooling and CI.
*   **Proposed Changes**: lint, format check, type check, migration up/down/up,
    and tests with the coverage gate, locally and in CI.

#### 22. `README.md` (Create)

*   **Type / Scope**: documentation (Spanish).
*   **Proposed Changes**: how to run it, architecture, data model, alert
    semantics, concurrency model, decisions and trade-offs, assumptions,
    production readiness, what is missing, how it was built, project layout.

#### 23. `app/static/demo/` and `app/api/routes/demo.py` (Create)

*   **Type / Scope**: demo page (Extra).
*   **Proposed Changes**: a static Spanish page served at `/demo` (no build
    step), an illustration file with the seeded fields' outlines, and a
    read-only endpoint `GET /api/v1/fields/{id}/outlook` (caller must be a
    member) returning the field's zones with coverage, the forecasts per
    event type, zone and date for the window, and the dates on which each of
    the caller's alerts fires (from the shared matching query builder). The
    page renders server results only: no alert rule or message text in
    JavaScript; the chat lists `GET /notifications`. Layout and visuals
    follow the agreed mock, [demo-mock.html](demo-mock.html) (version 3, with
    sample data; its client-side rule is for the mock only). It is built
    after the README.

### Verification Plan

*   **Compilation/Build**: `docker compose up -d --build` from a clean state
    (`docker compose down -v`).
*   **Unit Tests**: `uv run pytest`.
*   **Static Analysis & Linting**: `ruff check`, `ruff format --check`, `mypy`.
*   **Test Coverage Requirement**: at least 90% line coverage of `app`.
*   **E2E/Integration**: the migration upgraded, downgraded and upgraded again;
    the README walkthrough (create an alert with the seeded data, wait for the
    worker, list notifications) ends with a notification marked sent and one
    grouped delivery in the worker log; `docker compose stop worker` exits
    cleanly within the 30-second stop grace period.

--------------------------------------------------------------------------------

## Future improvements

This is the complete backlog: every item this document calls a next step.

*   **Delivery**: a real WhatsApp client with permanent vs transient errors and
    a provider idempotency key, with a message's dates frozen after an
    ambiguous failure (a timeout) so the retry is the same message; registering the
    message template and respecting the provider's rate limits; re-queue of
    failed notifications; lease-based claiming for slow providers
    (Alternative T); storing the retry schedule once per alert
    (Alternative W); a circuit breaker for channel-wide outages; one digest per
    user per cycle instead of one message per alert, for users with many
    alerts.
*   **Observability**: metrics on failed notifications and job lag; tracing;
    writing logs off the event loop (`QueueHandler`); a readiness signal on
    forecast age.
*   **Agrobot integration**: a field-mapping check in Agrobot (every field
    has zones whose coverages add up to 100%); confirming the ingestion job's
    write pattern with the Agrobot team, with a test against its upsert.
*   **Alert rules**: re-notification rules (re-arming when a probability drops
    and rises again, escalation and "all clear"); per-alert minimum affected
    area and horizon; notifying other members; hail alerts.
*   **Data**: keeping notification history after an alert is deleted; storing
    the rendered message text for audit; retention and partitioning for the
    notification and forecast tables; a per-field timezone; a real
    weather-provider adapter; computing the field-to-zone mapping here if
    Agrobot does not provide it (see
    [Appendix: computing the field-to-zone mapping](#appendix-computing-the-field-to-zone-mapping));
    provider cell borders for coverage, if a provider publishes them; PostGIS
    when a spatial query appears.
*   **API and security**: real authentication, which replaces one FastAPI
    dependency; PostgreSQL row-level security as a second layer under the
    ownership checks; keyset pagination and total counts; rate limiting.
*   **Scale**: evaluate only alerts whose forecasts changed, or in chunks of
    alerts; a connection pooler (PgBouncer), or smaller pools, before adding
    replicas.
*   **Unmapped fields**: when an alert is created on a field Agrobot has not
    yet linked to any zone, ask Agrobot to map that field right away, if it
    offers such a request, instead of waiting for its own mapping job.
*   **Latency of a new alert**: a new alert is first evaluated by the next
    periodic run, up to 5 minutes later; the API could evaluate it immediately
    with the same statement restricted to that alert.

--------------------------------------------------------------------------------

## Appendix: computing the field-to-zone mapping

This design reads the field-to-zone mapping; it does not compute it. If Agrobot
does not already have it, it is computed once per field, and again when the
field's outline changes, as follows:

1.  **Learn the grid, do not ask point by point.** Forecast grids are regular:
    once the provider's grid (cell size and origin) is known, the cell that
    contains a point is plain arithmetic. The provider is asked at most once
    per distinct cell, to confirm the cell's centre (the zone's identity),
    never once per sample point. A field needs one call per distinct zone.
2.  **Estimate coverage by sampling.** Place a regular lattice of points inside
    the outline, finer than the grid; a cell's coverage is the share of points
    that fall in it, rounded so that a field's coverages add up to exactly
    100%.
3.  **Do not hold locks while waiting on the provider.** Read the outline, call
    the provider outside any database transaction, then write the field's
    zones and coverages in one short transaction, only if the outline has not
    changed meanwhile (compared with the one read).

The coverage is an estimate, because providers report a zone's centre, not its
borders.

--------------------------------------------------------------------------------

## Additional resources

*   [Real Python: What Is the Python Global Interpreter Lock (GIL)?](https://realpython.com/python-gil/)
*   [google/elephant-goldfish-model](https://github.com/google/elephant-goldfish-model):
    the spec-driven workflow used to write and review this document (layout,
    fresh-context reviews, drift check against the code).
*   API documentation of the providers checked for the grid contract
    (Assumption 1):
    [Open-Meteo](https://open-meteo.com/en/docs),
    [OpenWeather](https://openweathermap.org/api) and
    [US National Weather Service](https://www.weather.gov/documentation/services-web-api).
