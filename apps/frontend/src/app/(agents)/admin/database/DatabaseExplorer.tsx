import { useEffect, useRef, useState } from 'react';
import {
  getDatabaseTables,
  queryDatabase,
  type DatabaseFilter,
  type DatabaseFilterOperator,
  type DatabaseQuery,
  type DatabaseQueryResult,
  type DatabaseTable,
} from '@/lib/api/adminDatabase';
import { getErrorMessage } from '@/lib/utils/errors';
import { accessDenied, trackedRequest, type DatabaseAccessProps } from './requests';

const INPUT =
  'min-h-10 rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-crimson disabled:opacity-50 dark:border-white/10 dark:bg-gray-950 dark:text-gray-100';
const BUTTON =
  'min-h-10 rounded-lg border border-gray-200 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson disabled:cursor-not-allowed disabled:opacity-50 dark:border-white/10 dark:text-gray-200 dark:hover:bg-gray-800';
const MAX_OFFSET = 100_000;
const MAX_FILTERS = 12;
const OPERATORS: [DatabaseFilterOperator, string][] = [
  ['eq', 'Equals'],
  ['ne', 'Does not equal'],
  ['contains', 'Contains'],
  ['gt', 'Greater than'],
  ['gte', 'Greater than or equal'],
  ['lt', 'Less than'],
  ['lte', 'Less than or equal'],
  ['is_null', 'Is null'],
  ['is_not_null', 'Is not null'],
];
const TEXT_TYPES = new Set(['text', 'character varying', 'character']);
const ORDERED_TYPES = new Set([
  'smallint',
  'integer',
  'bigint',
  'real',
  'double precision',
  'numeric',
  'date',
  'timestamp with time zone',
  'timestamp without time zone',
]);

function operatorsFor(dataType: string) {
  return OPERATORS.filter(([operator]) => {
    if (operator === 'contains') return TEXT_TYPES.has(dataType);
    if (['gt', 'gte', 'lt', 'lte'].includes(operator)) return ORDERED_TYPES.has(dataType);
    return true;
  });
}

function valueHint(dataType: string | undefined): string | null {
  if (dataType === 'boolean') return 'Use true or false.';
  if (dataType === 'date') return 'Use an ISO date, e.g. 2026-10-10.';
  if (dataType === 'timestamp with time zone')
    return 'Use an ISO timestamp with a timezone, e.g. 2026-10-10T12:00:00Z.';
  if (dataType === 'timestamp without time zone')
    return 'Use an ISO timestamp without a timezone, e.g. 2026-10-10T12:00:00.';
  return null;
}

function needsValue(operator: DatabaseFilterOperator): boolean {
  return operator !== 'is_null' && operator !== 'is_not_null';
}

export function DatabaseExplorer({ trackRequest, onDenied }: DatabaseAccessProps) {
  const [tables, setTables] = useState<DatabaseTable[]>([]);
  const [tableName, setTableName] = useState('');
  const [loadingTables, setLoadingTables] = useState(true);
  const [tablesError, setTablesError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [filters, setFilters] = useState<DatabaseFilter[]>([]);
  const [orderBy, setOrderBy] = useState('');
  const [direction, setDirection] = useState<'asc' | 'desc'>('asc');
  const [limit, setLimit] = useState(50);
  const [querying, setQuerying] = useState(false);
  const [queryError, setQueryError] = useState<string | null>(null);
  const [result, setResult] = useState<DatabaseQueryResult | null>(null);
  const [appliedQuery, setAppliedQuery] = useState<DatabaseQuery | null>(null);
  const request = useRef<ReturnType<typeof trackedRequest> | null>(null);
  const lastQuery = useRef<DatabaseQuery | null>(null);
  const table = tables.find((candidate) => candidate.name === tableName);

  useEffect(() => {
    const { controller, release } = trackedRequest(trackRequest);
    setLoadingTables(true);
    setTablesError(null);
    void getDatabaseTables(controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return;
        setTables(next.tables);
        setTableName(next.tables[0]?.name ?? '');
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        if (accessDenied(cause)) onDenied();
        else setTablesError(getErrorMessage(cause));
      })
      .finally(() => {
        release();
        if (!controller.signal.aborted) setLoadingTables(false);
      });
    return () => {
      controller.abort();
      release();
    };
  }, [trackRequest, onDenied, reload]);

  useEffect(
    () => () => {
      request.current?.controller.abort();
      request.current?.release();
    },
    [],
  );

  function clearResult() {
    request.current?.controller.abort();
    request.current?.release();
    request.current = null;
    lastQuery.current = null;
    setQuerying(false);
    setResult(null);
    setAppliedQuery(null);
    setQueryError(null);
  }

  function updateFilter(index: number, patch: Partial<DatabaseFilter>) {
    clearResult();
    setFilters((current) =>
      current.map((filter, position) => {
        if (position !== index) return filter;
        const next = { ...filter, ...patch };
        if (patch.column) {
          const column = table?.columns.find((candidate) => candidate.name === patch.column);
          if (
            column &&
            !operatorsFor(column.data_type).some(([operator]) => operator === next.operator)
          )
            next.operator = 'eq';
        }
        return next;
      }),
    );
  }

  async function runQuery(query: DatabaseQuery) {
    if (request.current) return;
    const pending = trackedRequest(trackRequest);
    request.current = pending;
    lastQuery.current = query;
    setQuerying(true);
    setQueryError(null);
    setResult(null);
    setAppliedQuery(null);
    try {
      const next = await queryDatabase(query, pending.controller.signal);
      if (pending.controller.signal.aborted) return;
      setResult(next);
      setAppliedQuery(query);
    } catch (cause: unknown) {
      if (pending.controller.signal.aborted) return;
      if (accessDenied(cause)) onDenied();
      else setQueryError(getErrorMessage(cause));
    } finally {
      pending.release();
      if (request.current === pending) request.current = null;
      if (!pending.controller.signal.aborted) setQuerying(false);
    }
  }

  function submitQuery() {
    if (!table) return;
    void runQuery({
      table: table.name,
      filters: filters.map((filter) =>
        needsValue(filter.operator)
          ? { ...filter, value: filter.value ?? '' }
          : { column: filter.column, operator: filter.operator },
      ),
      order_by: orderBy || null,
      order_direction: direction,
      limit,
      offset: 0,
    });
  }

  return (
    <div className="mt-8 rounded-2xl border border-gray-200 bg-white p-6 dark:border-white/10 dark:bg-gray-900">
      <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-50">Database explorer</h2>
      <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">
        Browse reviewed metadata columns and query rows with read-only filters. Sensitive content
        and credential fields are excluded. All filters must match.
      </p>
      {loadingTables && (
        <p role="status" className="mt-5 text-sm text-gray-500 dark:text-gray-400">
          Loading database tables…
        </p>
      )}
      {tablesError && (
        <div className="mt-5">
          <p role="alert" className="text-sm text-red-700 dark:text-red-400">
            {tablesError}
          </p>
          <button
            type="button"
            className={`${BUTTON} mt-3`}
            onClick={() => setReload((n) => n + 1)}
          >
            Retry loading tables
          </button>
        </div>
      )}
      {!loadingTables && !tablesError && tables.length === 0 && (
        <p role="status" className="mt-5 text-sm text-gray-500 dark:text-gray-400">
          No tables are available to browse.
        </p>
      )}
      {table && (
        <>
          <label className="mt-5 flex max-w-lg flex-col gap-2 text-sm text-gray-700 dark:text-gray-300">
            Table
            <select
              className={INPUT}
              value={tableName}
              onChange={(event) => {
                clearResult();
                setTableName(event.target.value);
                setFilters([]);
                setOrderBy('');
                setDirection('asc');
              }}
            >
              {tables.map((candidate) => (
                <option key={candidate.name} value={candidate.name}>
                  {candidate.name}
                </option>
              ))}
            </select>
          </label>
          <details className="mt-4 text-sm text-gray-700 dark:text-gray-300">
            <summary className="cursor-pointer rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson">
              Columns and types ({table.columns.length})
            </summary>
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-left text-sm">
                <caption className="sr-only">Columns in {table.name}</caption>
                <thead>
                  <tr>
                    {['Column', 'Type', 'Nullable', 'Primary key'].map((name) => (
                      <th key={name} scope="col" className="px-3 py-2 font-medium">
                        {name}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {table.columns.map((column) => (
                    <tr key={column.name} className="border-t border-gray-100 dark:border-white/5">
                      <th scope="row" className="px-3 py-2 font-mono font-normal">
                        {column.name}
                      </th>
                      <td className="px-3 py-2 font-mono">{column.data_type}</td>
                      <td className="px-3 py-2">{column.nullable ? 'Yes' : 'No'}</td>
                      <td className="px-3 py-2">
                        {table.primary_key.includes(column.name) ? 'Yes' : 'No'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
          <form
            className="mt-6"
            onSubmit={(event) => {
              event.preventDefault();
              submitQuery();
            }}
          >
            <fieldset>
              <legend className="text-sm font-semibold text-gray-900 dark:text-gray-100">
                Filters
              </legend>
              {filters.length === 0 && (
                <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                  No filters. Query all rows.
                </p>
              )}
              {filters.map((filter, index) => {
                const column = table.columns.find((candidate) => candidate.name === filter.column);
                const hint = valueHint(column?.data_type);
                return (
                  <div key={index} className="mt-3 flex flex-wrap items-end gap-3">
                    <label className="flex flex-col gap-1 text-xs text-gray-600 dark:text-gray-400">
                      Column {index + 1}
                      <select
                        className={INPUT}
                        value={filter.column}
                        onChange={(event) => updateFilter(index, { column: event.target.value })}
                      >
                        {table.columns.map((column) => (
                          <option key={column.name} value={column.name}>
                            {column.name}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="flex flex-col gap-1 text-xs text-gray-600 dark:text-gray-400">
                      Operator {index + 1}
                      <select
                        className={INPUT}
                        value={filter.operator}
                        onChange={(event) =>
                          updateFilter(index, {
                            operator: event.target.value as DatabaseFilterOperator,
                          })
                        }
                      >
                        {operatorsFor(column?.data_type ?? '').map(([operator, name]) => (
                          <option key={operator} value={operator}>
                            {name}
                          </option>
                        ))}
                      </select>
                    </label>
                    {needsValue(filter.operator) && (
                      <div className="text-xs text-gray-600 dark:text-gray-400">
                        <label className="flex flex-col gap-1">
                          Value {index + 1}
                          <input
                            className={INPUT}
                            value={filter.value ?? ''}
                            onChange={(event) => updateFilter(index, { value: event.target.value })}
                            placeholder="Value"
                            aria-describedby={hint ? `database-filter-hint-${index}` : undefined}
                          />
                        </label>
                        {hint && (
                          <p id={`database-filter-hint-${index}`} className="mt-1">
                            {hint}
                          </p>
                        )}
                      </div>
                    )}
                    <button
                      type="button"
                      className={BUTTON}
                      aria-label={`Remove filter ${index + 1}`}
                      onClick={() => {
                        clearResult();
                        setFilters((current) =>
                          current.filter((_, position) => position !== index),
                        );
                      }}
                    >
                      Remove
                    </button>
                  </div>
                );
              })}
              <button
                type="button"
                className={`${BUTTON} mt-3`}
                disabled={table.columns.length === 0 || filters.length >= MAX_FILTERS}
                onClick={() => {
                  clearResult();
                  setFilters((current) => [
                    ...current,
                    { column: table.columns[0].name, operator: 'eq', value: '' },
                  ]);
                }}
              >
                Add filter
              </button>
            </fieldset>
            <div className="mt-5 flex flex-wrap items-end gap-3">
              <label className="flex flex-col gap-1 text-xs text-gray-600 dark:text-gray-400">
                Sort by
                <select
                  className={INPUT}
                  value={orderBy}
                  onChange={(event) => {
                    clearResult();
                    setOrderBy(event.target.value);
                  }}
                >
                  <option value="">Default order</option>
                  {table.columns.map((column) => (
                    <option key={column.name} value={column.name}>
                      {column.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1 text-xs text-gray-600 dark:text-gray-400">
                Sort direction
                <select
                  className={INPUT}
                  value={direction}
                  disabled={!orderBy}
                  onChange={(event) => {
                    clearResult();
                    setDirection(event.target.value as 'asc' | 'desc');
                  }}
                >
                  <option value="asc">Ascending</option>
                  <option value="desc">Descending</option>
                </select>
              </label>
              <label className="flex flex-col gap-1 text-xs text-gray-600 dark:text-gray-400">
                Rows per page
                <select
                  className={INPUT}
                  value={limit}
                  onChange={(event) => {
                    clearResult();
                    setLimit(Number(event.target.value));
                  }}
                >
                  {[25, 50, 100, 200].map((size) => (
                    <option key={size} value={size}>
                      {size}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="submit"
                disabled={querying}
                className="min-h-10 rounded-lg bg-crimson px-4 py-2 text-sm font-semibold text-white hover:bg-crimson/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 dark:focus-visible:ring-offset-gray-900"
              >
                Run query
              </button>
            </div>
          </form>
          {querying && (
            <p role="status" className="mt-5 text-sm text-gray-500 dark:text-gray-400">
              Running query…
            </p>
          )}
          {queryError && (
            <div className="mt-5">
              <p role="alert" className="text-sm text-red-700 dark:text-red-400">
                {queryError}
              </p>
              <button
                type="button"
                className={`${BUTTON} mt-3`}
                onClick={() => {
                  if (lastQuery.current) void runQuery(lastQuery.current);
                }}
              >
                Retry query
              </button>
            </div>
          )}
          {!result && !querying && !queryError && (
            <p className="mt-5 text-sm text-gray-500 dark:text-gray-400">
              Choose filters and run a query to view rows.
            </p>
          )}
          {result && appliedQuery && (
            <QueryResults
              result={result}
              previous={() =>
                void runQuery({
                  ...appliedQuery,
                  offset: Math.max(0, result.offset - result.limit),
                })
              }
              next={() => void runQuery({ ...appliedQuery, offset: result.offset + result.limit })}
            />
          )}
        </>
      )}
    </div>
  );
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined) return 'NULL';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function QueryResults({
  result,
  previous,
  next,
}: {
  result: DatabaseQueryResult;
  previous: () => void;
  next: () => void;
}) {
  return (
    <div className="mt-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p role="status" className="text-sm text-gray-600 dark:text-gray-400">
          {result.rows.length === 0
            ? 'No rows matched your query.'
            : `Rows ${result.offset + 1}–${result.offset + result.rows.length}`}{' '}
          · {result.elapsed_ms.toLocaleString('en-US', { maximumFractionDigits: 1 })} ms
        </p>
        <div className="flex gap-2">
          <button
            type="button"
            className={BUTTON}
            disabled={result.offset === 0}
            onClick={previous}
          >
            Previous page
          </button>
          <button
            type="button"
            className={BUTTON}
            disabled={!result.has_more || result.offset + result.limit > MAX_OFFSET}
            onClick={next}
          >
            Next page
          </button>
        </div>
      </div>
      {result.has_more && result.offset + result.limit > MAX_OFFSET && (
        <p className="mt-2 text-sm text-amber-700 dark:text-amber-400">
          Pagination limit reached. Refine your filters to browse more matching rows.
        </p>
      )}
      <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
        Text cells are limited to {result.text_limit} characters; shortened values end with ….
      </p>
      {result.rows.length > 0 && (
        <div className="mt-4 overflow-x-auto rounded-lg border border-gray-200 dark:border-white/10">
          <table className="w-full text-left text-sm text-gray-700 dark:text-gray-300">
            <caption className="sr-only">Query results for {result.table}</caption>
            <thead className="bg-gray-50 dark:bg-gray-950">
              <tr>
                {result.columns.map((column) => (
                  <th
                    key={column}
                    scope="col"
                    className="whitespace-nowrap px-3 py-2 font-mono font-medium"
                  >
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 dark:divide-white/5">
              {result.rows.map((row, index) => (
                <tr key={index}>
                  {row.map((value, column) => (
                    <td
                      key={column}
                      className="max-w-lg whitespace-pre-wrap break-words px-3 py-2 align-top font-mono text-xs"
                    >
                      {displayValue(value)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
