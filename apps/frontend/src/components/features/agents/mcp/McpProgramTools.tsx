import Link from 'next/link';
import type { McpCapabilityApi } from '@/lib/api/mcp';

/**
 * A program's tools, listed for the conversation. A program runs with the
 * agent and its calls are not held for approval, so there is no policy to set.
 */
export function McpProgramTools({ capabilities }: { capabilities: McpCapabilityApi[] }) {
  return (
    <details className="mt-3">
      <summary className="w-fit cursor-pointer text-xs text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100">
        Tools
      </summary>
      <ul className="mt-2 space-y-1">
        {capabilities
          .filter((item) => item.kind === 'tool')
          .map((item) => (
            <li key={item.name} className="text-xs text-gray-600 dark:text-gray-300">
              <span className="font-mono">{item.name}</span>
              {item.description ? (
                <span className="text-gray-500 dark:text-gray-400"> · {item.description}</span>
              ) : null}
            </li>
          ))}
      </ul>
    </details>
  );
}

/** What a binding with no listed tools says: a program's appear once a chat uses it. */
export function McpNoToolsYet({ program }: { program: boolean }) {
  return (
    <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
      {program ? (
        'Its tools appear once a chat has used it.'
      ) : (
        <>
          No tools discovered yet.{' '}
          <Link href="/settings/plugins" className="underline">
            Refresh the connection
          </Link>
        </>
      )}
    </p>
  );
}
