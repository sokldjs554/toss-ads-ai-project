-- Additive, dedicated pilot schema. Never run automatically on app startup.
CREATE SCHEMA preflight_live_pilot;
REVOKE ALL ON SCHEMA preflight_live_pilot FROM PUBLIC;
CREATE TABLE preflight_live_pilot.accounts (
    id text PRIMARY KEY,
    token_hash text NOT NULL UNIQUE CHECK(token_hash ~ '^[0-9a-f]{64}$'),
    kind text NOT NULL CHECK(kind IN ('automation','person_unverified')),
    active boolean NOT NULL DEFAULT true,
    expires_at timestamptz NOT NULL DEFAULT now() + interval '30 days'
);
CREATE TABLE preflight_live_pilot.runs (
    request_id uuid PRIMARY KEY,
    account_id text NOT NULL REFERENCES preflight_live_pilot.accounts(id),
    input_hash text NOT NULL,
    payload jsonb NOT NULL,
    build text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    reserved_usd numeric NOT NULL DEFAULT .08 CHECK(reserved_usd = .08),
    call_count integer NOT NULL DEFAULT 0 CHECK(call_count BETWEEN 0 AND 4),
    result jsonb,
    review jsonb
);
CREATE TABLE preflight_live_pilot.calls (
    request_id uuid NOT NULL REFERENCES preflight_live_pilot.runs(request_id),
    sequence integer NOT NULL CHECK(sequence BETWEEN 1 AND 4),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    stage text NOT NULL CHECK(stage IN ('rag','rewrite_tools')),
    record jsonb NOT NULL DEFAULT '{"status":"outcome_unknown","cost_usd":null}',
    PRIMARY KEY(request_id,sequence)
);
CREATE TABLE preflight_live_pilot.audit (
    request_id uuid PRIMARY KEY REFERENCES preflight_live_pilot.runs(request_id),
    account_id text NOT NULL REFERENCES preflight_live_pilot.accounts(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    record jsonb NOT NULL
);
