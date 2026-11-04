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
]
