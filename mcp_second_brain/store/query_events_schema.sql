-- Explicit administrator migration only. The application never executes DDL.
-- sb_app is provisioned independently by the multiuser deployment migration.
CREATE TABLE IF NOT EXISTS query_events (
    event_id uuid PRIMARY KEY,
    request_id uuid NOT NULL UNIQUE,
    actor_id uuid,
    actor_role text NOT NULL CHECK (actor_role IN
        ('admin','member','reader','writer','viewer','analyst','unknown')),
    service text NOT NULL CHECK (service ~ '^[a-z][a-z0-9_]{0,63}$'),
    tool text NOT NULL CHECK (tool ~ '^[a-z][a-z0-9_]{0,63}$'),
    operation text NOT NULL CHECK (operation ~ '^[a-z][a-z0-9_]{0,63}$'),
    started_at timestamptz NOT NULL,
    duration_ms double precision NOT NULL CHECK
        (duration_ms >= 0 AND duration_ms < 'Infinity'::double precision),
    status text NOT NULL CHECK (status IN ('success','error','denied','cancelled','unknown')),
    error_code text NOT NULL CHECK (error_code IN
        ('none','internal','denied','cancelled','timeout','unavailable','invalid_request','classification_failed','unclassified')),
    result_count bigint CHECK (result_count >= 0),
    revision text CHECK (revision ~ '^[0-9a-f]{7,64}$'),
    retrieval_revision text CHECK (retrieval_revision ~ '^[0-9a-f]{7,64}$'),
    CHECK ((status = 'success' AND error_code = 'none')
        OR (status = 'denied' AND error_code = 'denied')
        OR (status = 'cancelled' AND error_code = 'cancelled')
        OR (status = 'unknown' AND error_code IN ('classification_failed','unclassified'))
        OR (status = 'error' AND error_code IN
            ('internal','timeout','unavailable','invalid_request')))
);
CREATE INDEX IF NOT EXISTS query_events_actor_time ON query_events(actor_id, started_at);
CREATE INDEX IF NOT EXISTS query_events_tool_time ON query_events(tool, started_at);
CREATE INDEX IF NOT EXISTS query_events_time ON query_events(started_at);
ALTER TABLE query_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE query_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS query_events_read ON query_events;
CREATE POLICY query_events_read ON query_events FOR SELECT TO sb_app USING (
    current_setting('app.query_event_role', true) = 'admin'
    OR actor_id = NULLIF(current_setting('app.query_event_actor', true), '')::uuid
);
DROP POLICY IF EXISTS query_events_insert ON query_events;
CREATE POLICY query_events_insert ON query_events FOR INSERT TO sb_app WITH CHECK (
    actor_id IS NOT DISTINCT FROM NULLIF(current_setting('app.query_event_actor', true), '')::uuid
    AND actor_role = current_setting('app.query_event_role', true)
);
REVOKE ALL ON query_events FROM PUBLIC, sb_app;
GRANT SELECT, INSERT ON query_events TO sb_app;

-- No actor ID, query text, resource ID or result body survives rollup.
CREATE TABLE IF NOT EXISTS query_event_daily (
    day date NOT NULL,
    service text NOT NULL,
    tool text NOT NULL,
    operation text NOT NULL,
    status text NOT NULL,
    error_code text NOT NULL,
    event_count bigint NOT NULL CHECK (event_count >= 0),
    duration_total_ms double precision NOT NULL CHECK (duration_total_ms >= 0),
    duration_max_ms double precision NOT NULL CHECK (duration_max_ms >= 0),
    result_total bigint NOT NULL CHECK (result_total >= 0),
    result_observed bigint NOT NULL CHECK (result_observed >= 0),
    zero_result_count bigint NOT NULL CHECK (zero_result_count >= 0),
    search_observed bigint NOT NULL CHECK (search_observed >= zero_result_count),
    PRIMARY KEY (day, service, tool, operation, status, error_code)
);
ALTER TABLE query_event_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE query_event_daily FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS query_event_daily_admin ON query_event_daily;
CREATE POLICY query_event_daily_admin ON query_event_daily FOR SELECT TO sb_app USING (
    current_setting('app.query_event_role', true) = 'admin'
);
REVOKE ALL ON query_event_daily FROM PUBLIC, sb_app;
GRANT SELECT ON query_event_daily TO sb_app;
