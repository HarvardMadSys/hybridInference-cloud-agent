-- Schema-only fixture: pre-baseline revision 0025_retained_followup_gate.
-- No production rows, owners, privileges or credentials are included.
--
-- PostgreSQL database dump
--


-- Dumped from database version 16.15
-- Dumped by pg_dump version 16.15

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: assign_agent_job_native_turn_no(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.assign_agent_job_native_turn_no() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NEW.thread_id IS NULL OR NEW.native_turn_no IS NOT NULL THEN
                RETURN NEW;
            END IF;
            IF NEW.state <> 'succeeded'
               AND NOT EXISTS (
                    SELECT 1
                    FROM agent_sandbox_snapshots snapshot
                    WHERE snapshot.included_job_ids ? NEW.id
               )
               AND NOT EXISTS (
                    SELECT 1
                    FROM agent_jobs source
                    WHERE source.id = NEW.fork_source_job_id
                      AND source.native_turn_no IS NOT NULL
               ) THEN
                RETURN NEW;
            END IF;

            PERFORM pg_advisory_xact_lock(
                hashtextextended('agent-native-turn:' || NEW.thread_id, 0)
            );
            SELECT COUNT(*) + 1
            INTO NEW.native_turn_no
            FROM agent_jobs job
            WHERE job.thread_id = NEW.thread_id
              AND job.id <> NEW.id
              AND (job.turn_no, job.created_at, job.id)
                    < (NEW.turn_no, NEW.created_at, NEW.id)
              AND (
                    job.native_turn_no IS NOT NULL
                    OR job.state = 'succeeded'
                    OR EXISTS (
                        SELECT 1
                        FROM agent_sandbox_snapshots snapshot
                        WHERE snapshot.included_job_ids ? job.id
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM agent_jobs source
                        WHERE source.id = job.fork_source_job_id
                          AND source.native_turn_no IS NOT NULL
                    )
              );
            RETURN NEW;
        END;
        $$;


--
-- Name: assign_snapshot_anchor_native_turn_no(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.assign_snapshot_anchor_native_turn_no() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            UPDATE agent_jobs
            SET native_turn_no = native_turn_no
            WHERE id = NEW.anchor_job_id
              AND native_turn_no IS NULL;
            RETURN NEW;
        END;
        $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agent_admin_audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_admin_audit_log (
    id bigint NOT NULL,
    action text NOT NULL,
    admin_ip text NOT NULL,
    target_user_id text,
    details jsonb,
    success boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_admin_audit_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_admin_audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_admin_audit_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_admin_audit_log_id_seq OWNED BY public.agent_admin_audit_log.id;


--
-- Name: agent_attempt_resources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_attempt_resources (
    attempt_id bigint NOT NULL,
    job_id text NOT NULL,
    cpu_usec bigint,
    mem_peak_bytes bigint,
    mem_avg_bytes bigint,
    io_read_bytes bigint,
    io_write_bytes bigint,
    net_rx_bytes bigint,
    net_tx_bytes bigint,
    sample_count integer DEFAULT 0 NOT NULL,
    coverage double precision DEFAULT 0 NOT NULL,
    sampler_version text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    tool_calls_truncated boolean DEFAULT false NOT NULL,
    tool_time_ms bigint,
    process_observer text,
    process_observation_complete boolean DEFAULT false NOT NULL,
    process_events_lost bigint DEFAULT 0 NOT NULL,
    processes_truncated boolean DEFAULT false NOT NULL,
    CONSTRAINT agent_attempt_resources_coverage_check CHECK (((coverage >= (0)::double precision) AND (coverage <= (1)::double precision)))
);


--
-- Name: agent_attempts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_attempts (
    id bigint NOT NULL,
    job_id text NOT NULL,
    attempt_no integer NOT NULL,
    lease_owner text NOT NULL,
    lease_generation bigint NOT NULL,
    lease_expires_at timestamp with time zone NOT NULL,
    terminal_ready boolean DEFAULT false NOT NULL,
    sandbox_id text,
    base_sha text,
    status text DEFAULT 'running'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    host_id text,
    grant_id text,
    sandbox_runtime text,
    sandbox_image_digest text,
    runtime_version text,
    continuity_mode text,
    host_kernel text,
    guest_kernel text,
    context_status text,
    CONSTRAINT agent_attempts_context_status_check CHECK ((context_status = ANY (ARRAY['new'::text, 'live'::text, 'restored'::text, 'lost'::text]))),
    CONSTRAINT agent_attempts_continuity_mode_check CHECK ((continuity_mode = ANY (ARRAY['hot'::text, 'warm_native'::text, 'cold_native'::text, 'replay'::text])))
);


--
-- Name: agent_attempts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_attempts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_attempts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_attempts_id_seq OWNED BY public.agent_attempts.id;


--
-- Name: agent_gitlab_connections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_gitlab_connections (
    id bigint NOT NULL,
    user_id text NOT NULL,
    external_user_id bigint NOT NULL,
    username text NOT NULL,
    display_name text,
    web_url text,
    access_token_ciphertext text NOT NULL,
    refresh_token_ciphertext text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_gitlab_connections_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_gitlab_connections_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_gitlab_connections_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_gitlab_connections_id_seq OWNED BY public.agent_gitlab_connections.id;


--
-- Name: agent_host_enroll_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_host_enroll_tokens (
    token_hash text NOT NULL,
    issued_by text,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    consumed_host text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    host text
);


--
-- Name: agent_job_artifacts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_job_artifacts (
    id bigint NOT NULL,
    job_id text NOT NULL,
    attempt_id bigint NOT NULL,
    kind text NOT NULL,
    content text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_job_artifacts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_job_artifacts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_job_artifacts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_job_artifacts_id_seq OWNED BY public.agent_job_artifacts.id;


--
-- Name: agent_job_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_job_events (
    id bigint NOT NULL,
    job_id text NOT NULL,
    attempt_id bigint NOT NULL,
    seq integer NOT NULL,
    event_type text NOT NULL,
    payload jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    client_seq bigint
);


--
-- Name: agent_job_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_job_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_job_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_job_events_id_seq OWNED BY public.agent_job_events.id;


--
-- Name: agent_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_jobs (
    id text NOT NULL,
    thread_id text,
    parent_job_id text,
    turn_no integer DEFAULT 1 NOT NULL,
    user_id text NOT NULL,
    repo text NOT NULL,
    base_sha text,
    task_prompt text NOT NULL,
    setup_script text,
    runtime text NOT NULL,
    model text NOT NULL,
    state text DEFAULT 'queued'::text NOT NULL,
    cancel_requested boolean DEFAULT false NOT NULL,
    terminal_resume_pending boolean DEFAULT false NOT NULL,
    current_attempt_id bigint,
    published_pr_url text,
    published_commit_sha text,
    detail text,
    metadata jsonb,
    mcp_servers text[],
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    fork_source_job_id text,
    reasoning_effort text,
    execution_profile text DEFAULT 'trusted-v1'::text NOT NULL,
    native_turn_no integer,
    retained_followup_eligible boolean,
    CONSTRAINT agent_jobs_execution_profile_check CHECK ((execution_profile = ANY (ARRAY['trusted-v1'::text, 'untrusted-kata-v1'::text]))),
    CONSTRAINT ck_agent_jobs_native_turn_no_positive CHECK ((native_turn_no > 0))
);


--
-- Name: agent_mcp_connections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_mcp_connections (
    id bigint NOT NULL,
    user_id text NOT NULL,
    server text NOT NULL,
    account_label text,
    access_token_ciphertext text NOT NULL,
    refresh_token_ciphertext text,
    expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_mcp_connections_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_mcp_connections_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_mcp_connections_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_mcp_connections_id_seq OWNED BY public.agent_mcp_connections.id;


--
-- Name: agent_native_session_edges; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_native_session_edges (
    id bigint NOT NULL,
    operation_id text NOT NULL,
    sandbox_id text NOT NULL,
    source_thread_id text NOT NULL,
    target_thread_id text NOT NULL,
    source_native_session_id text NOT NULL,
    target_native_session_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_native_session_edges_check CHECK ((source_native_session_id <> target_native_session_id))
);


--
-- Name: agent_native_session_edges_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_native_session_edges_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_native_session_edges_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_native_session_edges_id_seq OWNED BY public.agent_native_session_edges.id;


--
-- Name: agent_oauth_states; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_oauth_states (
    state_hash text NOT NULL,
    user_id text NOT NULL,
    provider text NOT NULL,
    code_verifier_ciphertext text,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_oauth_states_provider_check CHECK ((provider <> ''::text))
);


--
-- Name: agent_process_observations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_process_observations (
    attempt_id bigint NOT NULL,
    process_seq integer NOT NULL,
    uid bigint NOT NULL,
    pid bigint NOT NULL,
    tgid bigint NOT NULL,
    ppid bigint,
    comm text NOT NULL,
    executable text,
    started_offset_ms bigint NOT NULL,
    duration_ms bigint,
    exit_code integer,
    observed_fork boolean DEFAULT false NOT NULL,
    observed_exec boolean DEFAULT false NOT NULL,
    observed_exit boolean DEFAULT false NOT NULL,
    tool_call_seqs integer[] DEFAULT '{}'::integer[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    cpu_usec bigint,
    mem_peak_bytes bigint,
    io_read_bytes bigint,
    io_write_bytes bigint,
    io_rchar_bytes bigint,
    io_wchar_bytes bigint,
    CONSTRAINT agent_process_observations_check CHECK (((pid > 0) AND (tgid > 0))),
    CONSTRAINT agent_process_observations_cpu_usec_check CHECK (((cpu_usec IS NULL) OR (cpu_usec >= 0))),
    CONSTRAINT agent_process_observations_duration_ms_check CHECK (((duration_ms IS NULL) OR (duration_ms >= 0))),
    CONSTRAINT agent_process_observations_exit_code_check CHECK (((exit_code IS NULL) OR ((exit_code >= 0) AND (exit_code <= 255)))),
    CONSTRAINT agent_process_observations_io_rchar_bytes_check CHECK (((io_rchar_bytes IS NULL) OR (io_rchar_bytes >= 0))),
    CONSTRAINT agent_process_observations_io_read_bytes_check CHECK (((io_read_bytes IS NULL) OR (io_read_bytes >= 0))),
    CONSTRAINT agent_process_observations_io_wchar_bytes_check CHECK (((io_wchar_bytes IS NULL) OR (io_wchar_bytes >= 0))),
    CONSTRAINT agent_process_observations_io_write_bytes_check CHECK (((io_write_bytes IS NULL) OR (io_write_bytes >= 0))),
    CONSTRAINT agent_process_observations_mem_peak_bytes_check CHECK (((mem_peak_bytes IS NULL) OR (mem_peak_bytes >= 0))),
    CONSTRAINT agent_process_observations_ppid_check CHECK (((ppid IS NULL) OR (ppid > 0))),
    CONSTRAINT agent_process_observations_started_offset_ms_check CHECK ((started_offset_ms >= 0)),
    CONSTRAINT agent_process_observations_uid_check CHECK ((uid >= 0))
);


--
-- Name: agent_repo_grants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_repo_grants (
    id bigint NOT NULL,
    user_id text NOT NULL,
    installation_id bigint NOT NULL,
    account_login text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_repo_grants_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_repo_grants_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_repo_grants_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_repo_grants_id_seq OWNED BY public.agent_repo_grants.id;


--
-- Name: agent_runner_hosts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_runner_hosts (
    host text NOT NULL,
    last_worker_id text,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'online'::text NOT NULL,
    labels jsonb DEFAULT '{}'::jsonb NOT NULL,
    capabilities jsonb DEFAULT '[]'::jsonb NOT NULL,
    slots_total integer,
    slots_used integer DEFAULT 0 NOT NULL,
    runner_version text,
    broker_url text,
    last_heartbeat_at timestamp with time zone,
    credential_hash text,
    sandbox_runtime text,
    sandbox_image_digest text,
    runtime_versions jsonb,
    runtime_versions_available jsonb,
    CONSTRAINT agent_runner_hosts_slots_total_check CHECK (((slots_total IS NULL) OR (slots_total > 0))),
    CONSTRAINT agent_runner_hosts_slots_used_check CHECK ((slots_used >= 0)),
    CONSTRAINT agent_runner_hosts_status_check CHECK ((status = ANY (ARRAY['enrolling'::text, 'online'::text, 'draining'::text, 'disabled'::text, 'offline'::text])))
);


--
-- Name: agent_runner_policy; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_runner_policy (
    id boolean DEFAULT true NOT NULL,
    active_host text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    runtime_versions jsonb,
    CONSTRAINT agent_runner_policy_singleton CHECK (id)
);


--
-- Name: agent_sandbox_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_sandbox_snapshots (
    id bigint NOT NULL,
    sandbox_id text NOT NULL,
    workspace_epoch bigint NOT NULL,
    anchor_job_id text NOT NULL,
    included_job_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    base_sha text NOT NULL,
    content_sha256 text,
    patch_content text,
    state text DEFAULT 'pending'::text NOT NULL,
    published_commit_sha text,
    published_pr_url text,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    captured_at timestamp with time zone,
    published_at timestamp with time zone,
    CONSTRAINT agent_sandbox_snapshots_state_check CHECK ((state = ANY (ARRAY['pending'::text, 'capturing'::text, 'captured'::text, 'publishing'::text, 'published'::text, 'failed'::text]))),
    CONSTRAINT agent_sandbox_snapshots_workspace_epoch_check CHECK ((workspace_epoch > 0))
);


--
-- Name: agent_sandbox_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_sandbox_snapshots_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_sandbox_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_sandbox_snapshots_id_seq OWNED BY public.agent_sandbox_snapshots.id;


--
-- Name: agent_sandboxes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_sandboxes (
    id text NOT NULL,
    generation bigint DEFAULT 1 NOT NULL,
    origin_thread_id text NOT NULL,
    user_id text NOT NULL,
    mode text DEFAULT 'shared_live'::text NOT NULL,
    host text NOT NULL,
    container_id text,
    container_name text NOT NULL,
    runtime_id text NOT NULL,
    runtime_version text,
    sandbox_image_digest text,
    execution_profile text NOT NULL,
    memory_limit text DEFAULT '4g'::text NOT NULL,
    cpu_limit text DEFAULT '2'::text NOT NULL,
    stop_timeout_seconds integer DEFAULT 10 NOT NULL,
    state text DEFAULT 'provisioning'::text NOT NULL,
    last_activity_at timestamp with time zone DEFAULT now() NOT NULL,
    idle_deadline timestamp with time zone,
    idle_timeout_seconds double precision DEFAULT 1800 NOT NULL,
    active_operation_count integer DEFAULT 0 NOT NULL,
    resident_slot_capacity integer DEFAULT 8 NOT NULL,
    workspace_epoch bigint DEFAULT 0 NOT NULL,
    snapshot_state text DEFAULT 'quiescent'::text NOT NULL,
    published_base_sha text,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_sandboxes_active_operation_count_check CHECK ((active_operation_count >= 0)),
    CONSTRAINT agent_sandboxes_generation_check CHECK ((generation > 0)),
    CONSTRAINT agent_sandboxes_idle_timeout_seconds_check CHECK ((idle_timeout_seconds > (0)::double precision)),
    CONSTRAINT agent_sandboxes_mode_check CHECK ((mode = ANY (ARRAY['legacy_1to1'::text, 'shared_live'::text]))),
    CONSTRAINT agent_sandboxes_resident_slot_capacity_check CHECK (((resident_slot_capacity >= 2) AND (resident_slot_capacity <= 128))),
    CONSTRAINT agent_sandboxes_snapshot_state_check CHECK ((snapshot_state = ANY (ARRAY['quiescent'::text, 'pending'::text, 'snapshotting'::text, 'importing'::text, 'dirty'::text]))),
    CONSTRAINT agent_sandboxes_state_check CHECK ((state = ANY (ARRAY['provisioning'::text, 'starting'::text, 'running'::text, 'stopping'::text, 'stopped'::text, 'stopped_dirty'::text, 'lost'::text]))),
    CONSTRAINT agent_sandboxes_stop_timeout_seconds_check CHECK (((stop_timeout_seconds >= 1) AND (stop_timeout_seconds <= 300))),
    CONSTRAINT agent_sandboxes_workspace_epoch_check CHECK ((workspace_epoch >= 0))
);


--
-- Name: agent_session_forks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_session_forks (
    id text NOT NULL,
    generation bigint DEFAULT 1 NOT NULL,
    user_id text NOT NULL,
    idempotency_key text NOT NULL,
    sandbox_id text NOT NULL,
    sandbox_generation bigint NOT NULL,
    container_id text NOT NULL,
    runtime_id text NOT NULL,
    runtime_version text,
    source_thread_id text NOT NULL,
    source_job_id text NOT NULL,
    source_native_session_id text NOT NULL,
    source_activation_generation bigint NOT NULL,
    target_thread_id text NOT NULL,
    target_native_session_id text,
    target_anchor_job_id text,
    target_resident_slot_id integer,
    source_residency_generation bigint,
    target_residency_generation bigint,
    source_process_generation bigint,
    target_process_generation bigint,
    previous_source_grant_id text,
    previous_target_grant_id text,
    state text DEFAULT 'preparing'::text NOT NULL,
    host_receipt jsonb,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    coordinator_token text,
    coordinator_lease_expires_at timestamp with time zone,
    CONSTRAINT agent_session_forks_generation_check CHECK ((generation > 0)),
    CONSTRAINT agent_session_forks_sandbox_generation_check CHECK ((sandbox_generation > 0)),
    CONSTRAINT agent_session_forks_source_activation_generation_check CHECK ((source_activation_generation >= 0)),
    CONSTRAINT agent_session_forks_source_process_generation_check CHECK ((source_process_generation > 0)),
    CONSTRAINT agent_session_forks_source_residency_generation_check CHECK ((source_residency_generation > 0)),
    CONSTRAINT agent_session_forks_state_check CHECK ((state = ANY (ARRAY['preparing'::text, 'materialized'::text, 'ready'::text, 'failed'::text]))),
    CONSTRAINT agent_session_forks_target_process_generation_check CHECK ((target_process_generation > 0)),
    CONSTRAINT agent_session_forks_target_residency_generation_check CHECK ((target_residency_generation > 0)),
    CONSTRAINT agent_session_forks_target_resident_slot_id_check CHECK ((target_resident_slot_id >= 0))
);


--
-- Name: agent_thread_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_thread_messages (
    id bigint NOT NULL,
    thread_id text NOT NULL,
    job_id text NOT NULL,
    role text NOT NULL,
    content text NOT NULL,
    source_event_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_thread_messages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_thread_messages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_thread_messages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_thread_messages_id_seq OWNED BY public.agent_thread_messages.id;


--
-- Name: agent_thread_residencies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_thread_residencies (
    thread_id text NOT NULL,
    generation bigint DEFAULT 1 NOT NULL,
    host text NOT NULL,
    sandbox_id text,
    runtime_id text NOT NULL,
    runtime_version text,
    status text DEFAULT 'hot'::text NOT NULL,
    lease_expires_at timestamp with time zone NOT NULL,
    last_checkpoint_turn_id text,
    established_at timestamp with time zone DEFAULT now() NOT NULL,
    last_active_at timestamp with time zone DEFAULT now() NOT NULL,
    grant_id text,
    CONSTRAINT agent_thread_residencies_status_check CHECK ((status = ANY (ARRAY['hot'::text, 'draining'::text, 'none'::text])))
);


--
-- Name: agent_thread_sandbox_memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_thread_sandbox_memberships (
    thread_id text NOT NULL,
    sandbox_id text NOT NULL,
    native_session_id text,
    native_state text DEFAULT 'new'::text NOT NULL,
    context_status text DEFAULT 'new'::text NOT NULL,
    activation_generation bigint DEFAULT 0 NOT NULL,
    process_generation bigint DEFAULT 0 NOT NULL,
    residency_generation bigint DEFAULT 0 NOT NULL,
    resident_state text DEFAULT 'stopped'::text NOT NULL,
    resident_slot_id integer,
    active_attempt_id bigint,
    admitted_job_id text,
    admitted_at timestamp with time zone,
    last_settled_job_id text,
    forked_from_thread_id text,
    fork_anchor_job_id text,
    fork_operation_id text,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_thread_sandbox_memberships_activation_generation_check CHECK ((activation_generation >= 0)),
    CONSTRAINT agent_thread_sandbox_memberships_context_status_check CHECK ((context_status = ANY (ARRAY['new'::text, 'live'::text, 'restored'::text, 'lost'::text]))),
    CONSTRAINT agent_thread_sandbox_memberships_native_state_check CHECK ((native_state = ANY (ARRAY['new'::text, 'ready'::text, 'fork_preparing'::text, 'lost'::text]))),
    CONSTRAINT agent_thread_sandbox_memberships_process_generation_check CHECK ((process_generation >= 0)),
    CONSTRAINT agent_thread_sandbox_memberships_residency_generation_check CHECK ((residency_generation >= 0)),
    CONSTRAINT agent_thread_sandbox_memberships_resident_slot_id_check CHECK ((resident_slot_id >= 0)),
    CONSTRAINT agent_thread_sandbox_memberships_resident_state_check CHECK ((resident_state = ANY (ARRAY['stopped'::text, 'starting'::text, 'live'::text, 'restarting'::text, 'lost'::text])))
);


--
-- Name: agent_thread_sandboxes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_thread_sandboxes (
    thread_id text NOT NULL,
    sandbox_id text NOT NULL,
    generation bigint DEFAULT 1 NOT NULL,
    host text NOT NULL,
    container_id text,
    container_name text NOT NULL,
    runtime_id text NOT NULL,
    runtime_version text,
    sandbox_image_digest text,
    execution_profile text NOT NULL,
    state text DEFAULT 'provisioning'::text NOT NULL,
    context_status text DEFAULT 'new'::text NOT NULL,
    activation_generation bigint DEFAULT 0 NOT NULL,
    process_generation bigint DEFAULT 0 NOT NULL,
    active_attempt_id bigint,
    admitted_job_id text,
    admitted_at timestamp with time zone,
    last_settled_job_id text,
    native_session_id text,
    idle_deadline timestamp with time zone,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_thread_sandboxes_activation_generation_check CHECK ((activation_generation >= 0)),
    CONSTRAINT agent_thread_sandboxes_context_status_check CHECK ((context_status = ANY (ARRAY['new'::text, 'live'::text, 'restored'::text, 'lost'::text]))),
    CONSTRAINT agent_thread_sandboxes_generation_check CHECK ((generation > 0)),
    CONSTRAINT agent_thread_sandboxes_process_generation_check CHECK ((process_generation >= 0)),
    CONSTRAINT agent_thread_sandboxes_state_check CHECK ((state = ANY (ARRAY['provisioning'::text, 'starting'::text, 'running'::text, 'stopping'::text, 'stopped'::text, 'stopped_dirty'::text, 'lost'::text])))
);


--
-- Name: agent_threads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_threads (
    id text NOT NULL,
    user_id text NOT NULL,
    repo text NOT NULL,
    title text NOT NULL,
    archived_at timestamp with time zone,
    pinned_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_tool_call_resources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_tool_call_resources (
    attempt_id bigint NOT NULL,
    call_seq integer NOT NULL,
    tool_use_id text NOT NULL,
    tool_name text NOT NULL,
    timing_source text DEFAULT 'message'::text NOT NULL,
    duration_ms bigint,
    sample_count integer DEFAULT 0 NOT NULL,
    overlap_n integer DEFAULT 1 NOT NULL,
    measured_span_ms bigint,
    cpu_usec_exclusive bigint,
    cpu_usec_shared bigint,
    mem_peak_exclusive_bytes bigint,
    mem_peak_shared_bytes bigint,
    process_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    process_cpu_usec bigint,
    process_mem_peak_bytes bigint,
    process_io_read_bytes bigint,
    process_io_write_bytes bigint,
    process_resources_partial boolean DEFAULT false NOT NULL,
    CONSTRAINT agent_tool_call_resources_check CHECK ((NOT (((cpu_usec_exclusive IS NOT NULL) OR (mem_peak_exclusive_bytes IS NOT NULL)) AND ((cpu_usec_shared IS NOT NULL) OR (mem_peak_shared_bytes IS NOT NULL))))),
    CONSTRAINT agent_tool_call_resources_check1 CHECK ((((overlap_n = 1) AND (cpu_usec_shared IS NULL) AND (mem_peak_shared_bytes IS NULL)) OR ((overlap_n > 1) AND (cpu_usec_exclusive IS NULL) AND (mem_peak_exclusive_bytes IS NULL)))),
    CONSTRAINT agent_tool_call_resources_check2 CHECK (((sample_count >= 2) OR ((measured_span_ms IS NULL) AND (cpu_usec_exclusive IS NULL) AND (cpu_usec_shared IS NULL) AND (mem_peak_exclusive_bytes IS NULL) AND (mem_peak_shared_bytes IS NULL)))),
    CONSTRAINT agent_tool_call_resources_overlap_n_check CHECK ((overlap_n >= 1)),
    CONSTRAINT agent_tool_call_resources_process_count_check CHECK ((process_count >= 0)),
    CONSTRAINT agent_tool_call_resources_process_cpu_usec_check CHECK (((process_cpu_usec IS NULL) OR (process_cpu_usec >= 0))),
    CONSTRAINT agent_tool_call_resources_process_io_read_bytes_check CHECK (((process_io_read_bytes IS NULL) OR (process_io_read_bytes >= 0))),
    CONSTRAINT agent_tool_call_resources_process_io_write_bytes_check CHECK (((process_io_write_bytes IS NULL) OR (process_io_write_bytes >= 0))),
    CONSTRAINT agent_tool_call_resources_process_mem_peak_bytes_check CHECK (((process_mem_peak_bytes IS NULL) OR (process_mem_peak_bytes >= 0))),
    CONSTRAINT agent_tool_call_resources_process_resources_need_processes CHECK (((process_count > 0) OR ((process_cpu_usec IS NULL) AND (process_mem_peak_bytes IS NULL) AND (process_io_read_bytes IS NULL) AND (process_io_write_bytes IS NULL) AND (NOT process_resources_partial))))
);


--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: agent_admin_audit_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_admin_audit_log ALTER COLUMN id SET DEFAULT nextval('public.agent_admin_audit_log_id_seq'::regclass);


--
-- Name: agent_attempts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_attempts ALTER COLUMN id SET DEFAULT nextval('public.agent_attempts_id_seq'::regclass);


--
-- Name: agent_gitlab_connections id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_gitlab_connections ALTER COLUMN id SET DEFAULT nextval('public.agent_gitlab_connections_id_seq'::regclass);


--
-- Name: agent_job_artifacts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_job_artifacts ALTER COLUMN id SET DEFAULT nextval('public.agent_job_artifacts_id_seq'::regclass);


--
-- Name: agent_job_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_job_events ALTER COLUMN id SET DEFAULT nextval('public.agent_job_events_id_seq'::regclass);


--
-- Name: agent_mcp_connections id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_mcp_connections ALTER COLUMN id SET DEFAULT nextval('public.agent_mcp_connections_id_seq'::regclass);


--
-- Name: agent_native_session_edges id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_native_session_edges ALTER COLUMN id SET DEFAULT nextval('public.agent_native_session_edges_id_seq'::regclass);


--
-- Name: agent_repo_grants id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_repo_grants ALTER COLUMN id SET DEFAULT nextval('public.agent_repo_grants_id_seq'::regclass);


--
-- Name: agent_sandbox_snapshots id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sandbox_snapshots ALTER COLUMN id SET DEFAULT nextval('public.agent_sandbox_snapshots_id_seq'::regclass);


--
-- Name: agent_thread_messages id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_messages ALTER COLUMN id SET DEFAULT nextval('public.agent_thread_messages_id_seq'::regclass);


--
-- Name: agent_admin_audit_log agent_admin_audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_admin_audit_log
    ADD CONSTRAINT agent_admin_audit_log_pkey PRIMARY KEY (id);


--
-- Name: agent_attempt_resources agent_attempt_resources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_attempt_resources
    ADD CONSTRAINT agent_attempt_resources_pkey PRIMARY KEY (attempt_id);


--
-- Name: agent_attempts agent_attempts_job_id_attempt_no_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_attempts
    ADD CONSTRAINT agent_attempts_job_id_attempt_no_key UNIQUE (job_id, attempt_no);


--
-- Name: agent_attempts agent_attempts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_attempts
    ADD CONSTRAINT agent_attempts_pkey PRIMARY KEY (id);


--
-- Name: agent_gitlab_connections agent_gitlab_connections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_gitlab_connections
    ADD CONSTRAINT agent_gitlab_connections_pkey PRIMARY KEY (id);


--
-- Name: agent_gitlab_connections agent_gitlab_connections_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_gitlab_connections
    ADD CONSTRAINT agent_gitlab_connections_user_id_key UNIQUE (user_id);


--
-- Name: agent_host_enroll_tokens agent_host_enroll_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_host_enroll_tokens
    ADD CONSTRAINT agent_host_enroll_tokens_pkey PRIMARY KEY (token_hash);


--
-- Name: agent_job_artifacts agent_job_artifacts_job_id_attempt_id_kind_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_job_artifacts
    ADD CONSTRAINT agent_job_artifacts_job_id_attempt_id_kind_key UNIQUE (job_id, attempt_id, kind);


--
-- Name: agent_job_artifacts agent_job_artifacts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_job_artifacts
    ADD CONSTRAINT agent_job_artifacts_pkey PRIMARY KEY (id);


--
-- Name: agent_job_events agent_job_events_attempt_id_seq_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_job_events
    ADD CONSTRAINT agent_job_events_attempt_id_seq_key UNIQUE (attempt_id, seq);


--
-- Name: agent_job_events agent_job_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_job_events
    ADD CONSTRAINT agent_job_events_pkey PRIMARY KEY (id);


--
-- Name: agent_jobs agent_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_jobs
    ADD CONSTRAINT agent_jobs_pkey PRIMARY KEY (id);


--
-- Name: agent_mcp_connections agent_mcp_connections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_mcp_connections
    ADD CONSTRAINT agent_mcp_connections_pkey PRIMARY KEY (id);


--
-- Name: agent_mcp_connections agent_mcp_connections_user_id_server_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_mcp_connections
    ADD CONSTRAINT agent_mcp_connections_user_id_server_key UNIQUE (user_id, server);


--
-- Name: agent_native_session_edges agent_native_session_edges_operation_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_native_session_edges
    ADD CONSTRAINT agent_native_session_edges_operation_id_key UNIQUE (operation_id);


--
-- Name: agent_native_session_edges agent_native_session_edges_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_native_session_edges
    ADD CONSTRAINT agent_native_session_edges_pkey PRIMARY KEY (id);


--
-- Name: agent_native_session_edges agent_native_session_edges_sandbox_id_target_native_session_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_native_session_edges
    ADD CONSTRAINT agent_native_session_edges_sandbox_id_target_native_session_key UNIQUE (sandbox_id, target_native_session_id);


--
-- Name: agent_native_session_edges agent_native_session_edges_target_thread_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_native_session_edges
    ADD CONSTRAINT agent_native_session_edges_target_thread_id_key UNIQUE (target_thread_id);


--
-- Name: agent_oauth_states agent_oauth_states_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_oauth_states
    ADD CONSTRAINT agent_oauth_states_pkey PRIMARY KEY (state_hash);


--
-- Name: agent_process_observations agent_process_observations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_process_observations
    ADD CONSTRAINT agent_process_observations_pkey PRIMARY KEY (attempt_id, process_seq);


--
-- Name: agent_repo_grants agent_repo_grants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_repo_grants
    ADD CONSTRAINT agent_repo_grants_pkey PRIMARY KEY (id);


--
-- Name: agent_repo_grants agent_repo_grants_user_id_installation_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_repo_grants
    ADD CONSTRAINT agent_repo_grants_user_id_installation_id_key UNIQUE (user_id, installation_id);


--
-- Name: agent_runner_hosts agent_runner_hosts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runner_hosts
    ADD CONSTRAINT agent_runner_hosts_pkey PRIMARY KEY (host);


--
-- Name: agent_runner_policy agent_runner_policy_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runner_policy
    ADD CONSTRAINT agent_runner_policy_pkey PRIMARY KEY (id);


--
-- Name: agent_sandbox_snapshots agent_sandbox_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sandbox_snapshots
    ADD CONSTRAINT agent_sandbox_snapshots_pkey PRIMARY KEY (id);


--
-- Name: agent_sandbox_snapshots agent_sandbox_snapshots_sandbox_id_workspace_epoch_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sandbox_snapshots
    ADD CONSTRAINT agent_sandbox_snapshots_sandbox_id_workspace_epoch_key UNIQUE (sandbox_id, workspace_epoch);


--
-- Name: agent_sandboxes agent_sandboxes_container_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sandboxes
    ADD CONSTRAINT agent_sandboxes_container_id_key UNIQUE (container_id);


--
-- Name: agent_sandboxes agent_sandboxes_container_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sandboxes
    ADD CONSTRAINT agent_sandboxes_container_name_key UNIQUE (container_name);


--
-- Name: agent_sandboxes agent_sandboxes_origin_thread_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sandboxes
    ADD CONSTRAINT agent_sandboxes_origin_thread_id_key UNIQUE (origin_thread_id);


--
-- Name: agent_sandboxes agent_sandboxes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sandboxes
    ADD CONSTRAINT agent_sandboxes_pkey PRIMARY KEY (id);


--
-- Name: agent_session_forks agent_session_forks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_session_forks
    ADD CONSTRAINT agent_session_forks_pkey PRIMARY KEY (id);


--
-- Name: agent_session_forks agent_session_forks_target_thread_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_session_forks
    ADD CONSTRAINT agent_session_forks_target_thread_id_key UNIQUE (target_thread_id);


--
-- Name: agent_session_forks agent_session_forks_user_id_idempotency_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_session_forks
    ADD CONSTRAINT agent_session_forks_user_id_idempotency_key_key UNIQUE (user_id, idempotency_key);


--
-- Name: agent_thread_messages agent_thread_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_messages
    ADD CONSTRAINT agent_thread_messages_pkey PRIMARY KEY (id);


--
-- Name: agent_thread_messages agent_thread_messages_source_event_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_messages
    ADD CONSTRAINT agent_thread_messages_source_event_id_key UNIQUE (source_event_id);


--
-- Name: agent_thread_residencies agent_thread_residencies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_residencies
    ADD CONSTRAINT agent_thread_residencies_pkey PRIMARY KEY (thread_id);


--
-- Name: agent_thread_sandbox_memberships agent_thread_sandbox_memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_sandbox_memberships
    ADD CONSTRAINT agent_thread_sandbox_memberships_pkey PRIMARY KEY (thread_id);


--
-- Name: agent_thread_sandboxes agent_thread_sandboxes_container_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_sandboxes
    ADD CONSTRAINT agent_thread_sandboxes_container_id_key UNIQUE (container_id);


--
-- Name: agent_thread_sandboxes agent_thread_sandboxes_container_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_sandboxes
    ADD CONSTRAINT agent_thread_sandboxes_container_name_key UNIQUE (container_name);


--
-- Name: agent_thread_sandboxes agent_thread_sandboxes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_sandboxes
    ADD CONSTRAINT agent_thread_sandboxes_pkey PRIMARY KEY (thread_id);


--
-- Name: agent_thread_sandboxes agent_thread_sandboxes_sandbox_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_sandboxes
    ADD CONSTRAINT agent_thread_sandboxes_sandbox_id_key UNIQUE (sandbox_id);


--
-- Name: agent_threads agent_threads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_threads
    ADD CONSTRAINT agent_threads_pkey PRIMARY KEY (id);


--
-- Name: agent_tool_call_resources agent_tool_call_resources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_tool_call_resources
    ADD CONSTRAINT agent_tool_call_resources_pkey PRIMARY KEY (attempt_id, call_seq);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: idx_agent_admin_audit_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_admin_audit_created ON public.agent_admin_audit_log USING btree (created_at DESC);


--
-- Name: idx_agent_attempt_resources_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_attempt_resources_job ON public.agent_attempt_resources USING btree (job_id);


--
-- Name: idx_agent_attempts_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_attempts_expiry ON public.agent_attempts USING btree (lease_expires_at) WHERE (status = 'running'::text);


--
-- Name: idx_agent_attempts_grant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_attempts_grant ON public.agent_attempts USING btree (grant_id) WHERE (grant_id IS NOT NULL);


--
-- Name: idx_agent_attempts_host; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_attempts_host ON public.agent_attempts USING btree (host_id) WHERE (host_id IS NOT NULL);


--
-- Name: idx_agent_host_enroll_tokens_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_host_enroll_tokens_expiry ON public.agent_host_enroll_tokens USING btree (expires_at);


--
-- Name: idx_agent_job_events_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_job_events_job ON public.agent_job_events USING btree (job_id, id);


--
-- Name: idx_agent_jobs_native_turn; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_agent_jobs_native_turn ON public.agent_jobs USING btree (thread_id, native_turn_no) WHERE (native_turn_no IS NOT NULL);


--
-- Name: idx_agent_jobs_queued; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_jobs_queued ON public.agent_jobs USING btree (created_at) WHERE (state = 'queued'::text);


--
-- Name: idx_agent_jobs_thread_turn; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_agent_jobs_thread_turn ON public.agent_jobs USING btree (thread_id, turn_no);


--
-- Name: idx_agent_jobs_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_jobs_user ON public.agent_jobs USING btree (user_id, created_at DESC);


--
-- Name: idx_agent_jobs_user_repo; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_jobs_user_repo ON public.agent_jobs USING btree (user_id, repo, created_at DESC);


--
-- Name: idx_agent_memberships_native_session; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_agent_memberships_native_session ON public.agent_thread_sandbox_memberships USING btree (sandbox_id, native_session_id) WHERE (native_session_id IS NOT NULL);


--
-- Name: idx_agent_memberships_resident_slot; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_agent_memberships_resident_slot ON public.agent_thread_sandbox_memberships USING btree (sandbox_id, resident_slot_id) WHERE (resident_slot_id IS NOT NULL);


--
-- Name: idx_agent_memberships_sandbox; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_memberships_sandbox ON public.agent_thread_sandbox_memberships USING btree (sandbox_id);


--
-- Name: idx_agent_oauth_states_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_oauth_states_expiry ON public.agent_oauth_states USING btree (expires_at);


--
-- Name: idx_agent_repo_grants_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_repo_grants_user ON public.agent_repo_grants USING btree (user_id);


--
-- Name: idx_agent_runner_hosts_online; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_runner_hosts_online ON public.agent_runner_hosts USING btree (host) WHERE (status = 'online'::text);


--
-- Name: idx_agent_sandbox_snapshots_capturing; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_agent_sandbox_snapshots_capturing ON public.agent_sandbox_snapshots USING btree (sandbox_id) WHERE (state = 'capturing'::text);


--
-- Name: idx_agent_sandbox_snapshots_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_agent_sandbox_snapshots_pending ON public.agent_sandbox_snapshots USING btree (sandbox_id) WHERE (state = 'pending'::text);


--
-- Name: idx_agent_sandbox_snapshots_publish; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_sandbox_snapshots_publish ON public.agent_sandbox_snapshots USING btree (state, updated_at);


--
-- Name: idx_agent_sandboxes_host_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_sandboxes_host_state ON public.agent_sandboxes USING btree (host, state);


--
-- Name: idx_agent_sandboxes_idle; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_sandboxes_idle ON public.agent_sandboxes USING btree (idle_deadline) WHERE ((state = 'running'::text) AND (active_operation_count = 0));


--
-- Name: idx_agent_session_forks_source_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_agent_session_forks_source_active ON public.agent_session_forks USING btree (source_thread_id) WHERE (state = ANY (ARRAY['preparing'::text, 'materialized'::text]));


--
-- Name: idx_agent_session_forks_target_slot_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_agent_session_forks_target_slot_active ON public.agent_session_forks USING btree (sandbox_id, target_resident_slot_id) WHERE (state = ANY (ARRAY['preparing'::text, 'materialized'::text]));


--
-- Name: idx_agent_thread_messages_thread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_thread_messages_thread ON public.agent_thread_messages USING btree (thread_id, id);


--
-- Name: idx_agent_thread_messages_user_job; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_agent_thread_messages_user_job ON public.agent_thread_messages USING btree (job_id) WHERE (role = 'user'::text);


--
-- Name: idx_agent_thread_residencies_hot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_thread_residencies_hot ON public.agent_thread_residencies USING btree (host, lease_expires_at) WHERE (status = 'hot'::text);


--
-- Name: idx_agent_thread_sandboxes_host_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_thread_sandboxes_host_state ON public.agent_thread_sandboxes USING btree (host, state);


--
-- Name: idx_agent_thread_sandboxes_idle; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_thread_sandboxes_idle ON public.agent_thread_sandboxes USING btree (idle_deadline) WHERE ((state = 'running'::text) AND (active_attempt_id IS NULL));


--
-- Name: uq_agent_job_events_attempt_client_seq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_agent_job_events_attempt_client_seq ON public.agent_job_events USING btree (attempt_id, client_seq) WHERE (client_seq IS NOT NULL);


--
-- Name: agent_jobs trg_agent_job_native_turn_no; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_agent_job_native_turn_no BEFORE INSERT OR UPDATE OF state, fork_source_job_id, native_turn_no ON public.agent_jobs FOR EACH ROW EXECUTE FUNCTION public.assign_agent_job_native_turn_no();


--
-- Name: agent_sandbox_snapshots trg_snapshot_anchor_native_turn_no; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_snapshot_anchor_native_turn_no AFTER INSERT OR UPDATE OF anchor_job_id, included_job_ids ON public.agent_sandbox_snapshots FOR EACH ROW EXECUTE FUNCTION public.assign_snapshot_anchor_native_turn_no();


--
-- Name: agent_attempt_resources agent_attempt_resources_attempt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_attempt_resources
    ADD CONSTRAINT agent_attempt_resources_attempt_id_fkey FOREIGN KEY (attempt_id) REFERENCES public.agent_attempts(id) ON DELETE CASCADE;


--
-- Name: agent_attempts agent_attempts_host_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_attempts
    ADD CONSTRAINT agent_attempts_host_id_fkey FOREIGN KEY (host_id) REFERENCES public.agent_runner_hosts(host) ON DELETE SET NULL;


--
-- Name: agent_attempts agent_attempts_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_attempts
    ADD CONSTRAINT agent_attempts_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.agent_jobs(id);


--
-- Name: agent_job_artifacts agent_job_artifacts_attempt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_job_artifacts
    ADD CONSTRAINT agent_job_artifacts_attempt_id_fkey FOREIGN KEY (attempt_id) REFERENCES public.agent_attempts(id);


--
-- Name: agent_job_events agent_job_events_attempt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_job_events
    ADD CONSTRAINT agent_job_events_attempt_id_fkey FOREIGN KEY (attempt_id) REFERENCES public.agent_attempts(id);


--
-- Name: agent_native_session_edges agent_native_session_edges_operation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_native_session_edges
    ADD CONSTRAINT agent_native_session_edges_operation_id_fkey FOREIGN KEY (operation_id) REFERENCES public.agent_session_forks(id) ON DELETE RESTRICT;


--
-- Name: agent_native_session_edges agent_native_session_edges_sandbox_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_native_session_edges
    ADD CONSTRAINT agent_native_session_edges_sandbox_id_fkey FOREIGN KEY (sandbox_id) REFERENCES public.agent_sandboxes(id) ON DELETE RESTRICT;


--
-- Name: agent_native_session_edges agent_native_session_edges_source_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_native_session_edges
    ADD CONSTRAINT agent_native_session_edges_source_thread_id_fkey FOREIGN KEY (source_thread_id) REFERENCES public.agent_threads(id) ON DELETE RESTRICT;


--
-- Name: agent_native_session_edges agent_native_session_edges_target_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_native_session_edges
    ADD CONSTRAINT agent_native_session_edges_target_thread_id_fkey FOREIGN KEY (target_thread_id) REFERENCES public.agent_threads(id) ON DELETE RESTRICT;


--
-- Name: agent_process_observations agent_process_observations_attempt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_process_observations
    ADD CONSTRAINT agent_process_observations_attempt_id_fkey FOREIGN KEY (attempt_id) REFERENCES public.agent_attempts(id) ON DELETE CASCADE;


--
-- Name: agent_sandbox_snapshots agent_sandbox_snapshots_anchor_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sandbox_snapshots
    ADD CONSTRAINT agent_sandbox_snapshots_anchor_job_id_fkey FOREIGN KEY (anchor_job_id) REFERENCES public.agent_jobs(id) ON DELETE RESTRICT;


--
-- Name: agent_sandbox_snapshots agent_sandbox_snapshots_sandbox_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sandbox_snapshots
    ADD CONSTRAINT agent_sandbox_snapshots_sandbox_id_fkey FOREIGN KEY (sandbox_id) REFERENCES public.agent_sandboxes(id) ON DELETE CASCADE;


--
-- Name: agent_sandboxes agent_sandboxes_origin_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sandboxes
    ADD CONSTRAINT agent_sandboxes_origin_thread_id_fkey FOREIGN KEY (origin_thread_id) REFERENCES public.agent_threads(id) ON DELETE RESTRICT;


--
-- Name: agent_session_forks agent_session_forks_sandbox_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_session_forks
    ADD CONSTRAINT agent_session_forks_sandbox_id_fkey FOREIGN KEY (sandbox_id) REFERENCES public.agent_sandboxes(id) ON DELETE RESTRICT;


--
-- Name: agent_session_forks agent_session_forks_source_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_session_forks
    ADD CONSTRAINT agent_session_forks_source_job_id_fkey FOREIGN KEY (source_job_id) REFERENCES public.agent_jobs(id) ON DELETE RESTRICT;


--
-- Name: agent_session_forks agent_session_forks_source_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_session_forks
    ADD CONSTRAINT agent_session_forks_source_thread_id_fkey FOREIGN KEY (source_thread_id) REFERENCES public.agent_threads(id) ON DELETE RESTRICT;


--
-- Name: agent_session_forks agent_session_forks_target_anchor_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_session_forks
    ADD CONSTRAINT agent_session_forks_target_anchor_job_id_fkey FOREIGN KEY (target_anchor_job_id) REFERENCES public.agent_jobs(id);


--
-- Name: agent_thread_messages agent_thread_messages_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_messages
    ADD CONSTRAINT agent_thread_messages_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.agent_jobs(id);


--
-- Name: agent_thread_messages agent_thread_messages_source_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_messages
    ADD CONSTRAINT agent_thread_messages_source_event_id_fkey FOREIGN KEY (source_event_id) REFERENCES public.agent_job_events(id);


--
-- Name: agent_thread_messages agent_thread_messages_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_messages
    ADD CONSTRAINT agent_thread_messages_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.agent_threads(id);


--
-- Name: agent_thread_sandbox_memberships agent_thread_sandbox_memberships_active_attempt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_sandbox_memberships
    ADD CONSTRAINT agent_thread_sandbox_memberships_active_attempt_id_fkey FOREIGN KEY (active_attempt_id) REFERENCES public.agent_attempts(id);


--
-- Name: agent_thread_sandbox_memberships agent_thread_sandbox_memberships_admitted_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_sandbox_memberships
    ADD CONSTRAINT agent_thread_sandbox_memberships_admitted_job_id_fkey FOREIGN KEY (admitted_job_id) REFERENCES public.agent_jobs(id);


--
-- Name: agent_thread_sandbox_memberships agent_thread_sandbox_memberships_fork_anchor_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_sandbox_memberships
    ADD CONSTRAINT agent_thread_sandbox_memberships_fork_anchor_job_id_fkey FOREIGN KEY (fork_anchor_job_id) REFERENCES public.agent_jobs(id);


--
-- Name: agent_thread_sandbox_memberships agent_thread_sandbox_memberships_forked_from_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_sandbox_memberships
    ADD CONSTRAINT agent_thread_sandbox_memberships_forked_from_thread_id_fkey FOREIGN KEY (forked_from_thread_id) REFERENCES public.agent_threads(id);


--
-- Name: agent_thread_sandbox_memberships agent_thread_sandbox_memberships_last_settled_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_sandbox_memberships
    ADD CONSTRAINT agent_thread_sandbox_memberships_last_settled_job_id_fkey FOREIGN KEY (last_settled_job_id) REFERENCES public.agent_jobs(id);


--
-- Name: agent_thread_sandbox_memberships agent_thread_sandbox_memberships_sandbox_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_sandbox_memberships
    ADD CONSTRAINT agent_thread_sandbox_memberships_sandbox_id_fkey FOREIGN KEY (sandbox_id) REFERENCES public.agent_sandboxes(id) ON DELETE RESTRICT;


--
-- Name: agent_thread_sandbox_memberships agent_thread_sandbox_memberships_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_sandbox_memberships
    ADD CONSTRAINT agent_thread_sandbox_memberships_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.agent_threads(id) ON DELETE CASCADE;


--
-- Name: agent_thread_sandboxes agent_thread_sandboxes_active_attempt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_sandboxes
    ADD CONSTRAINT agent_thread_sandboxes_active_attempt_id_fkey FOREIGN KEY (active_attempt_id) REFERENCES public.agent_attempts(id);


--
-- Name: agent_thread_sandboxes agent_thread_sandboxes_admitted_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_sandboxes
    ADD CONSTRAINT agent_thread_sandboxes_admitted_job_id_fkey FOREIGN KEY (admitted_job_id) REFERENCES public.agent_jobs(id);


--
-- Name: agent_thread_sandboxes agent_thread_sandboxes_last_settled_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_sandboxes
    ADD CONSTRAINT agent_thread_sandboxes_last_settled_job_id_fkey FOREIGN KEY (last_settled_job_id) REFERENCES public.agent_jobs(id);


--
-- Name: agent_thread_sandboxes agent_thread_sandboxes_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_sandboxes
    ADD CONSTRAINT agent_thread_sandboxes_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.agent_threads(id) ON DELETE CASCADE;


--
-- Name: agent_tool_call_resources agent_tool_call_resources_attempt_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_tool_call_resources
    ADD CONSTRAINT agent_tool_call_resources_attempt_id_fkey FOREIGN KEY (attempt_id) REFERENCES public.agent_attempts(id) ON DELETE CASCADE;


--
-- Name: agent_thread_sandbox_memberships fk_agent_membership_fork; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_thread_sandbox_memberships
    ADD CONSTRAINT fk_agent_membership_fork FOREIGN KEY (fork_operation_id) REFERENCES public.agent_session_forks(id);


--
-- PostgreSQL database dump complete
--
