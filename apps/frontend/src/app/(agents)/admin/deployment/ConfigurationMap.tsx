'use client';

import Link from 'next/link';

import type { CatalogItem, ManagementCatalog } from '@/lib/api/adminManagement';

import { categoryTabId } from '../settings/ApplicationSettings';
import { itemDescription, itemLabel, readable } from './deploymentCopy';
import { GroupCard, Panel } from './deploymentUi';

export type DeploymentTab = 'overview' | 'machines' | 'releases' | 'keys' | 'database' | 'activity';

interface Place {
  title: string;
  description: string;
  link?: { label: string } & ({ href: string } | { tab: DeploymentTab });
}

/** Where administrators change things, in the order they are usually looked for. */
const PLACES: Record<string, Place> = {
  servers: {
    title: 'On the Servers page',
    description: 'Saved for all servers or for one, and reported back by each server.',
    link: { href: '/admin/hosts', label: 'Open Servers' },
  },
  machines: {
    title: 'Per machine, under Machines',
    description: 'Reviewed and applied by each machine’s deployment agent.',
    link: { tab: 'machines', label: 'Open Machines' },
  },
  releases: {
    title: 'Under Releases',
    description: 'Rolled out to every machine in order.',
    link: { tab: 'releases', label: 'Open Releases' },
  },
  keys: {
    title: 'Under Keys and credentials',
    description: 'Changed by guided rotations that keep everything working throughout.',
    link: { tab: 'keys', label: 'Open Keys and credentials' },
  },
  database: {
    title: 'Under Database',
    description: 'Changed by guided operations that prove each step.',
    link: { tab: 'database', label: 'Open Database' },
  },
  reported: {
    title: 'Reported, not changed here',
    description: 'Shown as the machines report them; they are set where the machine is installed.',
  },
};

function placeOf(item: CatalogItem): keyof typeof PLACES {
  const { owner, method } = item;
  if (method === 'observe') return 'reported';
  if (owner === 'object:admin_credentials') return 'keys';
  if (owner === 'host_settings' || owner === 'servers' || owner.startsWith('object:')) {
    return 'servers';
  }
  if (owner.startsWith('workflow:database')) return 'database';
  if (owner.startsWith('workflow:')) return 'keys';
  if (owner === 'deployment:release') return 'releases';
  return 'machines';
}

const LINK_CLASS =
  'inline-flex min-h-9 items-center rounded-lg px-2 text-sm font-medium text-accent underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson dark:focus-visible:ring-crimson-light';

function PlaceLink({
  link,
  onOpen,
}: {
  link: NonNullable<Place['link']>;
  onOpen: (tab: DeploymentTab) => void;
}) {
  if ('href' in link) {
    return (
      <Link href={link.href} className={LINK_CLASS}>
        {link.label}
      </Link>
    );
  }
  return (
    <a
      href={`?tab=${link.tab}`}
      className={LINK_CLASS}
      onClick={(event) => {
        event.preventDefault();
        onOpen(link.tab);
      }}
    >
      {link.label}
    </a>
  );
}

/** Application settings: one entry per category, each opening its own Settings tab. */
function SettingsCategories({ items }: { items: CatalogItem[] }) {
  if (items.length === 0) return null;
  return (
    <GroupCard
      title="On the Settings page"
      icon="link"
      description="Application settings. Each setting says there when a change takes effect."
      actions={
        <Link href="/admin/settings" className={LINK_CLASS}>
          Open Settings
        </Link>
      }
    >
      <ul className="flex flex-wrap gap-2 px-4 py-4 sm:px-5">
        {items.map((item) => (
          <li key={item.id}>
            <Link
              href={`/admin/settings?tab=${categoryTabId(item.label)}`}
              className="inline-flex min-h-9 items-center rounded-full border border-border bg-surface-muted/60 px-3 text-sm text-foreground hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson dark:focus-visible:ring-crimson-light"
            >
              {readable(item.label) || itemLabel(item)}
            </Link>
          </li>
        ))}
      </ul>
    </GroupCard>
  );
}

/** Every configuration item by where it is changed, in an administrator's words. */
export function ConfigurationMap({
  catalog,
  onOpen,
}: {
  catalog: ManagementCatalog | null;
  onOpen: (tab: DeploymentTab) => void;
}) {
  if (!catalog) return null;
  const settings = catalog.items.filter((item) => item.owner.startsWith('settings'));
  const places = new Map<string, CatalogItem[]>(Object.keys(PLACES).map((key) => [key, []]));
  for (const item of catalog.items) {
    if (!item.owner.startsWith('settings')) places.get(placeOf(item))?.push(item);
  }
  return (
    <Panel
      title="Where configuration lives"
      description="Every part of the configuration, by the place it is changed."
    >
      <SettingsCategories items={settings} />
      {[...places].map(([key, items]) => {
        if (items.length === 0) return null;
        const place = PLACES[key];
        return (
          <GroupCard
            key={key}
            title={place.title}
            icon={key === 'reported' ? 'review' : 'settings'}
            description={place.description}
            actions={place.link && <PlaceLink link={place.link} onOpen={onOpen} />}
          >
            <ul>
              {items.map((item) => {
                const description = itemDescription(item);
                return (
                  <li
                    key={item.id}
                    className="grid min-w-0 gap-1 border-b border-border/60 px-4 py-3 last:border-b-0 sm:px-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)] lg:gap-8"
                  >
                    <p className="break-words text-sm font-medium text-foreground">
                      {itemLabel(item)}
                    </p>
                    {description && (
                      <p className="break-words text-xs leading-5 text-muted">{description}</p>
                    )}
                  </li>
                );
              })}
            </ul>
          </GroupCard>
        );
      })}
    </Panel>
  );
}
