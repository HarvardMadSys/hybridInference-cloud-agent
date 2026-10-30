'use client';

import type { CatalogItem, ManagementCatalog, ManagementMethod } from '@/lib/api/adminManagement';

const SECTION = 'mt-6 rounded-lg border border-border p-4';

const METHODS: Record<ManagementMethod, string> = {
  edit: 'Edit',
  apply: 'Apply to a machine',
  workflow: 'Guided operation',
  observe: 'Observed',
  object: 'Managed with its object',
};

function where(item: CatalogItem): { href: string; label: string } | null {
  if (item.owner.startsWith('settings')) return { href: '/admin/settings', label: 'Settings' };
  if (item.owner === 'host_settings' || item.owner === 'servers') {
    return { href: '/admin/hosts', label: 'Servers' };
  }
  if (item.owner.startsWith('workflow:database')) return { href: '#database', label: 'Database' };
  if (item.owner.startsWith('workflow:')) return { href: '#keys', label: 'Keys and credentials' };
  if (item.owner === 'deployment:release') return { href: '#releases', label: 'Releases' };
  if (item.owner.startsWith('deployment:')) return { href: '#machines', label: 'Machines' };
  return null;
}

/** Every configuration item by function: where its value comes from and how it changes. */
export function ConfigurationMap({ catalog }: { catalog: ManagementCatalog | null }) {
  if (!catalog) return null;
  const groups = new Map<string, CatalogItem[]>();
  for (const item of catalog.items) {
    groups.set(item.group, [...(groups.get(item.group) ?? []), item]);
  }
  return (
    <section className={SECTION} aria-labelledby="configuration">
      <h2 id="configuration" className="text-base font-semibold">
        Where configuration lives
      </h2>
      {[...groups].map(([group, items]) => (
        <div key={group} className="mt-3">
          <h3 className="text-sm font-semibold">{group}</h3>
          <table className="mt-1 w-full text-left text-xs">
            <tbody>
              {items.map((item) => {
                const link = where(item);
                return (
                  <tr key={item.id} className="border-t border-border align-top">
                    <td className="w-1/3 py-1 pr-3">
                      <div className="font-medium">{item.label}</div>
                      <div className="text-muted">{item.description}</div>
                    </td>
                    <td className="py-1 pr-3">
                      {item.sources.map((source) => source.meaning).join('; ')}
                    </td>
                    <td className="py-1">
                      {METHODS[item.method]}
                      {link && (
                        <>
                          {' · '}
                          <a className="underline" href={link.href}>
                            {link.label}
                          </a>
                        </>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ))}
    </section>
  );
}
