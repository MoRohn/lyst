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
    5,
    """
    ALTER TABLE api_tokens ADD COLUMN expires_at TIMESTAMPTZ;
    ALTER TABLE api_tokens ADD COLUMN last_used_at TIMESTAMPTZ;
    ALTER TABLE users ADD COLUMN subject VARCHAR;
    ALTER TABLE users ADD COLUMN last_login_at TIMESTAMPTZ
    """,
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
]
