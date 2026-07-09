# CoWork API — Bug Report

**Challenge:** ICT Fest Hackathon (Preliminary) — API Bug Fixing
**Codebase:** CoWork (FastAPI + SQLAlchemy + SQLite, JWT auth)
**Baseline commit:** `5bb6f56` (*Initial commit*) — all line numbers below refer to this baseline.

This report documents **23 bugs** found and fixed across the codebase. For each bug it
gives the location, the business rule it violated, the root cause and observable
incorrect behavior, the fix (before/after), and a concrete test case with the
before/after result.

Every fix preserves the API contract exactly (paths, status codes, error `code`
strings, JSON field names). No features were added and no unrelated code was
refactored.

---

## 1. Methodology

Each bug was handled with a strict **test-before → fix → test-after** loop, and every
fix was re-checked against all previously written tests to guard against regressions.

Three complementary testing techniques were used:

1. **Direct function tests** — for pure logic (datetime parsing, token claims, refund
   rounding), calling the functions directly and asserting outputs.
2. **`TestClient` API tests** — for request/response behavior, status codes, error
   codes, pagination, tenancy, and caching, exercised through the real FastAPI stack.
3. **Real-server concurrency tests** — for the Hard-tier races. A `uvicorn` server is
   booted against a throwaway SQLite database and hit with **simultaneous** HTTP
   requests (synchronized by a `threading.Barrier`) so the deliberately planted
   `time.sleep()` race windows are actually hit. Service-level races were additionally
   reproduced by driving the service modules directly from many threads.

The deliberate `time.sleep()` calls scattered through the services
(`_pricing_warmup`, `_quota_audit`, `_settlement_pause`, `_aggregate_pause`,
`_settle_pause`, `_format_pause`) are not bugs themselves — they widen the race
windows so concurrency bugs are reproducible. The fixes keep behavior correct **with
those sleeps in place**.

### Test result summary (before → after)

| Suite | Focus | Before | After |
|-------|-------|:------:|:-----:|
| `test_foundational` | UTC parsing, token expiry, logout | 4/8 | **8/8** |
| `test_authflow` | refresh single-use, register 409 | 6/10 | **10/10** |
| `test_booking_validation` | window, duration, overlap, paging, detail, visibility | 14/24 | **24/24** |
| `test_refunds` | refund tiers + rounding + ledger parity | 30/36 | **36/36** |
| `test_caching` | report/availability invalidation | 7/10 | **10/10** |
| `test_export` | cross-org export isolation | 6/8 | **8/8** |
| `test_conc_services` | reference / rate-limit / stats races | 1/8 | **8/8** |
| `test_conc_bookings` | double-booking / quota / duplicate-refund (HTTP) | 1/8 | **9/9** |
| `test_conc_notifications` | notification deadlock | 1/4 | **4/4** |
| `test_hardening` | malformed-datetime 500, availability cache key | 6/12 | **16/16** |

Concurrency suites were run repeatedly (3–4×) to confirm stability.

---

## 2. Summary of all bugs

| # | Difficulty | File | Location | Rule | One-line summary |
|---|:----------:|------|----------|:----:|------------------|
| 1 | Easy | `app/auth.py` | L50 | 8 | Access token lived 900 **minutes** instead of 900 seconds |
| 2 | Easy | `app/auth.py` | L97 | 8 | Logout compared `sub` against a set of `jti` → never invalidated |
| 3 | Medium | `app/routers/auth.py` | L81–93 | 8 | Refresh tokens were not single-use / never invalidated |
| 4 | Easy | `app/timeutils.py` | L12–13 | 1 | Offset-aware datetimes stripped, not converted to UTC |
| 5 | Easy | `app/routers/auth.py` | L37–43 | 15 | Duplicate username returned the existing user instead of 409 |
| 6 | Easy | `app/routers/bookings.py` | L86 | 2 | 300-second past-start grace window |
| 7 | Easy | `app/routers/bookings.py` | L89–94 | 2 | No minimum-duration / `end > start` validation |
| 8 | Easy | `app/routers/bookings.py` | L50 | 3 | Overlap used `<=` → back-to-back wrongly conflicted |
| 9 | Easy | `app/routers/bookings.py` | L137–139 | 11 | Pagination: wrong sort, wrong offset, hardcoded page size |
| 10 | Easy | `app/routers/bookings.py` | L166 | 5§ | Booking detail overwrote `start_time` with `created_at` |
| 11 | Easy | `app/routers/bookings.py` | L150–175 | 10 | Member could read another member's booking |
| 12 | Medium | `app/routers/bookings.py`, `app/services/refunds.py` | L201–208, L14–27 | 6 | Wrong refund tiers + truncating/banker's rounding; response ≠ ledger |
| 13 | Medium | `app/routers/bookings.py` | L121, L217 | 12,13 | Stale caches: create→report, cancel→availability |
| 14 | Medium | `app/services/export.py` | L48–52 | 9 | `include_all`+`room_id` leaked other orgs' bookings |
| 15 | Hard | `app/services/reference.py` | L17–21 | 7 | Reference-code counter race → duplicate codes |
| 16 | Hard | `app/services/ratelimit.py` | L18–26 | 5 | Rate-limit bucket race → limit not enforced |
| 17 | Hard | `app/services/stats.py` | L15–26 | 14 | Stats read-modify-write race → lost updates |
| 18 | Hard | `app/routers/bookings.py` | L100 | 3 | Double-booking race → two overlapping confirmed bookings |
| 19 | Hard | `app/routers/bookings.py` | L103 | 4 | Quota race → member exceeded 3 bookings |
| 20 | Hard | `app/routers/bookings.py` | L195–214 | 6 | Concurrent cancels → multiple RefundLog rows |
| 21 | Hard | `app/services/notifications.py` | L24–35 | 16 | Inverse lock ordering → deadlock hangs the service |
| 22 | Easy | `app/routers/bookings.py` | L82–83 | Errors | Malformed datetime input crashed with 500 instead of 400 |
| 23 | Medium | `app/routers/rooms.py` | L69–99 | 13 | Availability cache keyed by raw date string → deterministically stale |

*§ Rule reference for Bug 10 is the response-schema contract for `GET /bookings/{id}`.*

---

## 3. Easy-tier bugs

### Bug 1 — Access token lifetime is 900 minutes, not 900 seconds
- **File/line:** `app/auth.py:50` (`create_access_token`)
- **Rule violated:** 8 — *access tokens expire in exactly 900 seconds.*
- **Root cause:** `ACCESS_TOKEN_EXPIRE_MINUTES` is `15`, but the lifetime multiplied it
  by 60, producing 900 **minutes** (54 000 s).
- **Impact:** tokens lived 60× too long; the required 900-second expiry was violated.

```python
# Before
lifetime = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)
# After
lifetime = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
```

- **Test case:** decode a freshly issued access token and assert `exp - iat == 900`.
  - Before: `54000`. After: `900`. (The refresh token's 7-day expiry was already
    correct and left unchanged.)

---

### Bug 2 — Logout never invalidates the token
- **File/line:** `app/auth.py:97` (`get_token_payload`)
- **Rule violated:** 8 — *logout immediately invalidates the presented access token.*
- **Root cause:** `revoke_access_token` blacklists the token's `jti`, but the guard
  compared the token's **`sub`** (user id) against that set of `jti`s, so it never
  matched.
- **Impact:** a logged-out access token continued to work.

```python
# Before
if payload.get("sub") in _revoked_tokens:
# After
if payload.get("jti") in _revoked_tokens:
```

- **Test case:** login → call an authenticated endpoint (200) → logout (200) → call it
  again with the same token.
  - Before: `200` (still valid). After: `401`.

---

### Bug 4 — Offset-aware datetimes are stripped, not converted to UTC
- **File/line:** `app/timeutils.py:11–14` (`parse_input_datetime`)
- **Rule violated:** 1 — *input carrying a UTC offset must be converted to UTC;
  naive input is treated as UTC.*
- **Root cause:** the code removed the tzinfo without shifting the clock.
- **Impact:** `12:00+05:00` was stored as `12:00` instead of `07:00Z`, corrupting
  pricing, conflict detection, quota windows, availability, and reports.

```python
# Before
if dt.tzinfo is not None:
    dt = dt.replace(tzinfo=None)
# After
if dt.tzinfo is not None:
    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
```

- **Test cases:** `12:00+05:00 → 07:00`; `12:00-03:00 → 15:00`; `12:00` (naive) → `12:00`;
  `12:00+00:00 → 12:00`.
  - Before: all offset inputs returned `12:00`. After: correct UTC values.

---

### Bug 5 — Duplicate username returns the existing user instead of 409
- **File/line:** `app/routers/auth.py:37–43` (`register`)
- **Rule violated:** 15 / Errors — *duplicate username within an org → 409
  USERNAME_TAKEN.*
- **Root cause:** the endpoint returned the existing user's details instead of raising.
- **Impact:** duplicate registrations silently "succeeded", masking the conflict.

```python
# Before
if existing is not None:
    return {"user_id": existing.id, "org_id": org.id,
            "username": existing.username, "role": existing.role}
# After
if existing is not None:
    raise AppError(409, "USERNAME_TAKEN", "Username already taken")
```

- **Test case:** register `admin1` (new org) → 201; register `member1` (same org) → 201,
  role `member`; register `admin1` again.
  - Before: `201` with existing user body. After: `409` `{"code":"USERNAME_TAKEN"}`.

---

### Bug 6 — Past-start grace window
- **File/line:** `app/routers/bookings.py:86` (`create_booking`)
- **Rule violated:** 2 — *`start_time` must be strictly in the future — no grace.*
- **Root cause:** the guard allowed starts up to 300 seconds in the past.

```python
# Before
if start <= now - timedelta(seconds=300):
# After
if start <= now:
```

- **Test case:** book with `start_time` 2 minutes in the past (whole-hour duration).
  - Before: `201`. After: `400` `INVALID_BOOKING_WINDOW`.

---

### Bug 7 — Missing minimum-duration / `end > start` validation
- **File/line:** `app/routers/bookings.py:89–94` (`create_booking`)
- **Rule violated:** 2 — *duration is whole hours, **min 1**, max 8; `end_time`
  strictly after `start_time`.*
- **Root cause:** only an upper bound (`> 8`) was checked. `end == start` gives duration
  `0` (a whole number, not `> 8`) and `end < start` gives a negative duration — both
  slipped through, the former producing a `price_cents = 0` booking.

```python
# Before
if duration_hours > MAX_DURATION_HOURS:
    raise AppError(400, "INVALID_BOOKING_WINDOW", "duration out of range")
# After
if duration_hours < MIN_DURATION_HOURS or duration_hours > MAX_DURATION_HOURS:
    raise AppError(400, "INVALID_BOOKING_WINDOW", "duration out of range")
```

- **Test cases:** 1h/2h/8h → `201`; 0h / negative / 9h / 1.5h → `400`.
  - Before: 0h and negative returned `201`. After: all four rejected.

---

### Bug 8 — Back-to-back bookings wrongly flagged as conflicts
- **File/line:** `app/routers/bookings.py:50` (`_has_conflict`)
- **Rule violated:** 3 — *overlap iff `existing.start < new.end AND new.start <
  existing.end`; back-to-back is allowed.*
- **Root cause:** the comparison used `<=`, so touching intervals counted as
  overlapping.

```python
# Before
if b.start_time <= end and start <= b.end_time:
# After
if b.start_time < end and start < b.end_time:
```

- **Test case:** book `[10:00,11:00]`, then `[11:00,12:00]` (back-to-back), then
  `[10:30,11:30]` (real overlap).
  - Before: back-to-back → `409`. After: back-to-back → `201`, real overlap → `409`
    `ROOM_CONFLICT`.

---

### Bug 9 — Pagination: wrong ordering, offset, and page size
- **File/line:** `app/routers/bookings.py:136–140` (`list_bookings`)
- **Rule violated:** 11 — *ascending by `start_time` (ties by ascending id); `page`
  default 1; `limit` default 10, max 100; sequential pages never skip/repeat.*
- **Root cause:** three defects — descending sort, offset `page * limit` (page 1 skips
  the first page), and a hardcoded `.limit(10)` ignoring `limit`.

```python
# Before
base.order_by(Booking.start_time.desc(), Booking.id.asc())
    .offset(page * limit)
    .limit(10)
# After
base.order_by(Booking.start_time.asc(), Booking.id.asc())
    .offset((page - 1) * limit)
    .limit(limit)
```

- **Test case:** create 5 bookings; page through `limit=2`.
  - Before: page 1 skipped items, wrong order, returned 3 items. After: pages 1–3
    return the 5 bookings ascending with no skips/repeats; `limit` respected; `total=5`.

---

### Bug 10 — Booking detail clobbers `start_time`
- **File/line:** `app/routers/bookings.py:165–166` (`get_booking`)
- **Rule violated:** response-schema contract for `GET /bookings/{id}`.
- **Root cause:** the detail response overwrote `start_time` with `created_at`.

```python
# Before
response = serialize_booking(booking)
response["start_time"] = iso_utc(booking.created_at)   # removed
response["refunds"] = [ ... ]
# After
response = serialize_booking(booking)
response["refunds"] = [ ... ]
```

- **Test case:** create a booking, GET its detail.
  - Before: `start_time == created_at`. After: `start_time` equals the real start.

---

### Bug 11 — Members can read other members' bookings
- **File/line:** `app/routers/bookings.py:150–163` (`get_booking`)
- **Rule violated:** 10 — *members read only their own bookings (another member's id →
  404 BOOKING_NOT_FOUND); admins read any booking in their org.*
- **Root cause:** the getter filtered by org only and never checked ownership for
  non-admins (`cancel_booking` had the guard; the getter did not).

```python
# After — added, mirroring cancel_booking
if user.role != "admin" and booking.user_id != user.id:
    raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")
```

- **Test case:** member A books; member B (same org) GETs A's booking id.
  - Before: `200` (data leak). After: `404` `BOOKING_NOT_FOUND`. Owner → `200`,
    admin → `200`.

---

## 4. Medium-tier bugs

### Bug 3 — Refresh tokens are not single-use
- **File/line:** `app/routers/auth.py:81–93` (`refresh`), `app/auth.py`
- **Rule violated:** 8 — *refresh is single-use; refreshing invalidates the presented
  refresh token (reuse → 401).*
- **Root cause:** `refresh` validated the token type and minted a new pair but never
  invalidated the presented refresh token and never checked whether it was already
  used.
- **Impact:** a refresh token could be replayed indefinitely.

Two helpers were added in `app/auth.py`, reusing the existing `_revoked_tokens` jti
blacklist (the same one Bug 2 fixed to key on `jti`):

```python
def revoke_token(payload: dict) -> None:
    """Blacklist any token (access or refresh) by its jti."""
    _revoked_tokens.add(payload["jti"])

def is_token_revoked(payload: dict) -> bool:
    return payload.get("jti") in _revoked_tokens
```

```python
# refresh(): after checking type == "refresh"
if is_token_revoked(data):
    raise AppError(401, "UNAUTHORIZED", "Refresh token already used")
...
revoke_token(data)   # single-use: consume the presented refresh token
```

- **Test case:** login → refresh (200, new pair) → new access token usable →
  refresh with the **old** token again → 401 → refresh with the new token (200) →
  reuse the new token → 401.
  - Before: every reuse returned `200`. After: each refresh token works exactly once.

---

### Bug 12 — Refund tiers and rounding
- **File/line:** `app/routers/bookings.py:200–208` (`cancel_booking`),
  `app/services/refunds.py:14–27` (`log_refund`)
- **Rule violated:** 6 — *`≥48h → 100%`, `24h ≤ notice < 48h → 50%`, `<24h → 0%`;
  amount rounds to nearest cent, half-cents rounding **up**; the response amount equals
  the RefundLog amount.*

**(a) Tier boundaries** — two defects: the 100% branch used floored hours `> 48`
(so a notice of 48h–48h59m fell to 50%), and the `else` branch returned 50% for
`notice < 24h` (must be 0%).

```python
# Before
notice_hours = int(notice.total_seconds() // 3600)
if notice_hours > 48:      refund_percent = 100
elif notice >= timedelta(hours=24): refund_percent = 50
else:                      refund_percent = 50
# After
if notice >= timedelta(hours=48):   refund_percent = 100
elif notice >= timedelta(hours=24): refund_percent = 50
else:                               refund_percent = 0
```

**(b) Rounding + response/ledger mismatch** — the response used Python `round()`
(banker's rounding) while the ledger used `int()` truncation, so they could disagree
**and** neither honored "half-cents round up".

```python
# refunds.py — After
from decimal import ROUND_HALF_UP, Decimal
amount_cents = int(
    (Decimal(price_cents) * Decimal(percent) / Decimal(100))
    .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
)
# bookings.py cancel — After: return the stored amount (single source of truth)
entry = log_refund(db, booking_id, price_cents, refund_percent)
refund_amount_cents = entry.amount_cents
```

- **Test cases:**
  - Tiers: `48h30m → 100%`, `36h → 50%`, `25h → 50%`, `23h → 0%`.
  - Rounding: price 101 @ 50% = 50.5 → **51**; price 303 @ 50% = 151.5 → **152**.
  - Ledger parity: cancel response `refund_amount_cents` equals the single RefundLog's
    `amount_cents`.
  - Before: `48h30m` gave 50%, `23h` gave 50%, `50.5` gave 50, and `303@50%` gave
    response 152 vs ledger 151 (**mismatch**). After: all correct and consistent.

---

### Bug 13 — Stale report / availability caches
- **File/line:** `app/routers/bookings.py:121` (create), `:217` (cancel)
- **Rule violated:** 12 (report reflects current state immediately), 13 (availability
  reflects current state immediately).
- **Root cause:** each write path invalidated only one of the two dependent caches —
  create invalidated availability but not the usage-report; cancel invalidated the
  report but not availability.

```python
# create_booking — After
cache.invalidate_availability(room_id, start.date().isoformat())
cache.invalidate_report(org_id)                                   # added
# cancel_booking — After
cache.invalidate_report(org_id)
cache.invalidate_availability(room_id, start_date)               # added
```

- **Test cases:** cache a `usage-report`, create a booking, re-read; cache an
  `availability`, cancel a booking, re-read.
  - Before: report stayed at 0 after a create; availability still showed the cancelled
    booking. After: both reflect the change immediately.

---

### Bug 14 — Export leaks other orgs' bookings
- **File/line:** `app/services/export.py:48–52` (`generate_export`)
- **Rule violated:** 9 — *a user may only read data in their own org; cross-org IDs
  behave as non-existent.*
- **Root cause:** the `include_all + room_id` branch called `fetch_bookings_raw`, which
  filters only by `room_id` and **ignores `org_id`**.
- **Impact:** an admin could pass another tenant's `room_id` (with `include_all=true`)
  and export that org's bookings.

```python
# Before
if include_all:
    if room_id is not None:
        rows = fetch_bookings_raw(db, room_id)     # no org filter!
    else:
        rows = _fetch_scoped(db, org_id, None, None)
# After
if include_all:
    rows = _fetch_scoped(db, org_id, None, room_id)   # org-scoped join
```

- **Test case:** org1 admin books; org2 admin calls
  `GET /admin/export?include_all=true&room_id=<org1 room>`.
  - Before: CSV contained org1's booking. After: empty. Legitimate exports (own room,
    all-org within tenant, `include_all=false` = own bookings) still work.

---

## 5. Hard-tier bugs (concurrency)

All service modules below performed a **read → `time.sleep()` → write** without
synchronization, so concurrent callers read the same stale value and clobbered each
other. The general fix: a module-level `threading.Lock` making the read-modify-write
**atomic**, with the `sleep()` moved **outside** the critical section so threads still
overlap and the service never stalls (Rule 16).

### Bug 15 — Duplicate reference codes
- **File/line:** `app/services/reference.py:17–21` (`next_reference_code`)
- **Rule violated:** 7 — *every reference code is unique, including under concurrent
  creation.*
- **Root cause:** `current = counter; sleep; counter = current + 1` — concurrent
  callers all read the same `current`.

```python
# After
with _lock:
    current = _counter["value"]
    _counter["value"] = current + 1
_format_pause()
return f"CW-{current:06d}"
```

- **Test case:** call `next_reference_code()` from 30 barrier-synchronized threads.
  - Before: **1** unique code out of 30. After: **30/30** unique.

---

### Bug 16 — Rate limit not enforced under concurrency
- **File/line:** `app/services/ratelimit.py:18–26` (`record_and_check`)
- **Rule violated:** 5 — *20 requests / rolling 60 s per user; excess → 429; must hold
  under concurrency.*
- **Root cause:** trim → `sleep` → append → write-back; each concurrent caller starts
  from the same short bucket and last-writer-wins collapses the count.

```python
# After
with _lock:
    bucket = [t for t in _buckets.get(user_id, []) if t > now - _WINDOW_SECONDS]
    bucket.append(now)
    _buckets[user_id] = bucket
    over_limit = len(bucket) > _MAX_REQUESTS
if over_limit:
    raise AppError(429, "RATE_LIMITED", "Too many booking requests")
```

- **Test case:** 25 barrier-synchronized calls for one user.
  - Before: **25 allowed, 0 rejected** (limiter dead). After: **exactly 20 allowed, 5
    `RATE_LIMITED`.**

---

### Bug 17 — Stats lost updates
- **File/line:** `app/services/stats.py:15–26` (`record_create`, `record_cancel`)
- **Rule violated:** 14 — *stats always consistent with the bookings, including after
  bursts.*
- **Root cause:** read count/revenue → `sleep` → write back; concurrent updates lose
  increments/decrements.

```python
# After (record_create; record_cancel analogous)
_aggregate_pause()
with _lock:
    current = _stats.get(room_id, {"count": 0, "revenue": 0})
    _stats[room_id] = {"count": current["count"] + 1,
                       "revenue": current["revenue"] + price_cents}
# get() also snapshots under the lock and returns a copy
```

- **Test cases:** 30 concurrent creates; then 30 creates + 10 cancels concurrently.
  - Before: `count == 1` after 30 creates. After: `count == 30, revenue == 3000`; mixed
    → `count == 20, revenue == 2000`.

---

### Bugs 18 & 19 — Double-booking and quota bypass under concurrency
- **File/line:** `app/routers/bookings.py:100` (`_has_conflict` + insert), `:103`
  (`_check_quota` + insert)
- **Rules violated:** 3 (no double-booking, under concurrency), 4 (quota ≤ 3, under
  concurrency).
- **Root cause:** the conflict/quota checks (each with a `sleep`) and the subsequent
  `db.add` + `db.commit` were not atomic, so N concurrent requests all passed the check
  and all committed.

**Fix:** a single module-level `threading.Lock` (`_booking_write_lock`) serializes the
entire check-then-insert region so a second request only runs its checks after the
first has committed:

```python
db.rollback()   # release the auth read-transaction before contending (see §6)
with _booking_write_lock:
    room = db.query(Room).filter(Room.id == payload.room_id, Room.org_id == org_id).first()
    if room is None:
        raise AppError(404, "ROOM_NOT_FOUND", "Room not found")
    if _has_conflict(db, room.id, start, end):
        raise AppError(409, "ROOM_CONFLICT", "Room already booked for this interval")
    _check_quota(db, user_id, now, start)
    booking = Booking(...)
    db.add(booking); db.commit(); db.refresh(booking)
```

- **Test cases (real server, barrier-synchronized HTTP):**
  - 8 identical bookings for the same room/slot.
    - Before: **8 created.** After: **1 created, 7 × 409 `ROOM_CONFLICT`.**
  - 6 concurrent bookings (distinct rooms) within the 24h quota window by one member.
    - Before: **6 created.** After: **3 created, 3 × 409 `QUOTA_EXCEEDED`.**

---

### Bug 20 — Duplicate refunds on concurrent cancel
- **File/line:** `app/routers/bookings.py:195–214` (`cancel_booking`),
  `app/services/refunds.py` (`log_refund`)
- **Rule violated:** 6 — *a cancelled booking has exactly one RefundLog; the returned
  amount equals the stored one; must hold under concurrent cancels for the same
  booking.*
- **Root cause:** the `status == "cancelled"` check, `log_refund`, `sleep`, status
  write, and commit were not atomic, so concurrent cancels each saw `confirmed` and
  each wrote a RefundLog.

**Fix:** a DB-level **atomic compare-and-swap** inside `_booking_write_lock` — only the
request whose `UPDATE ... WHERE status='confirmed'` matches the row proceeds to write
the single refund:

```python
with _booking_write_lock:
    updated = (db.query(Booking)
               .filter(Booking.id == booking_id, Booking.status == "confirmed")
               .update({Booking.status: "cancelled"}, synchronize_session=False))
    db.commit()
    if updated == 0:
        raise AppError(409, "ALREADY_CANCELLED", "Booking already cancelled")
    entry = log_refund(db, booking_id, price_cents, refund_percent)
    refund_amount_cents = entry.amount_cents
```

`log_refund` was refactored to `(db, booking_id, price_cents, percent)` so it does not
depend on an ORM object whose attributes are expired by the pre-lock `rollback()`; the
half-up rounding and response==ledger guarantee (Bug 12) are preserved.

- **Test case:** 8 concurrent cancels of one booking; then GET its detail.
  - Before: **8 × 200, 8 RefundLogs.** After: **1 × 200, 7 × 409 `ALREADY_CANCELLED`,
    exactly one RefundLog,** response amount == ledger amount.

---

### Bug 21 — Notification lock-ordering deadlock
- **File/line:** `app/services/notifications.py:24–35`
- **Rule violated:** 16 — *no combination of concurrent valid requests may hang the
  service.*
- **Root cause:** `notify_created` acquired `_email_lock` → `_audit_lock`, while
  `notify_cancelled` acquired `_audit_lock` → `_email_lock`. A concurrent create +
  cancel could each hold one lock and block forever on the other. (These run *outside*
  `_booking_write_lock`, so they genuinely overlap.)

```python
# notify_cancelled — After: same order as notify_created (email → audit)
with _email_lock:
    _send_email("cancelled", booking)
    with _audit_lock:
        _write_audit("cancelled", booking)
```

- **Test cases:**
  - Direct: run `notify_created` and `notify_cancelled` on two threads with a 3 s
    watchdog.
    - Before: **deadlock** (watchdog fires). After: completes.
  - HTTP: fire 3 creates + 3 cancels simultaneously.
    - Before: **5 of 6 requests time out.** After: **all 6 complete**, `/health`
      responsive.

---

## 6. Input-validation & cache-correctness bugs

### Bug 22 — Malformed datetime crashes with 500 instead of 400
- **File/line:** `app/routers/bookings.py:82–83` (`create_booking`)
- **Rule violated:** Errors contract — invalid booking input must return
  `400 INVALID_BOOKING_WINDOW`, not an unhandled server error.
- **Root cause:** `start_time` / `end_time` are typed as `str`, so FastAPI accepts any
  string; `parse_input_datetime` then calls `datetime.fromisoformat(...)`, which raises
  `ValueError` on a non-ISO value. The exception was unhandled and surfaced as
  **HTTP 500**. The sibling endpoints (`availability`, `usage-report`) already catch the
  parse error and return `400 INVALID_BOOKING_WINDOW`; `create_booking` was the
  inconsistent one.

```python
# After
try:
    start = parse_input_datetime(payload.start_time)
    end = parse_input_datetime(payload.end_time)
except ValueError:
    raise AppError(400, "INVALID_BOOKING_WINDOW", "Invalid datetime")
```

- **Test cases:** `start_time="not-a-date"`, `""`, `end_time="13:99"`,
  `start_time="2026-13-40T10:00:00"`.
  - Before: all `500`. After: all `400` `INVALID_BOOKING_WINDOW`; a valid booking still
    returns `201`.

---

### Bug 23 — Availability cache keyed by the raw date string
- **File/line:** `app/routers/rooms.py:69–99` (`availability`)
- **Rule violated:** 13 — *availability reflects the current state immediately.*
- **Root cause:** the cache was read/written under the **raw** `date` query string,
  while invalidation (on create/cancel) uses the normalized `start.date().isoformat()`.
  `strptime("%Y-%m-%d")` accepts non-zero-padded spellings such as `2026-7-9`, so such a
  request cached under a key that invalidation can **never** clear — the pre-booking
  (empty) busy list was then returned forever for that spelling. The cache was also
  consulted before the date was validated.

```python
# After — validate first, then key on the normalized date
try:
    day = datetime.strptime(date, "%Y-%m-%d").date()
except ValueError:
    raise AppError(400, "INVALID_BOOKING_WINDOW", "Invalid date")
date_key = day.isoformat()
cached = cache.get_availability(room.id, date_key)
...
result = {"room_id": room.id, "date": date_key, "busy": [...]}
cache.set_availability(room.id, date_key, result)
```

- **Test case:** warm the cache with `?date=2027-3-5`, create a booking that day,
  re-query `?date=2027-3-5`.
  - Before: still `busy: []` (stale), and `date` echoed as `2027-3-5`. After: reflects
    the booking (1 busy), agrees with the padded `?date=2027-03-05` query, and echoes
    the normalized `2027-03-05`. Invalid dates still return `400`.

---

## 7. Concurrency deep-dive: why a lock alone was not enough

The booking/cancel fixes (Bugs 18–20) required care beyond "wrap it in a lock",
because of how SQLite's locking interacts with per-request transactions:

- The auth dependency (`get_current_user`) issues a `SELECT` that opens a **read
  transaction** on the request's session. If a thread then parked on
  `_booking_write_lock` while still holding that read transaction, the thread *inside*
  the lock could not obtain SQLite's `EXCLUSIVE` lock to `COMMIT` (a writer must wait
  for all readers to release), producing multi-second stalls / `database is locked`
  errors — itself a liveness problem.
- **Mitigation:** each handler calls `db.rollback()` **before** contending for the lock
  (after snapshotting the scalar fields it needs). A waiting thread therefore holds no
  DB lock, and its post-lock queries begin a fresh transaction that observes other
  threads' commits — which is exactly what makes the re-check inside the lock correct.
- Keeping the `sleep()` "settlement" pauses **outside** the locked region keeps the
  critical section short, so the serialization required for correctness does not turn
  into a throughput cliff or a hang.

This is validated by the liveness assertions in the concurrency suites: after each
burst, `GET /health` still returns 200 in well under a second, and every create/cancel
request returns (no timeouts).

---

## 8. Non-bugs considered (and why they were left alone)

- **`iso_utc` renders `+00:00`** — a valid ISO-8601 UTC designator, satisfying Rule 1.
- **`usage-report` date window** (`>= from 00:00`, `< (to + 1 day) 00:00`) correctly
  implements the inclusive `[from, to]` day range — not a bug.
- **`Booking.reference_code` has no DB `UNIQUE` constraint** — the reference-code lock
  (Bug 15) already guarantees uniqueness; adding a constraint would require a schema
  change and is not necessary for correctness.
- **The planted `time.sleep()` helpers** are intentional race-window wideners, not
  bugs; the fixes are correct with them left in place.
- **`fetch_bookings_raw`** (in `export.py`) is now unreachable after Bug 14's fix; it
  was left in place to keep the change minimal (no unrelated refactor).

---

## 9. How to reproduce

```bash
# Windows, no Docker
.venv\Scripts\activate
uvicorn app.main:app --reload           # http://localhost:8000/docs
```

The verification harnesses used for this report live outside the repository (in a
scratch workspace) and follow the pattern described in §1: direct function tests,
`fastapi.testclient.TestClient` API tests, and `uvicorn` + `httpx` real-server
concurrency tests synchronized with a `threading.Barrier`. Each was run
**before** and **after** its corresponding fix, with all prior suites re-run as
regression guards.

---

## 10. Files changed

| File | Bugs addressed |
|------|----------------|
| `app/auth.py` | 1, 2, 3 |
| `app/routers/auth.py` | 3, 5 |
| `app/timeutils.py` | 4 |
| `app/routers/bookings.py` | 6, 7, 8, 9, 10, 11, 12, 13, 18, 19, 20, 22 |
| `app/routers/rooms.py` | 23 |
| `app/services/refunds.py` | 12, 20 |
| `app/services/export.py` | 14 |
| `app/services/reference.py` | 15 |
| `app/services/ratelimit.py` | 16 |
| `app/services/stats.py` | 17 |
| `app/services/notifications.py` | 21 |

**Total: 23 bugs fixed** — 11 Easy, 5 Medium, 7 Hard *(difficulty tags are our own
estimates; the challenge does not label individual bugs)*.
