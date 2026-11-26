-- VolunteerConnect schema (see PRD.md, "Data model").
--
-- This file runs on every app start, so every statement must be safe to run
-- again: use CREATE TABLE IF NOT EXISTS, and for later changes append
-- ALTER TABLE ... ADD COLUMN IF NOT EXISTS at the bottom.
-- Never DROP a table: redeploys must never erase data.
-- All times are timestamptz.

CREATE TABLE IF NOT EXISTS schools (
    id                  BIGSERIAL PRIMARY KEY,
    name                TEXT NOT NULL,
    address             TEXT,
    invite_code         TEXT NOT NULL UNIQUE,
    required_clearances TEXT[] NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('coordinator', 'teacher', 'volunteer')),
    school_id     BIGINT REFERENCES schools(id),
    -- For volunteers this column is NOT authoritative: their status is derived
    -- from unexpired clearances (volunteerconnect/volunteer_status.py).
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active')),
    blocked_until TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Teachers always have a school; nobody else does.
    CHECK ((role = 'teacher') = (school_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS clearances (
    id           BIGSERIAL PRIMARY KEY,
    volunteer_id BIGINT NOT NULL REFERENCES users(id),
    type         TEXT NOT NULL CHECK (type IN ('tb_test', 'background_check')),
    verified_by  BIGINT NOT NULL REFERENCES users(id),
    verified_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_on   DATE NOT NULL,
    UNIQUE (volunteer_id, type)
);

CREATE TABLE IF NOT EXISTS help_requests (
    id                BIGSERIAL PRIMARY KEY,
    school_id         BIGINT NOT NULL REFERENCES schools(id),
    teacher_id        BIGINT NOT NULL REFERENCES users(id),
    activity          TEXT NOT NULL,
    room              TEXT,
    date              DATE NOT NULL,
    start_time        TIME NOT NULL,
    end_time          TIME NOT NULL,
    volunteers_needed INTEGER NOT NULL CHECK (volunteers_needed > 0),
    status            TEXT NOT NULL DEFAULT 'submitted'
                      CHECK (status IN ('submitted', 'changes_requested', 'approved', 'rejected')),
    coordinator_note  TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (end_time > start_time)
);

CREATE TABLE IF NOT EXISTS shifts (
    id         BIGSERIAL PRIMARY KEY,
    request_id BIGINT REFERENCES help_requests(id),
    school_id  BIGINT NOT NULL REFERENCES schools(id),
    start_at   TIMESTAMPTZ NOT NULL,
    end_at     TIMESTAMPTZ NOT NULL,
    capacity   INTEGER NOT NULL CHECK (capacity > 0),
    status     TEXT NOT NULL DEFAULT 'open'
               CHECK (status IN ('open', 'full', 'cancelled', 'completed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (end_at > start_at)
);

CREATE TABLE IF NOT EXISTS signups (
    id           BIGSERIAL PRIMARY KEY,
    shift_id     BIGINT NOT NULL REFERENCES shifts(id),
    volunteer_id BIGINT NOT NULL REFERENCES users(id),
    status       TEXT NOT NULL
                 CHECK (status IN ('confirmed', 'waitlisted', 'cancelled', 'present', 'no_show')),
    -- Waitlist order; reset to now() when a volunteer rejoins (PRD rule 11).
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (shift_id, volunteer_id)
);

-- Phase 2
CREATE TABLE IF NOT EXISTS swap_requests (
    id             BIGSERIAL PRIMARY KEY,
    from_signup_id BIGINT NOT NULL REFERENCES signups(id),
    to_shift_id    BIGINT NOT NULL REFERENCES shifts(id),
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by     BIGINT REFERENCES users(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Phase 2
CREATE TABLE IF NOT EXISTS strikes (
    id           BIGSERIAL PRIMARY KEY,
    volunteer_id BIGINT NOT NULL REFERENCES users(id),
    signup_id    BIGINT REFERENCES signups(id),
    reason       TEXT NOT NULL CHECK (reason IN ('no_show', 'late_cancel')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Failed logins, for rate limiting (see auth.py). Kept in the database, not
-- in memory, because Cloud Run may run several copies of the app at once.
-- `email` is whatever was typed (lowercased): it may not belong to any
-- account, so nothing here reveals which emails are registered.
CREATE TABLE IF NOT EXISTS login_attempts (
    id           BIGSERIAL PRIMARY KEY,
    email        TEXT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_attempts_email_time ON login_attempts (email, attempted_at);

-- Future schema changes go below, as ALTER TABLE ... IF NOT EXISTS.
