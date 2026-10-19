// Explicit HTTP contract fixtures, not a replacement for control-plane/DB tests.
// Unknown API calls fail visibly so new UI dependencies cannot silently pass.
const timestamp = '2026-10-17T00:00:00Z';

function job(id, prompt = id) {
  return {
    id,
    title: prompt,
    task_prompt: prompt,
    repo: 'fixture/repository',
    thread_id: `thread-${id}`,
    turn_no: 1,
    runtime: 'codex',
    model: 'fixture-model',
    state: 'succeeded',
    conversation_state: 'idle',
    native_state: 'ready',
    sandbox_id: 'fixture-workspace',
    workspace_shared: true,
    native_fork_available: true,
    context_status: 'live',
    resident_state: 'live',
    cancel_requested: false,
    current_attempt_id: null,
    base_sha: 'a'.repeat(40),
    output_branch: null,
    published_pr_url: null,
    detail: null,
    metadata: {},
    spent_usd: null,
    tokens_in: null,
    tokens_out: null,
    model_calls: null,
    revision: 1,
    thread_revision: 1,
    created_at: timestamp,
    updated_at: timestamp,
    archived_at: null,
    pinned_at: null,
  };
}
function json(res, body, status = 200) {
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(body));
}
async function body(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString() || '{}');
}

export function createFixture() {
  let state;
  const held = new Map();
  const streams = new Set();
  function close() {
    for (const { res } of held.values()) res.destroy();
    held.clear();
    for (const res of streams) res.end();
    streams.clear();
  }
  function reset() {
    close();
    state = {
      jobs: new Map([
        ['source', job('source', 'Source conversation')],
        ['other', job('other', 'Other conversation')],
      ]),
      receipts: new Map(),
      readReceipts: new Map(),
      readRevisions: new Map(),
      readRevision: 0,
      creates: [],
      forks: [],
      terminals: [],
      terminalStreams: 0,
      delayCreate: false,
      dropCreate: false,
      delayFork: false,
      unknown: [],
    };
  }
  reset();
  const globalEnvironment = {
    id: null,
    name: 'Global',
    revision: 1,
    variables: [],
    setup_script: '',
    start_script: '',
  };
  function stream(req, res, content = ': connected\n\n') {
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' });
    res.write(content);
    streams.add(res);
    req.on('close', () => streams.delete(res));
  }
  async function handle(req, res) {
    const url = new URL(req.url, 'http://fixture.invalid');
    const path = url.pathname;
    if (path === '/__fixture/health') {
      json(res, { ready: true });
      return true;
    }
    if (path === '/__fixture/reset') {
      reset();
      json(res, {});
      return true;
    }
    if (path === '/__fixture/config') {
      Object.assign(state, await body(req));
      json(res, {});
      return true;
    }
    if (path === '/__fixture/state') {
      json(res, {
        creates: state.creates,
        forks: state.forks,
        pending: [...held.keys()],
        terminalStreams: state.terminalStreams,
        readReceipts: [...state.readReceipts.values()],
        unknown: state.unknown,
      });
      return true;
    }
    if (path === '/__fixture/release') {
      const { key, fail = false } = await body(req);
      const pending = held.get(key);
      if (!pending) {
        json(res, { error: `No held response: ${key}` }, 409);
        return true;
      }
      held.delete(key);
      json(
        pending.res,
        fail ? { detail: 'Delayed operation failed' } : pending.value,
        fail ? 503 : 201,
      );
      json(res, {});
      return true;
    }
    if (!path.startsWith('/api/')) return false;
    const api = path.slice(4);
    if (api === '/v1/session/me')
      json(res, {
        id: 'browser-owner',
        agent_access: { allowed: true, permissions: ['agent.use'] },
      });
    else if (api === '/v1/agent/config')
      json(res, {
        repos: ['fixture/repository'],
        runtimes: ['codex'],
        models: ['fixture-model'],
        github_connected: true,
        github_install_url: null,
      });
    else if (api === '/v1/agent/branches')
      json(res, { branches: ['main'], default_branch: 'main' });
    else if (api === '/v1/agent/profile')
      json(res, {
        email: 'browser@example.invalid',
        first_name: 'Browser',
        last_name: '',
        image_data_url: null,
        companion_data_url: null,
        revision: 1,
      });
    else if (api === '/v1/agent/insights')
      json(res, { avatar: null, focus_areas: [], suggestions: [] });
    else if (api === '/v1/agent/skills') json(res, { skills: [] });
    else if (api === '/v1/agent/ssh-servers') json(res, { servers: [] });
    else if (api === '/v1/agent/mcp/connections') json(res, { connections: [] });
    else if (api === '/v1/agent/mcp/invocations')
      json(res, { invocations: [], next_cursor: null, pending_count: 0 });
    else if (api === '/v1/agent/environments')
      json(res, { environments: [], globals: globalEnvironment, snapshots_enabled: false });
    else if (api === '/v1/agent/projects')
      json(res, {
        projects: [
          {
            repo: 'fixture/repository',
            task_count: state.jobs.size,
            active_count: 0,
            last_activity_at: timestamp,
          },
        ],
      });
    else if (api === '/v1/agent/conversations/read-receipts') {
      const changed = [];
      if (req.method === 'POST') {
        for (const receipt of (await body(req)).receipts) {
          const item = state.jobs.get(receipt.job_id);
          if (!item || receipt.attempt_id !== item.current_attempt_id) continue;
          const canonical = { ...receipt, thread_id: item.thread_id, turn_no: item.turn_no };
          if (JSON.stringify(state.readReceipts.get(item.thread_id)) === JSON.stringify(canonical))
            continue;
          state.readReceipts.set(item.thread_id, canonical);
          state.readRevisions.set(item.thread_id, ++state.readRevision);
          changed.push(canonical);
        }
      }
      const cursor = Number(url.searchParams.get('cursor') ?? 0);
      const updated = [...state.readReceipts.values()].filter(
        (receipt) => state.readRevisions.get(receipt.thread_id) > cursor,
      );
      json(res, {
        user_id: 'browser-owner',
        receipts: req.method === 'POST' ? changed : updated,
        next_cursor: null,
        sync_cursor: req.method === 'POST' ? null : String(state.readRevision),
      });
    } else if (api === '/v1/agent/conversations')
      json(res, {
        conversations: [...state.jobs.values()].map((item) => ({
          job: item,
          title: item.title,
          archived: false,
          last_activity_at: timestamp,
        })),
        next_cursor: null,
      });
    else if (api === '/v1/agent/jobs' && req.method === 'POST') {
      const input = await body(req);
      const key = req.headers['idempotency-key'];
      const fingerprint = JSON.stringify(input);
      const prior = state.receipts.get(key);
      if (prior && prior.fingerprint !== fingerprint) {
        json(
          res,
          { detail: { error: { type: 'idempotency_conflict', message: 'Different submission' } } },
          409,
        );
      } else {
        const value = prior?.value ?? job(`created-${state.creates.length + 1}`, input.task_prompt);
        if (!prior) {
          state.receipts.set(key, { fingerprint, value });
          state.jobs.set(value.id, value);
          state.creates.push({ id: value.id, key, prompt: input.task_prompt });
        }
        if (state.delayCreate) {
          state.delayCreate = false;
          held.set('create', { res, value });
        } else if (state.dropCreate) {
          state.dropCreate = false;
          // Start a response, then close before the advertised body arrives.
          // Destroying a reused socket before headers can make Chromium resend
          // the POST automatically, hiding the ambiguous response from the UI.
          res.writeHead(201, {
            'Content-Type': 'application/json',
            'Content-Length': '1024',
            Connection: 'close',
          });
          res.end('{');
        } else json(res, value, 201);
      }
    } else if (/^\/v1\/agent\/jobs\/[^/]+\/fork$/.test(api) && req.method === 'POST') {
      const value = job(`fork-${state.forks.length + 1}`, 'Forked conversation');
      state.jobs.set(value.id, value);
      state.forks.push(value.id);
      if (state.delayFork) held.set('fork', { res, value });
      else json(res, value, 201);
    } else if (/^\/v1\/agent\/jobs\/[^/]+$/.test(api)) {
      const value = state.jobs.get(api.split('/').at(-1));
      json(res, value ?? { detail: 'Unknown job' }, value ? 200 : 404);
    } else if (/^\/v1\/agent\/threads\/[^/]+\/messages$/.test(api)) {
      const threadId = api.split('/')[4];
      const value = [...state.jobs.values()].find((item) => item.thread_id === threadId);
      json(res, {
        thread_id: threadId,
        pending_messages: [],
        queue_paused_reason: null,
        latest_job_id: value.id,
        latest_turn_no: value.turn_no,
        latest_job_state: value.state,
      });
    } else if (/^\/v1\/agent\/mcp\/threads\/[^/]+\/bindings$/.test(api))
      json(res, { bindings: [] });
    else if (/^\/v1\/agent\/jobs\/[^/]+\/usage$/.test(api))
      json(res, {
        ledger: {
          status: 'pending',
          source: 'gateway_api_logs',
          attribution: 'grant_time_window',
          finalized: false,
          totals: null,
          unknown_rows: null,
          estimated_calls: 0,
          queried_at: null,
          missing_attempts: 0,
          attempts: [],
        },
        runtime: {
          source: 'runtime',
          input_tokens: null,
          output_tokens: null,
          cache_read_tokens: null,
        },
      });
    else if (/^\/v1\/agent\/jobs\/[^/]+\/git$/.test(api))
      json(res, { available: true, branch: 'main', changes: [], patch: '', commits: [] });
    else if (/\/thread$/.test(api)) {
      const value = state.jobs.get(api.split('/')[4]);
      json(res, {
        thread_id: value.thread_id,
        title: value.title,
        repo: value.repo,
        jobs: [value],
        messages: [
          {
            id: 1,
            role: 'user',
            content: value.task_prompt,
            job_id: value.id,
            created_at: timestamp,
          },
        ],
        pending_messages: [],
      });
    } else if (/\/thread\/events$/.test(api)) json(res, { events: [], next_cursor: 0 });
    else if (/\/thread\/environment$/.test(api))
      json(res, {
        environment_id: null,
        environment_name: null,
        variables: [],
        setup_script: '',
        start_script: '',
        actions: [],
      });
    else if (/\/thread\/stats$/.test(api)) json(res, { detail: 'No stats' }, 404);
    else if (/\/terminals$/.test(api)) {
      if (req.method === 'POST')
        state.terminals.push({
          id: `terminal-${state.terminals.length + 1}`,
          shell: 'bash',
          state: 'running',
          cwd: '/workspace',
          rows: 24,
          cols: 80,
          last_seq: 0,
        });
      json(
        res,
        req.method === 'POST'
          ? state.terminals.at(-1)
          : { terminals: state.terminals, max_sessions: 4 },
      );
    } else if (/\/terminals\/[^/]+\/(input|resize)$/.test(api)) json(res, {});
    else if (/\/terminals\/[^/]+\/stream$/.test(api)) {
      state.terminalStreams += 1;
      stream(req, res);
    } else if (/\/stream$/.test(api) || api === '/v1/agent/changes') stream(req, res);
    else {
      state.unknown.push(`${req.method} ${api}`);
      json(res, { detail: `Unimplemented browser fixture: ${api}` }, 501);
    }
    return true;
  }
  return { handle, close };
}
