// The host's resource and attempt telemetry rollups, renamed into the UI's shape.

import type {
  AgentAttemptSummaryApi,
  AgentAttemptTelemetryApi,
  AttemptResourcesApi,
  ProcessObservationApi,
  ToolCallResourcesApi,
} from '@/lib/api/agents';
import type { AgentAttempt, ProcessObservation, ToolCallResources, TurnResources } from '../types';

/** Rename the host's rollup into the UI's shape, keeping every null a null. */
export function toTurnResources(api: AttemptResourcesApi): TurnResources {
  const toolCallsTruncated = api.tool_calls_truncated === true;
  const toolCalls = (api.tool_calls ?? []).map((call) => {
    const adapted = toToolCallResources(call);
    if (!toolCallsTruncated) return adapted;
    // A dropped call may have started processes the retained rows now
    // claim; nothing per-call survives a truncated list, exit-accounted or
    // sampled alike.
    return {
      ...adapted,
      sampleCount: 0,
      measuredSpanMs: null,
      cpuUsec: null,
      memPeakBytes: null,
      exclusive: false,
      processCpuUsec: null,
      processMemPeakBytes: null,
      processIoReadBytes: null,
      processIoWriteBytes: null,
      processResourcesPartial: false,
    };
  });
  const processObserver = api.process_observer ?? null;
  const processEventsLost = api.process_events_lost ?? 0;
  const processesTruncated = api.processes_truncated === true;
  const processObservationComplete =
    processObserver !== null &&
    api.process_observation_complete === true &&
    processEventsLost === 0 &&
    !processesTruncated;
  // An incomplete census may have lost a process a call started, so no
  // call's exit-accounted numbers can be presented as its total. The host
  // marks this too; the reader is kept conservative even against an older
  // or wrong row.
  const conservativeCalls = toolCalls.map((call) => {
    if (processObservationComplete || (call.processCount ?? 0) === 0) return call;
    return { ...call, processResourcesPartial: true };
  });
  return {
    cpuUsec: api.cpu_usec,
    memPeakBytes: api.mem_peak_bytes,
    memAvgBytes: api.mem_avg_bytes,
    ioReadBytes: api.io_read_bytes,
    ioWriteBytes: api.io_write_bytes,
    netRxBytes: api.net_rx_bytes,
    netTxBytes: api.net_tx_bytes,
    sampleCount: api.sample_count,
    samplerVersion: api.sampler_version,
    coverage: api.coverage,
    toolTimeMs: toolCallsTruncated ? null : (api.tool_time_ms ?? null),
    toolCalls: conservativeCalls,
    toolCallsTruncated,
    processObserver,
    processObservationComplete,
    processEventsLost,
    processesTruncated,
    processes: (api.processes ?? []).map(toProcessObservation),
  };
}

/**
 * Collapse the exclusive/shared column pair into one number and a flag.
 *
 * The pair exists in the database so a shared measurement cannot be read as
 * an exclusive one by a query that does not know the difference. Here the
 * difference is carried by `exclusive`, and every renderer of these numbers
 * has to answer it — a value with no flag beside it is what this shape makes
 * impossible.
 */
function toToolCallResources(api: ToolCallResourcesApi): ToolCallResources {
  const exclusive = api.cpu_usec_exclusive !== null || api.mem_peak_exclusive_bytes !== null;
  return {
    toolUseId: api.tool_use_id,
    toolName: api.tool_name,
    timingSource: api.timing_source === 'exact' ? 'exact' : 'message',
    durationMs: api.duration_ms,
    sampleCount: api.sample_count,
    overlapN: api.overlap_n,
    measuredSpanMs: api.measured_span_ms,
    cpuUsec: exclusive ? api.cpu_usec_exclusive : api.cpu_usec_shared,
    memPeakBytes: exclusive ? api.mem_peak_exclusive_bytes : api.mem_peak_shared_bytes,
    exclusive,
    processCount: api.process_count ?? 0,
    processCpuUsec: api.process_cpu_usec ?? null,
    processMemPeakBytes: api.process_mem_peak_bytes ?? null,
    processIoReadBytes: api.process_io_read_bytes ?? null,
    processIoWriteBytes: api.process_io_write_bytes ?? null,
    processResourcesPartial: api.process_resources_partial === true,
  };
}

function toProcessObservation(api: ProcessObservationApi): ProcessObservation {
  return {
    processSeq: api.process_seq,
    uid: api.uid,
    pid: api.pid,
    tgid: api.tgid,
    ppid: api.ppid,
    comm: api.comm,
    executable: api.executable,
    startedOffsetMs: api.started_offset_ms,
    durationMs: api.duration_ms,
    exitCode: api.exit_code,
    observedFork: api.observed_fork,
    observedExec: api.observed_exec,
    observedExit: api.observed_exit,
    toolCallSeqs: api.tool_call_seqs,
    cpuUsec: api.cpu_usec ?? null,
    memPeakBytes: api.mem_peak_bytes ?? null,
    ioReadBytes: api.io_read_bytes ?? null,
    ioWriteBytes: api.io_write_bytes ?? null,
    ioRcharBytes: api.io_rchar_bytes ?? null,
    ioWcharBytes: api.io_wchar_bytes ?? null,
  };
}

export function toAttemptTelemetry(
  api: AgentAttemptSummaryApi & Partial<Pick<AgentAttemptTelemetryApi, 'resources'>>,
): NonNullable<AgentAttempt['telemetry']> {
  return {
    ...(api.continuity_mode ? { continuityMode: api.continuity_mode } : {}),
    ...(api.context_status ? { contextStatus: api.context_status } : {}),
    ...(typeof api.duration_ms === 'number' ? { durationMs: api.duration_ms } : {}),
    ...(api.resources ? { resources: toTurnResources(api.resources) } : {}),
  };
}
