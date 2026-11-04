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
