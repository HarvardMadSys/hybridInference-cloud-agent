import Link from 'next/link';

export function AdminNavigation({
  current,
}: {
  current: 'servers' | 'runs' | 'conversations' | 'usage';
}) {
  const section = current === 'runs' ? 'conversations' : current;
  return (
    <nav
      aria-label="Admin sections"
      className="mt-6 flex gap-1 overflow-x-auto border-b border-gray-200 dark:border-white/10"
    >
      {[
        { key: 'servers', label: 'Servers', href: '/admin/hosts' },
        { key: 'conversations', label: 'Conversations', href: '/admin/conversations' },
        { key: 'usage', label: 'Usage', href: '/admin/usage' },
      ].map((item) => (
        <Link
          key={item.key}
          href={item.href}
          aria-current={section === item.key ? 'page' : undefined}
          className={`-mb-px shrink-0 border-b-2 px-4 py-3 text-sm font-medium transition-colors ${
            section === item.key
              ? 'border-crimson text-crimson dark:border-crimson-light dark:text-crimson-light'
              : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-900 dark:text-gray-400 dark:hover:border-white/20 dark:hover:text-gray-100'
          }`}
        >
          {item.label}
        </Link>
      ))}
    </nav>
  );
}
