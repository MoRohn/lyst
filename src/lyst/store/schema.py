# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Rohn Springfield
"""Versioned schemas. Append a new (version, sql) pair; never edit an applied one."""

from __future__ import annotations

from .db import Migration

CONTROL: list[Migration] = [
    (
        1,
        """
        CREATE TABLE workspaces (
            id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            autonomy VARCHAR NOT NULL DEFAULT 'delegate',
            allow_external_llm BOOLEAN NOT NULL DEFAULT false
        );
        CREATE TABLE users (
            id VARCHAR PRIMARY KEY,
            email VARCHAR UNIQUE NOT NULL,
            name VARCHAR NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE TABLE memberships (
            workspace_id VARCHAR NOT NULL,
            user_id VARCHAR NOT NULL,
            role VARCHAR NOT NULL,
            PRIMARY KEY (workspace_id, user_id)
        );
        CREATE TABLE api_tokens (
            token_hash VARCHAR PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            label VARCHAR,
            created_at TIMESTAMPTZ DEFAULT now(),
            revoked_at TIMESTAMPTZ
        );
        CREATE TABLE audit_log (
            id BIGINT PRIMARY KEY,
            occurred_at TIMESTAMPTZ DEFAULT now(),
            actor VARCHAR NOT NULL,
            workspace_id VARCHAR,
            action VARCHAR NOT NULL,
            target VARCHAR,
            outcome VARCHAR NOT NULL,
            detail JSON
        );
        CREATE SEQUENCE audit_seq;
        CREATE TABLE tasks (
            id VARCHAR PRIMARY KEY,
            workspace_id VARCHAR NOT NULL,
            created_by VARCHAR NOT NULL,
            kind VARCHAR NOT NULL,
            title VARCHAR NOT NULL,
            request JSON NOT NULL,
            status VARCHAR NOT NULL,
            autonomy VARCHAR NOT NULL,
            plan JSON,
            result JSON,
            error VARCHAR,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_steps INTEGER NOT NULL DEFAULT 40,
            budget_seconds INTEGER NOT NULL DEFAULT 600,
            lease_owner VARCHAR,
            lease_until TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now(),
            schedule_id VARCHAR
        );
        CREATE TABLE task_steps (
            task_id VARCHAR NOT NULL,
            seq INTEGER NOT NULL,
            phase VARCHAR NOT NULL,
            capability VARCHAR NOT NULL,
            agent VARCHAR NOT NULL,
            args JSON,
            status VARCHAR NOT NULL,
            output JSON,
            error VARCHAR,
            idempotency_key VARCHAR,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            PRIMARY KEY (task_id, seq)
        );
        CREATE TABLE task_events (
            id BIGINT PRIMARY KEY,
            task_id VARCHAR NOT NULL,
            occurred_at TIMESTAMPTZ DEFAULT now(),
            kind VARCHAR NOT NULL,
            message VARCHAR NOT NULL,
            data JSON
        );
        CREATE SEQUENCE task_event_seq;
        CREATE TABLE approvals (
            id VARCHAR PRIMARY KEY,
            task_id VARCHAR,
            workspace_id VARCHAR NOT NULL,
            requested_by VARCHAR NOT NULL,
            action VARCHAR NOT NULL,
            summary VARCHAR NOT NULL,
            payload JSON,
            status VARCHAR NOT NULL DEFAULT 'pending',
            decided_by VARCHAR,
            decided_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE TABLE schedules (
            id VARCHAR PRIMARY KEY,
            workspace_id VARCHAR NOT NULL,
            created_by VARCHAR NOT NULL,
            title VARCHAR NOT NULL,
            request JSON NOT NULL,
            every_seconds INTEGER NOT NULL,
            next_run_at TIMESTAMPTZ NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT true,
            approval_id VARCHAR
        );
        CREATE TABLE llm_calls (
            id BIGINT PRIMARY KEY,
            occurred_at TIMESTAMPTZ DEFAULT now(),
            workspace_id VARCHAR,
            task_id VARCHAR,
            model VARCHAR,
            purpose VARCHAR,
            external BOOLEAN,
            input_tokens INTEGER,
            output_tokens INTEGER,
            latency_ms INTEGER,
            outcome VARCHAR
        );
        CREATE SEQUENCE llm_call_seq
        """,
    ),
]

CONTROL.append((
    2,
    """
    ALTER TABLE llm_calls ADD COLUMN pool VARCHAR;
    ALTER TABLE llm_calls ADD COLUMN workload VARCHAR;
    ALTER TABLE llm_calls ADD COLUMN provider VARCHAR;
    ALTER TABLE llm_calls ADD COLUMN status INTEGER;
    ALTER TABLE llm_calls ADD COLUMN attempt INTEGER;
    ALTER TABLE llm_calls ADD COLUMN queue_ms INTEGER;
    ALTER TABLE llm_calls ADD COLUMN cost_usd DOUBLE
    """,
))

CONTROL.append((
    3,
    """
    CREATE TABLE eval_runs (
        id VARCHAR PRIMARY KEY,
        workspace_id VARCHAR NOT NULL,
        label VARCHAR,
        model VARCHAR,
        cases INTEGER NOT NULL,
        passed INTEGER NOT NULL,
        p95_seconds DOUBLE,
        results JSON NOT NULL,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now()
    )
    """,
))

CONTROL.append((
    4,
    """
    CREATE TABLE artifacts (
        id VARCHAR PRIMARY KEY,
        workspace_id VARCHAR NOT NULL,
        task_id VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        path VARCHAR NOT NULL,
        bytes BIGINT NOT NULL,
        sha256 VARCHAR NOT NULL,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now()
    )
    """,
))

CONTROL.append((
    6,
    """
    ALTER TABLE workspaces ADD COLUMN webhook_url VARCHAR
    """,
))

CONTROL.append((
    8,
    """
    CREATE TABLE secret_blobs (
        ref VARCHAR PRIMARY KEY,
        key_version INTEGER NOT NULL,
        nonce BLOB NOT NULL,
        ciphertext BLOB NOT NULL,
        dek_nonce BLOB NOT NULL,
        wrapped_dek BLOB NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE credentials (
        id VARCHAR PRIMARY KEY,
        scope_kind VARCHAR NOT NULL,
        scope_id VARCHAR,
        provider VARCHAR NOT NULL,
        name VARCHAR NOT NULL,
        secret_ref VARCHAR NOT NULL,
        backend VARCHAR NOT NULL,
        key_version INTEGER NOT NULL,
        fields JSON NOT NULL,
        owner_id VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        version INTEGER NOT NULL,
        fingerprint VARCHAR,
        last_test JSON,
        last_used_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT now(),
        replaced_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ,
        revoked_by VARCHAR
    );
    CREATE SEQUENCE credential_event_seq;
    CREATE TABLE credential_events (
        seq BIGINT PRIMARY KEY,
        occurred_at VARCHAR NOT NULL,
        actor VARCHAR NOT NULL,
        action VARCHAR NOT NULL,
        credential_id VARCHAR,
        scope_kind VARCHAR NOT NULL,
        scope_id VARCHAR,
        outcome VARCHAR NOT NULL,
        detail JSON,
        prev_hash VARCHAR NOT NULL,
        hash VARCHAR NOT NULL
    );
    CREATE TABLE installation_admins (
        user_id VARCHAR PRIMARY KEY,
        added_by VARCHAR NOT NULL,
        added_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE installation_settings (
        key VARCHAR PRIMARY KEY,
        value VARCHAR,
        set_by VARCHAR NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT now()
    );
    ALTER TABLE connections ADD COLUMN credential_id VARCHAR
    """,
))

CONTROL.append((
    7,
    """
    CREATE TABLE connections (
        id VARCHAR PRIMARY KEY,
        workspace_id VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        name VARCHAR NOT NULL,
        config JSON NOT NULL,
        secrets JSON NOT NULL,
        status VARCHAR NOT NULL,
        last_tested_at TIMESTAMPTZ,
        last_error VARCHAR,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE connection_syncs (
        connection_id VARCHAR NOT NULL,
        workspace_id VARCHAR NOT NULL,
        source_table VARCHAR NOT NULL,
        dataset VARCHAR NOT NULL,
        cursor_column VARCHAR,
        last_cursor JSON,
        last_sync_at TIMESTAMPTZ,
        last_rows_added BIGINT,
        rows_total BIGINT,
        syncs INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (connection_id, source_table)
    )
    """,
))

CONTROL.append((
    5,
    """
    ALTER TABLE api_tokens ADD COLUMN expires_at TIMESTAMPTZ;
    ALTER TABLE api_tokens ADD COLUMN last_used_at TIMESTAMPTZ;
    ALTER TABLE users ADD COLUMN subject VARCHAR;
    ALTER TABLE users ADD COLUMN last_login_at TIMESTAMPTZ
    """,
))

# Takeovers of a dead worker's lease, counted apart from ordinary claims so a task paused and resumed many times
# is not mistaken for one that keeps killing its worker (runtime.MAX_TAKEOVERS).
CONTROL.append((
    9,
    "ALTER TABLE tasks ADD COLUMN takeovers INTEGER NOT NULL DEFAULT 0",
))

WORKSPACE: list[Migration] = [
    (
        1,
        """
        CREATE SCHEMA data;
        CREATE TABLE datasets (
            name VARCHAR PRIMARY KEY,
            title VARCHAR NOT NULL,
            description VARCHAR,
            owner VARCHAR NOT NULL,
            classification VARCHAR NOT NULL DEFAULT 'internal',
            current_version INTEGER NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE TABLE dataset_versions (
            dataset VARCHAR NOT NULL,
            version INTEGER NOT NULL,
            physical_table VARCHAR NOT NULL,
            source VARCHAR NOT NULL,
            source_sha256 VARCHAR NOT NULL,
            row_count BIGINT NOT NULL,
            schema JSON NOT NULL,
            profile JSON,
            created_by VARCHAR NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (dataset, version)
        );
        CREATE TABLE column_policies (
            dataset VARCHAR NOT NULL,
            column_name VARCHAR NOT NULL,
            sensitivity VARCHAR NOT NULL,
            min_role VARCHAR NOT NULL,
            PRIMARY KEY (dataset, column_name)
        );
        CREATE TABLE metrics (
            name VARCHAR PRIMARY KEY,
            current_version INTEGER NOT NULL,
            status VARCHAR NOT NULL DEFAULT 'approved',
            owner VARCHAR NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE TABLE metric_versions (
            name VARCHAR NOT NULL,
            version INTEGER NOT NULL,
            spec JSON NOT NULL,
            note VARCHAR,
            created_by VARCHAR NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (name, version)
        );
        CREATE TABLE glossary (
            term VARCHAR PRIMARY KEY,
            definition VARCHAR NOT NULL,
            maps_to_metric VARCHAR,
            status VARCHAR NOT NULL DEFAULT 'approved',
            source VARCHAR,
            updated_by VARCHAR NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE TABLE query_history (
            id BIGINT PRIMARY KEY,
            occurred_at TIMESTAMPTZ DEFAULT now(),
            actor VARCHAR NOT NULL,
            task_id VARCHAR,
            sql VARCHAR NOT NULL,
            rows BIGINT,
            ms INTEGER,
            outcome VARCHAR NOT NULL,
            error VARCHAR
        );
        CREATE SEQUENCE query_seq;
        CREATE TABLE analyses (
            id VARCHAR PRIMARY KEY,
            task_id VARCHAR,
            question VARCHAR NOT NULL,
            answer JSON NOT NULL,
            evidence JSON NOT NULL,
            created_by VARCHAR NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE TABLE memory (
            id VARCHAR PRIMARY KEY,
            layer VARCHAR NOT NULL,
            scope VARCHAR NOT NULL,
            owner VARCHAR NOT NULL,
            content VARCHAR NOT NULL,
            provenance JSON NOT NULL,
            trust VARCHAR NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            expires_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ
        );
        CREATE TABLE skills (
            name VARCHAR NOT NULL,
            version INTEGER NOT NULL,
            status VARCHAR NOT NULL,
            purpose VARCHAR NOT NULL,
            source VARCHAR NOT NULL,
            tests VARCHAR NOT NULL,
            permissions JSON NOT NULL,
            test_report JSON,
            proposed_by VARCHAR NOT NULL,
            approved_by VARCHAR,
            created_at TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (name, version)
        )
        """,
    ),
    (
        2,
        """
        CREATE TABLE entities (
            kind VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            description VARCHAR,
            owner VARCHAR,
            attributes JSON,
            source VARCHAR NOT NULL,
            updated_by VARCHAR NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (kind, name)
        );
        CREATE TABLE relations (
            from_kind VARCHAR NOT NULL,
            from_name VARCHAR NOT NULL,
            rel VARCHAR NOT NULL,
            to_kind VARCHAR NOT NULL,
            to_name VARCHAR NOT NULL,
            evidence JSON,
            source VARCHAR NOT NULL,
            created_by VARCHAR NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (from_kind, from_name, rel, to_kind, to_name)
        );
        CREATE TABLE documents (
            id VARCHAR PRIMARY KEY,
            title VARCHAR NOT NULL,
            filename VARCHAR NOT NULL,
            classification VARCHAR NOT NULL,
            min_role VARCHAR NOT NULL,
            passages INTEGER NOT NULL,
            chars INTEGER NOT NULL,
            added_by VARCHAR NOT NULL,
            added_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE TABLE passages (
            document_id VARCHAR NOT NULL,
            seq INTEGER NOT NULL,
            text VARCHAR NOT NULL,
            terms JSON NOT NULL,
            PRIMARY KEY (document_id, seq)
        )
        """,
    ),
    (
        3,
        """
        CREATE TABLE monitors (
            id VARCHAR PRIMARY KEY,
            metric VARCHAR NOT NULL,
            grain VARCHAR NOT NULL,
            threshold_sigma DOUBLE NOT NULL,
            every_seconds INTEGER NOT NULL,
            title VARCHAR NOT NULL,
            investigate BOOLEAN NOT NULL DEFAULT true,
            enabled BOOLEAN NOT NULL DEFAULT false,
            approval_id VARCHAR,
            created_by VARCHAR NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            next_run_at TIMESTAMPTZ,
            last_run_at TIMESTAMPTZ
        );
        CREATE TABLE findings (
            id VARCHAR PRIMARY KEY,
            monitor_id VARCHAR NOT NULL,
            kind VARCHAR NOT NULL,
            period VARCHAR NOT NULL,
            label VARCHAR NOT NULL,
            metric VARCHAR NOT NULL,
            summary VARCHAR NOT NULL,
            priority DOUBLE NOT NULL,
            direction VARCHAR NOT NULL,
            z DOUBLE,
            value DOUBLE,
            baseline DOUBLE,
            task_id VARCHAR,
            status VARCHAR NOT NULL,
            dedupe_key VARCHAR UNIQUE,
            created_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE TABLE notifications (
            id VARCHAR PRIMARY KEY,
            user_id VARCHAR,
            finding_id VARCHAR NOT NULL,
            channel VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now()
        )
        """,
    ),
    (
        4,
        """
        CREATE TABLE dashboards (
            id VARCHAR PRIMARY KEY,
            title VARCHAR NOT NULL,
            design JSON NOT NULL,
            path VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            created_by VARCHAR NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            last_refresh_at TIMESTAMPTZ,
            refresh_count INTEGER NOT NULL DEFAULT 0,
            schedule_id VARCHAR,
            checks JSON
        )
        """,
    ),
    (
        5,
        """
        CREATE TABLE passage_vectors (
            document_id VARCHAR NOT NULL,
            seq INTEGER NOT NULL,
            model VARCHAR NOT NULL,
            vector FLOAT[] NOT NULL,
            PRIMARY KEY (document_id, seq)
        );
        CREATE TABLE memory_vectors (
            memory_id VARCHAR PRIMARY KEY,
            model VARCHAR NOT NULL,
            vector FLOAT[] NOT NULL
        )
        """,
    ),
    (
        6,
        """
        CREATE TABLE workflows (
            id VARCHAR NOT NULL,
            version INTEGER NOT NULL,
            signature VARCHAR NOT NULL,
            title VARCHAR NOT NULL,
            intent VARCHAR NOT NULL,
            definition JSON NOT NULL,
            deps JSON NOT NULL,
            permissions JSON NOT NULL,
            patterns VARCHAR[] NOT NULL,
            status VARCHAR NOT NULL,
            invalid_reason VARCHAR,
            source_task VARCHAR,
            last_verified_task VARCHAR,
            verified_runs INTEGER NOT NULL DEFAULT 0,
            runs INTEGER NOT NULL DEFAULT 0,
            failed_verifications INTEGER NOT NULL DEFAULT 0,
            created_by VARCHAR NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            last_verified_at TIMESTAMPTZ,
            last_used_at TIMESTAMPTZ,
            PRIMARY KEY (id, version)
        )
        """,
    ),
]

WORKSPACE.append((
    7,
    """
    CREATE TABLE row_policies (
        dataset VARCHAR NOT NULL,
        column_name VARCHAR NOT NULL,
        principal_kind VARCHAR NOT NULL,
        principal VARCHAR NOT NULL,
        allowed_values JSON NOT NULL,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (dataset, column_name, principal_kind, principal)
    )
    """,
))

CONTROL.append((
    10,
    """
    CREATE TABLE model_catalog (
        id VARCHAR PRIMARY KEY,
        fetched_at TIMESTAMPTZ DEFAULT now(),
        fetched_by VARCHAR,
        payload VARCHAR NOT NULL
    );
    ALTER TABLE workspaces ADD COLUMN llm_tier VARCHAR;
    ALTER TABLE workspaces ADD COLUMN llm_model VARCHAR;
    ALTER TABLE llm_calls ADD COLUMN tier VARCHAR
    """,
))

CONTROL.append((
    11,
    """
    ALTER TABLE tasks ADD COLUMN idempotency_key VARCHAR;
    CREATE INDEX tasks_idempotency ON tasks (workspace_id, created_by, idempotency_key)
    """,
))

CONTROL.append((
    12,
    """
    ALTER TABLE llm_calls ADD COLUMN step INTEGER
    """,
))

WORKSPACE.append((
    8,
    """
    ALTER TABLE query_history ADD COLUMN step INTEGER
    """,
))

WORKSPACE.append((
    9,
    """
    CREATE TABLE dataset_insights (
        dataset VARCHAR NOT NULL,
        version INTEGER NOT NULL,
        insights JSON NOT NULL,
        computed_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (dataset, version)
    );
    CREATE TABLE data_contracts (
        dataset VARCHAR NOT NULL,
        version INTEGER NOT NULL,
        spec JSON NOT NULL,
        status VARCHAR NOT NULL,
        note VARCHAR,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (dataset, version)
    );
    CREATE TABLE contract_checks (
        id VARCHAR PRIMARY KEY,
        dataset VARCHAR NOT NULL,
        contract_version INTEGER NOT NULL,
        dataset_version INTEGER NOT NULL,
        outcome VARCHAR NOT NULL,
        results JSON NOT NULL,
        impacted JSON NOT NULL,
        checked_by VARCHAR NOT NULL,
        checked_at TIMESTAMPTZ DEFAULT now()
    );
    ALTER TABLE metrics ADD COLUMN replaced_by VARCHAR
    """,
))

# Connector operations: every test, discovery and sync as an event with its normalized failure category, and the
# connection's own health state (last success, last failure, consecutive failures).
CONTROL.append((
    13,
    """
    CREATE TABLE connection_events (
        id VARCHAR PRIMARY KEY,
        connection_id VARCHAR NOT NULL,
        workspace_id VARCHAR NOT NULL,
        action VARCHAR NOT NULL,
        source_table VARCHAR,
        outcome VARCHAR NOT NULL,
        category VARCHAR,
        message VARCHAR,
        attempts INTEGER NOT NULL DEFAULT 1,
        retried JSON,
        ms INTEGER,
        details JSON,
        actor VARCHAR,
        occurred_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE INDEX connection_events_by_connection ON connection_events (connection_id, occurred_at);
    ALTER TABLE connections ADD COLUMN last_success_at TIMESTAMPTZ;
    ALTER TABLE connections ADD COLUMN last_failure_at TIMESTAMPTZ;
    ALTER TABLE connections ADD COLUMN failure_category VARCHAR;
    ALTER TABLE connections ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE connection_syncs ADD COLUMN last_schema JSON
    """,
))

# The asset registry: one identity, owner, version, validation and dependency record for every deliverable Lyst
# produces (reports, workbooks, decks, briefings, dashboards, workflows), and every version of each.
WORKSPACE.append((
    10,
    """
    CREATE TABLE assets (
        id VARCHAR PRIMARY KEY,
        kind VARCHAR NOT NULL,
        title VARCHAR NOT NULL,
        owner VARCHAR NOT NULL,
        visibility VARCHAR NOT NULL DEFAULT 'workspace',
        status VARCHAR NOT NULL DEFAULT 'active',
        health VARCHAR NOT NULL DEFAULT 'current',
        health_reason VARCHAR,
        validation VARCHAR NOT NULL,
        version INTEGER NOT NULL,
        ref_kind VARCHAR NOT NULL,
        ref_id VARCHAR NOT NULL,
        task_id VARCHAR,
        question VARCHAR,
        depends JSON NOT NULL,
        repair JSON,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now(),
        checked_at TIMESTAMPTZ
    );
    CREATE TABLE asset_versions (
        asset_id VARCHAR NOT NULL,
        version INTEGER NOT NULL,
        ref_id VARCHAR NOT NULL,
        sha256 VARCHAR,
        depends JSON NOT NULL,
        validation VARCHAR NOT NULL,
        note VARCHAR,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (asset_id, version)
    );
    CREATE INDEX assets_by_ref ON assets (ref_kind, ref_id)
    """,
))

# Collaboration: comments (with change requests and decisions), assignments, reviews of proposed changes, asset
# shares, personal notifications and per-person notification preferences.
WORKSPACE.append((
    11,
    """
    CREATE TABLE comments (
        id VARCHAR PRIMARY KEY,
        object_kind VARCHAR NOT NULL,
        object_id VARCHAR NOT NULL,
        parent_id VARCHAR,
        kind VARCHAR NOT NULL,
        body VARCHAR NOT NULL,
        mentions JSON NOT NULL,
        status VARCHAR NOT NULL,
        author VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        resolved_by VARCHAR,
        resolved_at TIMESTAMPTZ
    );
    CREATE INDEX comments_by_object ON comments (object_kind, object_id);
    CREATE TABLE assignments (
        id VARCHAR PRIMARY KEY,
        object_kind VARCHAR NOT NULL,
        object_id VARCHAR NOT NULL,
        assignee VARCHAR NOT NULL,
        assigned_by VARCHAR NOT NULL,
        note VARCHAR,
        due DATE,
        status VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        completed_at TIMESTAMPTZ
    );
    CREATE TABLE reviews (
        id VARCHAR PRIMARY KEY,
        object_kind VARCHAR NOT NULL,
        object_id VARCHAR NOT NULL,
        action VARCHAR NOT NULL,
        payload JSON NOT NULL,
        summary VARCHAR NOT NULL,
        requested_by VARCHAR NOT NULL,
        reviewer VARCHAR,
        min_role VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        decided_by VARCHAR,
        decision_note VARCHAR,
        created_at TIMESTAMPTZ DEFAULT now(),
        decided_at TIMESTAMPTZ
    );
    CREATE TABLE asset_shares (
        asset_id VARCHAR NOT NULL,
        user_id VARCHAR NOT NULL,
        shared_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (asset_id, user_id)
    );
    CREATE TABLE user_notifications (
        id VARCHAR PRIMARY KEY,
        user_id VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        object_kind VARCHAR,
        object_id VARCHAR,
        text VARCHAR NOT NULL,
        actor VARCHAR,
        status VARCHAR NOT NULL DEFAULT 'unread',
        created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE notification_prefs (
        user_id VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        enabled BOOLEAN NOT NULL,
        PRIMARY KEY (user_id, kind)
    )
    """,
))

# Operations: task priority and deadline, run time carried across resumes, calendar schedules (local time, weekdays,
# time zone) with overlap policy and a missed-run record, and per-workspace quotas (admission and model budgets).
CONTROL.append((
    20,
    """
    DROP INDEX tasks_idempotency;
    ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE tasks ADD COLUMN deadline_at TIMESTAMPTZ;
    ALTER TABLE tasks ADD COLUMN active_seconds DOUBLE NOT NULL DEFAULT 0;
    CREATE INDEX tasks_idempotency ON tasks (workspace_id, created_by, idempotency_key);
    ALTER TABLE schedules ADD COLUMN timezone VARCHAR;
    ALTER TABLE schedules ADD COLUMN at_time VARCHAR;
    ALTER TABLE schedules ADD COLUMN weekdays VARCHAR;
    ALTER TABLE schedules ADD COLUMN overlap VARCHAR NOT NULL DEFAULT 'skip';
    ALTER TABLE schedules ADD COLUMN last_run_at TIMESTAMPTZ;
    ALTER TABLE schedules ADD COLUMN last_task_id VARCHAR;
    ALTER TABLE schedules ADD COLUMN missed INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE schedules ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE workspace_quotas (
        workspace_id VARCHAR PRIMARY KEY,
        max_running INTEGER,
        max_queued INTEGER,
        monthly_spend_usd DOUBLE,
        monthly_tokens BIGINT,
        task_budget_seconds INTEGER,
        updated_by VARCHAR,
        updated_at TIMESTAMPTZ DEFAULT now()
    )
    """,
))

# Onboarding (this lane uses WORKSPACE 30-39 and CONTROL 30-39; numbers may be applied out of order).
# Proposals from discovery wait here until an administrator accepts or dismisses them; each discovery run is kept;
# the business profile (domain packages, the organization's terms, fiscal calendar, objectives) is versioned.
WORKSPACE.append((
    30,
    """
    CREATE TABLE onboarding_proposals (
        id VARCHAR PRIMARY KEY,
        kind VARCHAR NOT NULL,
        title VARCHAR NOT NULL,
        domain VARCHAR,
        status VARCHAR NOT NULL DEFAULT 'proposed',
        payload JSON NOT NULL,
        evidence JSON NOT NULL,
        result JSON,
        decided_by VARCHAR,
        decided_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE onboarding_discoveries (
        id VARCHAR PRIMARY KEY,
        domains JSON NOT NULL,
        datasets INTEGER NOT NULL,
        proposals INTEGER NOT NULL,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE domain_profiles (
        version INTEGER PRIMARY KEY,
        spec JSON NOT NULL,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now()
    )
    """,
))

# Transformations: derived datasets defined by a governed DuckDB query, verified before they are saved, refreshed
# when an input changes, versioned with every run recorded.
WORKSPACE.append((
    12,
    """
    CREATE TABLE transforms (
        name VARCHAR PRIMARY KEY,
        version INTEGER NOT NULL,
        title VARCHAR NOT NULL,
        description VARCHAR,
        sql VARCHAR NOT NULL,
        inputs JSON NOT NULL,
        key JSON NOT NULL,
        incremental_column VARCHAR,
        status VARCHAR NOT NULL,
        last_error VARCHAR,
        last_run_at TIMESTAMPTZ,
        owner VARCHAR NOT NULL,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE transform_versions (
        name VARCHAR NOT NULL,
        version INTEGER NOT NULL,
        sql VARCHAR NOT NULL,
        description VARCHAR,
        checks JSON NOT NULL,
        note VARCHAR,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (name, version)
    );
    CREATE TABLE transform_runs (
        id VARCHAR PRIMARY KEY,
        name VARCHAR NOT NULL,
        version INTEGER NOT NULL,
        trigger VARCHAR NOT NULL,
        outcome VARCHAR NOT NULL,
        mode VARCHAR NOT NULL,
        rows BIGINT,
        dataset_version INTEGER,
        checks JSON,
        error VARCHAR,
        ms INTEGER,
        actor VARCHAR,
        ran_at TIMESTAMPTZ DEFAULT now()
    )
    """,
))

# -- extensibility lane (migrations 40-49): ingestion, experiments, environments, publishing -------------------------
WORKSPACE.append((
    40,
    """
    CREATE TABLE ingest_jobs (
        id VARCHAR PRIMARY KEY,
        filename VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        format VARCHAR NOT NULL,
        size_bytes BIGINT NOT NULL,
        sha256 VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        plan JSON NOT NULL,
        result JSON,
        content BLOB,
        classification VARCHAR NOT NULL DEFAULT 'internal',
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        finished_at TIMESTAMPTZ
    )
    """,
))
CONTROL.append((
    40,
    """
    CREATE TABLE platform_events (
        seq BIGINT PRIMARY KEY,
        id VARCHAR UNIQUE NOT NULL,
        type VARCHAR NOT NULL,
        workspace_id VARCHAR,
        occurred_at TIMESTAMPTZ NOT NULL,
        actor VARCHAR,
        subject_kind VARCHAR,
        subject_id VARCHAR,
        data JSON NOT NULL,
        source VARCHAR NOT NULL
    );
    CREATE SEQUENCE platform_event_seq;
    CREATE INDEX platform_events_by_ws ON platform_events (workspace_id, seq);
    CREATE TABLE event_cursors (
        name VARCHAR PRIMARY KEY,
        position VARCHAR NOT NULL
    );
    CREATE TABLE webhook_subscriptions (
        id VARCHAR PRIMARY KEY,
        workspace_id VARCHAR NOT NULL,
        url VARCHAR NOT NULL,
        types JSON NOT NULL,
        secret VARCHAR NOT NULL,
        description VARCHAR,
        active BOOLEAN NOT NULL DEFAULT true,
        disabled_reason VARCHAR,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        after_seq BIGINT NOT NULL DEFAULT 0
    );
    CREATE TABLE webhook_deliveries (
        id VARCHAR PRIMARY KEY,
        subscription_id VARCHAR NOT NULL,
        event_seq BIGINT NOT NULL,
        status VARCHAR NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TIMESTAMPTZ DEFAULT now(),
        last_status INTEGER,
        last_error VARCHAR,
        created_at TIMESTAMPTZ DEFAULT now(),
        delivered_at TIMESTAMPTZ,
        UNIQUE (subscription_id, event_seq)
    )
    """,
))

# Anticipation (migrations 50-59): prepared-work objects with their signals, scores, preparation, verification, validity
# and outcome; an append-only event trail per object; the workspace's autonomy level and budgets; per-day usage; and each
# person's attention preferences.
WORKSPACE.append((
    50,
    """
    CREATE TABLE anticipations (
        id VARCHAR PRIMARY KEY,
        kind VARCHAR NOT NULL,
        objective VARCHAR NOT NULL,
        subject_kind VARCHAR NOT NULL,
        subject_id VARCHAR NOT NULL,
        audience JSON NOT NULL,
        signals JSON NOT NULL,
        reasons JSON NOT NULL,
        scores JSON NOT NULL,
        score DOUBLE NOT NULL,
        level_required INTEGER NOT NULL,
        status VARCHAR NOT NULL,
        verdict VARCHAR,
        prepared JSON,
        verification JSON,
        actions JSON NOT NULL,
        depends JSON NOT NULL,
        resources JSON NOT NULL,
        needed_at TIMESTAMPTZ,
        valid_until TIMESTAMPTZ,
        revision INTEGER NOT NULL DEFAULT 1,
        dedupe_key VARCHAR UNIQUE NOT NULL,
        finding_id VARCHAR,
        task_id VARCHAR,
        snoozed_until TIMESTAMPTZ,
        outcome VARCHAR,
        decided_by VARCHAR,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now(),
        prepared_at TIMESTAMPTZ,
        surfaced_at TIMESTAMPTZ,
        decided_at TIMESTAMPTZ
    );
    CREATE INDEX anticipations_by_status ON anticipations (status, kind);
    CREATE SEQUENCE anticipation_event_seq;
    CREATE TABLE anticipation_events (
        id BIGINT PRIMARY KEY,
        anticipation_id VARCHAR NOT NULL,
        event VARCHAR NOT NULL,
        actor VARCHAR NOT NULL,
        detail JSON NOT NULL,
        occurred_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE INDEX anticipation_events_by_object ON anticipation_events (anticipation_id);
    CREATE TABLE anticipation_settings (
        id VARCHAR PRIMARY KEY,
        level INTEGER NOT NULL,
        daily_prepare_limit INTEGER NOT NULL,
        daily_investigation_limit INTEGER NOT NULL,
        daily_compute_ms BIGINT NOT NULL,
        daily_surface_limit INTEGER NOT NULL,
        updated_by VARCHAR,
        updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE anticipation_usage (
        day DATE NOT NULL,
        operation VARCHAR NOT NULL,
        count INTEGER NOT NULL,
        compute_ms BIGINT NOT NULL,
        refused INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, operation)
    );
    CREATE TABLE attention_prefs (
        user_id VARCHAR PRIMARY KEY,
        opted_out BOOLEAN NOT NULL DEFAULT false,
        min_score DOUBLE,
        max_per_day INTEGER,
        quiet_start VARCHAR,
        quiet_end VARCHAR,
        timezone VARCHAR,
        muted_kinds JSON NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT now()
    )
    """,
))

# Scenario models (versioned driver models), saved scenarios with their result and a reproducibility digest, and
# decision records (analysis, recommendation, the person's choice, approval and outcome review).
WORKSPACE.append((
    20,
    """
    CREATE TABLE scenario_models (
        name VARCHAR NOT NULL,
        version INTEGER NOT NULL,
        title VARCHAR NOT NULL,
        spec JSON NOT NULL,
        note VARCHAR,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (name, version)
    );
    CREATE TABLE scenarios (
        id VARCHAR PRIMARY KEY,
        model VARCHAR NOT NULL,
        model_version INTEGER NOT NULL,
        name VARCHAR NOT NULL,
        version INTEGER NOT NULL,
        overrides JSON NOT NULL,
        result JSON NOT NULL,
        digest VARCHAR NOT NULL,
        seed INTEGER,
        note VARCHAR,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE decisions (
        id VARCHAR PRIMARY KEY,
        title VARCHAR NOT NULL,
        question VARCHAR,
        spec JSON NOT NULL,
        analysis JSON,
        recommendation JSON,
        status VARCHAR NOT NULL,
        chosen VARCHAR,
        rationale VARCHAR,
        approval_id VARCHAR,
        decided_by VARCHAR,
        decided_at TIMESTAMPTZ,
        review_at TIMESTAMPTZ,
        outcome JSON,
        task_id VARCHAR,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now()
    )
    """,
))

# Data classification history and the workspace's versioned data policy; the query log records which datasets each
# query read, so a task's sensitivity is known before anything leaves (external model, exported file).
WORKSPACE.append((
    31,
    """
    CREATE SEQUENCE classification_seq;
    CREATE TABLE classification_events (
        id BIGINT PRIMARY KEY,
        dataset VARCHAR NOT NULL,
        level VARCHAR NOT NULL,
        previous VARCHAR,
        tags JSON NOT NULL,
        reason VARCHAR NOT NULL,
        actor VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE data_policies (
        version INTEGER PRIMARY KEY,
        spec JSON NOT NULL,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now()
    );
    ALTER TABLE query_history ADD COLUMN datasets JSON
    """,
))

# Source precedence: which dataset is the authoritative source for a subject, and which are secondary copies.
WORKSPACE.append((
    13,
    """
    CREATE TABLE source_authority (
        dataset VARCHAR PRIMARY KEY,
        level VARCHAR NOT NULL,
        subject VARCHAR,
        note VARCHAR,
        set_by VARCHAR NOT NULL,
        set_at TIMESTAMPTZ DEFAULT now()
    )
    """,
))

# Living project specifications: requirements with acceptance criteria, traced to the tasks, artifacts, assets and
# decisions that satisfy them; every revision is a version.
WORKSPACE.append((
    32,
    """
    CREATE TABLE project_specs (
        id VARCHAR PRIMARY KEY,
        title VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        version INTEGER NOT NULL,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE spec_versions (
        spec_id VARCHAR NOT NULL,
        version INTEGER NOT NULL,
        body JSON NOT NULL,
        note VARCHAR,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (spec_id, version)
    )
    """,
))

WORKSPACE.append((
    41,
    """
    CREATE TABLE experiments (
        id VARCHAR PRIMARY KEY,
        name VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        question VARCHAR,
        spec JSON NOT NULL,
        status VARCHAR NOT NULL,
        decision JSON,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE experiment_runs (
        id VARCHAR PRIMARY KEY,
        experiment_id VARCHAR NOT NULL,
        method VARCHAR NOT NULL,
        params JSON NOT NULL,
        status VARCHAR NOT NULL,
        metrics JSON,
        detail JSON,
        inputs JSON NOT NULL,
        environment JSON NOT NULL,
        input_digest VARCHAR NOT NULL,
        result_digest VARCHAR,
        seconds DOUBLE,
        error VARCHAR,
        reproduces VARCHAR,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE INDEX experiment_runs_by_experiment ON experiment_runs (experiment_id)
    """,
))
WORKSPACE.append((
    42,
    """
    CREATE TABLE workbench_runs (
        id VARCHAR PRIMARY KEY,
        kind VARCHAR NOT NULL,
        code VARCHAR NOT NULL,
        inputs JSON NOT NULL,
        environment JSON NOT NULL,
        backend VARCHAR,
        status VARCHAR NOT NULL,
        error VARCHAR,
        stdout VARCHAR,
        result_kind VARCHAR,
        result JSON,
        input_digest VARCHAR NOT NULL,
        result_digest VARCHAR,
        seconds DOUBLE,
        reproduces VARCHAR,
        saved_as VARCHAR,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now()
    )
    """,
))

# Connector drafts: connections Lyst proposes from an API description, tested in a dry run and promoted by an admin.
CONTROL.append((
    14,
    """
    CREATE TABLE connector_drafts (
        id VARCHAR PRIMARY KEY,
        workspace_id VARCHAR NOT NULL,
        name VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        proposal JSON NOT NULL,
        status VARCHAR NOT NULL,
        test_report JSON,
        connection_id VARCHAR,
        created_by VARCHAR NOT NULL,
        reviewed_by VARCHAR,
        review_note VARCHAR,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now()
    )
    """,
))

# Voice (migrations 60-69): workspace voice settings, each person's voice preferences, voice sessions and their turns.
# Audio is stored only when an administrator turns on retention; transcripts are redacted before they are written.
WORKSPACE.append((
    60,
    """
    CREATE TABLE voice_settings (
        id VARCHAR PRIMARY KEY,
        spec JSON NOT NULL,
        updated_by VARCHAR,
        updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE voice_prefs (
        user_id VARCHAR PRIMARY KEY,
        spec JSON NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE voice_sessions (
        id VARCHAR PRIMARY KEY,
        user_id VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        mode VARCHAR NOT NULL,
        turns INTEGER NOT NULL DEFAULT 0,
        last_task_id VARCHAR,
        started_at TIMESTAMPTZ DEFAULT now(),
        last_activity TIMESTAMPTZ,
        ended_at TIMESTAMPTZ
    );
    CREATE TABLE voice_turns (
        id VARCHAR PRIMARY KEY,
        session_id VARCHAR NOT NULL,
        seq INTEGER NOT NULL,
        role VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        text VARCHAR NOT NULL,
        intent VARCHAR,
        task_id VARCHAR,
        outcome VARCHAR,
        engines JSON,
        audio BLOB,
        audio_mime VARCHAR,
        created_at TIMESTAMPTZ DEFAULT now(),
        UNIQUE (session_id, seq)
    )
    """,
))

# UI lane (migrations 80-89): what each person pins (an artifact, a dataset, a metric) for one-click return.
WORKSPACE.append((
    80,
    """
    CREATE TABLE ui_pins (
        user_id VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        ref_id VARCHAR NOT NULL,
        title VARCHAR,
        created_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (user_id, kind, ref_id)
    )
    """,
))

# Graph lane (migrations 90-99): graph runs. A Planner splits a goal into branches; a Builder per branch does the work
# as an ordinary task; a separate Checker grades it against the branch's requirements; a Router moves it on (pass),
# sends it back with the error (fail, at most max_attempts times) or marks it blocked. Every routed edge is kept, so
# the graph can be shown as it ran. Two return paths: the correction edge sends one unit back with a scoped packet, and
# the learning edge turns a repeated failure (avoid) or an accepted fix (rewrite) into a rule the Planner applies.
WORKSPACE.append((
    90,
    """
    CREATE TABLE graph_runs (
        id VARCHAR PRIMARY KEY,
        title VARCHAR NOT NULL,
        goal VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        created_by VARCHAR NOT NULL,
        intake VARCHAR NOT NULL DEFAULT '{}',
        max_attempts INTEGER NOT NULL DEFAULT 3,
        rerun_of VARCHAR,
        approved_by VARCHAR,
        final VARCHAR,
        merged VARCHAR,
        budget_minutes INTEGER NOT NULL DEFAULT 120,
        created_at TIMESTAMPTZ DEFAULT now(),
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE graph_branches (
        run_id VARCHAR NOT NULL,
        id VARCHAR NOT NULL,
        seq INTEGER NOT NULL,
        title VARCHAR NOT NULL,
        job VARCHAR NOT NULL,
        job_key VARCHAR NOT NULL,
        requirements VARCHAR NOT NULL DEFAULT '[]',
        status VARCHAR NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        task_id VARCHAR,
        last_reason VARCHAR,
        evidence VARCHAR,
        warning VARCHAR,
        lane VARCHAR NOT NULL DEFAULT 'contained',
        after VARCHAR NOT NULL DEFAULT '[]',
        original_job VARCHAR,
        hold VARCHAR,
        optional BOOLEAN NOT NULL DEFAULT false,
        updated_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (run_id, id)
    );
    CREATE TABLE graph_attempts (
        run_id VARCHAR NOT NULL,
        branch_id VARCHAR NOT NULL,
        attempt INTEGER NOT NULL,
        task_id VARCHAR,
        outcome VARCHAR,
        reason VARCHAR,
        signature VARCHAR,
        checks VARCHAR,
        packet VARCHAR,
        started_at TIMESTAMPTZ DEFAULT now(),
        checked_at TIMESTAMPTZ,
        PRIMARY KEY (run_id, branch_id, attempt)
    );
    CREATE SEQUENCE graph_edge_seq;
    CREATE TABLE graph_edges (
        seq BIGINT PRIMARY KEY,
        run_id VARCHAR NOT NULL,
        branch_id VARCHAR,
        src VARCHAR NOT NULL,
        dst VARCHAR NOT NULL,
        edge VARCHAR NOT NULL,
        detail VARCHAR,
        occurred_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE graph_rules (
        id VARCHAR PRIMARY KEY,
        edge VARCHAR NOT NULL,
        signature VARCHAR NOT NULL,
        job_key VARCHAR NOT NULL,
        kind VARCHAR NOT NULL DEFAULT 'avoid',
        rule VARCHAR NOT NULL,
        rewrite VARCHAR,
        source_run VARCHAR NOT NULL,
        source_branch VARCHAR NOT NULL,
        hits INTEGER NOT NULL DEFAULT 0,
        active BOOLEAN NOT NULL DEFAULT true,
        created_at TIMESTAMPTZ DEFAULT now(),
        retired_by VARCHAR,
        retired_at TIMESTAMPTZ,
        UNIQUE (edge, signature, job_key)
    )
    """,
))
WORKSPACE.append((
    43,
    """
    CREATE TABLE publications (
        id VARCHAR PRIMARY KEY,
        title VARCHAR NOT NULL,
        audience VARCHAR NOT NULL,
        min_role VARCHAR NOT NULL DEFAULT 'viewer',
        link_hash VARCHAR UNIQUE,
        expires_at TIMESTAMPTZ,
        status VARCHAR NOT NULL,
        current_version INTEGER NOT NULL,
        views INTEGER NOT NULL DEFAULT 0,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        revoked_by VARCHAR,
        revoked_at TIMESTAMPTZ
    );
    CREATE TABLE publication_versions (
        publication_id VARCHAR NOT NULL,
        version INTEGER NOT NULL,
        task_id VARCHAR NOT NULL,
        markdown VARCHAR NOT NULL,
        html VARCHAR NOT NULL,
        sha256 VARCHAR NOT NULL,
        datasets JSON NOT NULL,
        note VARCHAR,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (publication_id, version)
    );
    CREATE TABLE publication_views (
        publication_id VARCHAR NOT NULL,
        version INTEGER NOT NULL,
        viewer VARCHAR NOT NULL,
        viewed_at TIMESTAMPTZ DEFAULT now()
    )
    """,
))
CONTROL.append((
    41,
    """
    CREATE TABLE publication_links (
        link_hash VARCHAR PRIMARY KEY,
        workspace_id VARCHAR NOT NULL,
        publication_id VARCHAR NOT NULL
    )
    """,
))

# Harness lane (migrations 100-109): a frozen contract per task (goal, inputs, output, constraints, done_when), one
# immutable change receipt per run, every classified failure, and the harness rules proposed from repeated failures.
# A rule is proposed, then active only once a person approves it, and retired by a person (docs/HARNESS.md).
CONTROL.append((
    100,
    """
    CREATE TABLE task_contracts (
        task_id VARCHAR PRIMARY KEY,
        workspace_id VARCHAR NOT NULL,
        job_key VARCHAR NOT NULL,
        contract JSON NOT NULL,
        sha256 VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE task_receipts (
        task_id VARCHAR NOT NULL,
        run INTEGER NOT NULL,
        workspace_id VARCHAR NOT NULL,
        state VARCHAR NOT NULL,
        contract_met BOOLEAN,
        receipt JSON NOT NULL,
        sha256 VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (task_id, run)
    );
    CREATE TABLE harness_failures (
        id VARCHAR PRIMARY KEY,
        workspace_id VARCHAR NOT NULL,
        task_id VARCHAR NOT NULL,
        run INTEGER NOT NULL,
        job_key VARCHAR NOT NULL,
        failure_class VARCHAR NOT NULL,
        fix_kind VARCHAR,
        capability VARCHAR,
        signature VARCHAR NOT NULL,
        evidence VARCHAR,
        rule_id VARCHAR,
        created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE harness_rules (
        id VARCHAR PRIMARY KEY,
        workspace_id VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        job_key VARCHAR NOT NULL,
        capability VARCHAR,
        signature VARCHAR NOT NULL,
        text VARCHAR NOT NULL,
        spec JSON NOT NULL,
        status VARCHAR NOT NULL,
        source_failures JSON NOT NULL,
        approval_id VARCHAR,
        memory_id VARCHAR,
        hits INTEGER NOT NULL DEFAULT 0,
        proposed_by VARCHAR,
        decided_by VARCHAR,
        retired_by VARCHAR,
        created_at TIMESTAMPTZ DEFAULT now(),
        decided_at TIMESTAMPTZ,
        retired_at TIMESTAMPTZ
    );
    CREATE INDEX harness_failures_job ON harness_failures (workspace_id, job_key, signature)
    """,
))

# Events lane (110-119). The canonical envelope (correlation, causation, depth, sensitivity, dedupe key, related
# objects, type version) on the platform event stream, and durable consumers: a cursor per consumer and an inbox
# recording how each event was processed (done, retry with its next attempt, dead), so delivery is at least once and
# a redelivered event is never handled twice.
CONTROL.append((
    110,
    """
    ALTER TABLE platform_events ADD COLUMN type_version INTEGER DEFAULT 1;
    ALTER TABLE platform_events ADD COLUMN correlation_id VARCHAR;
    ALTER TABLE platform_events ADD COLUMN causation_id VARCHAR;
    ALTER TABLE platform_events ADD COLUMN depth INTEGER DEFAULT 0;
    ALTER TABLE platform_events ADD COLUMN sensitivity VARCHAR DEFAULT 'internal';
    ALTER TABLE platform_events ADD COLUMN dedupe_key VARCHAR;
    ALTER TABLE platform_events ADD COLUMN related JSON;
    CREATE INDEX platform_events_by_correlation ON platform_events (workspace_id, correlation_id);
    CREATE INDEX platform_events_by_dedupe ON platform_events (workspace_id, dedupe_key);
    CREATE TABLE event_consumers (
        name VARCHAR PRIMARY KEY,
        position BIGINT NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE event_inbox (
        consumer VARCHAR NOT NULL,
        event_id VARCHAR NOT NULL,
        workspace_id VARCHAR,
        seq BIGINT NOT NULL,
        status VARCHAR NOT NULL,
        attempts INTEGER NOT NULL,
        last_error VARCHAR,
        next_attempt_at TIMESTAMPTZ,
        duration_ms INTEGER,
        updated_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (consumer, event_id)
    );
    CREATE INDEX event_inbox_due ON event_inbox (consumer, status, next_attempt_at)
    """,
))
# The transactional outbox: a service stages an event in the same workspace transaction as the change it describes;
# the collector publishes it to the control stream and stamps published_at.
WORKSPACE.append((
    110,
    """
    CREATE SEQUENCE event_outbox_seq;
    CREATE TABLE event_outbox (
        n BIGINT DEFAULT nextval('event_outbox_seq'),
        id VARCHAR PRIMARY KEY,
        type VARCHAR NOT NULL,
        type_version INTEGER NOT NULL DEFAULT 1,
        occurred_at TIMESTAMPTZ DEFAULT now(),
        actor VARCHAR,
        subject_kind VARCHAR NOT NULL,
        subject_id VARCHAR NOT NULL,
        data JSON NOT NULL,
        source VARCHAR NOT NULL,
        correlation_id VARCHAR,
        causation_id VARCHAR,
        depth INTEGER NOT NULL DEFAULT 0,
        sensitivity VARCHAR NOT NULL DEFAULT 'internal',
        dedupe_key VARCHAR,
        related JSON,
        published_at TIMESTAMPTZ
    );
    CREATE INDEX event_outbox_pending ON event_outbox (published_at, n);
    CREATE INDEX event_outbox_dedupe ON event_outbox (dedupe_key)
    """,
))
# Event rules (typed, versioned event-condition-action) and the activities they open, each with its lifecycle stages;
# rule_evaluations records every decision a rule made and why (kept 30 days).
WORKSPACE.append((
    111,
    """
    CREATE TABLE event_rules (
        id VARCHAR PRIMARY KEY,
        name VARCHAR NOT NULL,
        current_version INTEGER NOT NULL,
        status VARCHAR NOT NULL,
        owner VARCHAR NOT NULL,
        approval_id VARCHAR,
        hits INTEGER NOT NULL DEFAULT 0,
        last_matched_at TIMESTAMPTZ,
        activated_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE event_rule_versions (
        rule_id VARCHAR NOT NULL,
        version INTEGER NOT NULL,
        spec JSON NOT NULL,
        created_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (rule_id, version)
    );
    CREATE SEQUENCE rule_evaluation_seq;
    CREATE TABLE rule_evaluations (
        id BIGINT DEFAULT nextval('rule_evaluation_seq') PRIMARY KEY,
        rule_id VARCHAR NOT NULL,
        rule_version INTEGER NOT NULL,
        event_id VARCHAR NOT NULL,
        event_seq BIGINT,
        decision VARCHAR NOT NULL,
        why VARCHAR,
        evaluated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE TABLE activities (
        id VARCHAR PRIMARY KEY,
        rule_id VARCHAR NOT NULL,
        rule_version INTEGER NOT NULL,
        event_id VARCHAR NOT NULL,
        event_seq BIGINT,
        correlation_id VARCHAR,
        depth INTEGER NOT NULL DEFAULT 0,
        subject_kind VARCHAR NOT NULL,
        subject_id VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        action VARCHAR NOT NULL,
        autonomy VARCHAR NOT NULL,
        risk VARCHAR NOT NULL,
        priority INTEGER NOT NULL,
        explanation VARCHAR NOT NULL,
        stages JSON NOT NULL,
        owner VARCHAR NOT NULL,
        sensitivity VARCHAR NOT NULL DEFAULT 'internal',
        task_id VARCHAR,
        approval_id VARCHAR,
        outcome JSON,
        dedupe_key VARCHAR UNIQUE NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE INDEX activities_by_subject ON activities (subject_kind, subject_id, created_at);
    CREATE INDEX activities_by_task ON activities (task_id);
    CREATE INDEX rule_evaluations_by_rule ON rule_evaluations (rule_id, id)
    """,
))

# Learning lane (120-129). A person's verdict on an answer (lyst.feedback): the analysis carries its standing status
# (open, confirmed, disputed), a disputed answer's episodic memory leaves recall (disputed_at), and every verdict is
# kept with who gave it, whether it governs reuse (editor or above), and when it was withdrawn.
WORKSPACE.append((
    120,
    """
    ALTER TABLE analyses ADD COLUMN status VARCHAR DEFAULT 'open';
    ALTER TABLE memory ADD COLUMN disputed_at TIMESTAMPTZ;
    CREATE TABLE answer_feedback (
        id VARCHAR PRIMARY KEY,
        analysis_id VARCHAR NOT NULL,
        task_id VARCHAR,
        verdict VARCHAR NOT NULL,
        note VARCHAR,
        correction VARCHAR,
        correction_memory_id VARCHAR,
        governs BOOLEAN NOT NULL,
        given_by VARCHAR NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(),
        withdrawn_at TIMESTAMPTZ,
        withdrawn_by VARCHAR
    );
    CREATE INDEX answer_feedback_by_analysis ON answer_feedback (analysis_id, withdrawn_at)
    """,
))
