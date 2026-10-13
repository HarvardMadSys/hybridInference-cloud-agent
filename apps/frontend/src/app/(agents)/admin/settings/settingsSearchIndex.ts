import type { HostSettingDefinition } from '@/lib/api/admin';
import type { AgentAdminSettings } from '@/lib/api/adminSettings';

import { agentName } from './AgentVersions';
import { applicationCategory, applicationSettingGroup } from './settingsOrganization';

export interface SettingsSearchItem {
  id: string;
  label: string;
  description: string;
  section: string;
  tab: string;
  targetId: string;
  applicationKey?: string;
  keywords?: string[];
}

interface StaticSearchField {
  key: string;
  label: string;
  description: string;
  keywords?: string[];
}

const CLOUDLAB_FIELDS: StaticSearchField[] = [
  {
    key: 'project',
    label: 'CloudLab project',
    description: 'Project that will own new experiments.',
  },
  {
    key: 'duration',
    label: 'Initial duration (hours)',
    description: 'Choose the project and initial lifetime for new CloudLab servers.',
    keywords: ['lifetime'],
  },
  {
    key: 'profile_project',
    label: 'Template project',
    description: 'Project that publishes the CloudLab profile.',
  },
  {
    key: 'profile_name',
    label: 'Template name',
    description: 'Name of the CloudLab profile to use.',
  },
  {
    key: 'ssh_username',
    label: 'CloudLab SSH user',
    description: 'Customize the machine template and SSH access used for new servers.',
  },
  {
    key: 'ssh_public_key',
    label: 'CloudLab SSH public key',
    description:
      'Installed for this user on new experiments. Leave blank to use keys already on the CloudLab account.',
  },
];

const ARCHIVE_FIELDS: StaticSearchField[] = [
  {
    key: 'enabled',
    label: 'Enable sandbox archives',
    description:
      'With an access key, uploads use that key. Without one, brokers use their workload identity and need the bucket owner and KMS key. Bucket versioning is required either way.',
  },
  {
    key: 's3_uri',
    label: 'S3 destination',
    description: 'Fixed by the deployment security policy and not included in update requests.',
  },
  {
    key: 'region',
    label: 'AWS region',
    description: 'Enable archives and configure their destination, ownership, and encryption.',
  },
  {
    key: 'aws_access_key_id',
    label: 'AWS access key ID',
    description: 'Use this instead of the bucket owner and KMS key.',
  },
  {
    key: 'aws_secret_access_key',
    label: 'AWS secret access key',
    description: 'Write-only. It is not shown again after saving.',
  },
  {
    key: 'expected_bucket_owner',
    label: 'Expected bucket owner',
    description: 'Enable archives and configure their destination, ownership, and encryption.',
  },
  {
    key: 'kms_key_arn',
    label: 'Customer-managed KMS key ARN',
    description: 'Enable archives and configure their destination, ownership, and encryption.',
  },
  {
    key: 'max_archive_bytes',
    label: 'Maximum archive bytes',
    description: 'Maximum compressed size of one archive. Hard limit: 4 GiB.',
  },
  {
    key: 'multipart_threshold_bytes',
    label: 'Multipart threshold bytes',
    description: 'Archives at or above this size use multipart upload. Maximum: 64 MiB.',
  },
  {
    key: 'multipart_part_bytes',
    label: 'Multipart part bytes',
    description: 'Size of each uploaded part, from 5 MiB through 64 MiB.',
  },
];

function staticItems(
  fields: StaticSearchField[],
  prefix: string,
  section: string,
  tab: string,
): SettingsSearchItem[] {
  return fields.map(({ key, label, description, keywords = [] }) => ({
    id: `${prefix}:${key}`,
    label,
    description,
    section,
    tab,
    targetId: `${prefix}-setting-${key}`,
    keywords: [key, ...keywords],
  }));
}

export function buildSettingsSearchItems({
  snapshot,
  hostDefinitions,
  runtimes,
  archiveAvailable,
  applicationTab,
}: {
  snapshot: AgentAdminSettings | null;
  hostDefinitions: HostSettingDefinition[];
  runtimes: string[];
  archiveAvailable: boolean;
  applicationTab: string;
}): SettingsSearchItem[] {
  return [
    ...(snapshot?.definitions ?? []).map((definition) => {
      const category = applicationCategory(definition);
      const tab =
        category === 'Sandbox archives' ? 'sandbox' : `application-${encodeURIComponent(category)}`;
      return {
        id: `application:${definition.key}`,
        label: definition.label,
        description: definition.description,
        section: category === 'Sandbox archives' ? 'Sandbox' : category,
        tab,
        targetId: `application-setting-${definition.key}`,
        applicationKey: definition.key,
        keywords: [definition.key, definition.category, applicationSettingGroup(definition)],
      };
    }),
    ...(snapshot?.bootstrap ?? []).map((definition) => ({
      id: `bootstrap:${definition.key}`,
      label: definition.label,
      description: definition.description,
      section: 'Deployment setup',
      tab: applicationTab,
      targetId: `application-bootstrap-${definition.key}`,
      keywords: [definition.key],
    })),
    ...hostDefinitions.map((definition) => ({
      id: `host:${definition.key}`,
      label: definition.label,
      description: definition.description,
      section: 'Sandbox',
      tab: 'sandbox',
      targetId: `host-setting-${definition.key}`,
      keywords: [definition.key, definition.env, 'all servers'],
    })),
    ...runtimes.map((runtime) => ({
      id: `runtime:${runtime}`,
      label: agentName(runtime),
      description: 'Choose the default version for each agent.',
      section: 'Agent versions',
      tab: 'versions',
      targetId: `agent-version-${runtime}`,
      keywords: [runtime],
    })),
    ...(archiveAvailable
      ? staticItems(ARCHIVE_FIELDS, 'archive', 'Sandbox archives', 'sandbox')
      : []),
    ...staticItems(CLOUDLAB_FIELDS, 'cloudlab', 'CloudLab', 'cloudlab'),
  ];
}

function normalizeSearch(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, ' ')
    .trim();
}

/** Search only setting metadata, never saved values, defaults, or editor drafts. */
export function searchSettingsItems(
  items: SettingsSearchItem[],
  query: string,
): SettingsSearchItem[] {
  const normalizedQuery = normalizeSearch(query);
  if (!normalizedQuery) return [];
  const terms = normalizedQuery.split(/\s+/);
  return items.filter((item) => {
    const text = normalizeSearch(
      [item.label, item.description, item.section, ...(item.keywords ?? [])].join(' '),
    );
    return terms.every((term) => text.includes(term));
  });
}
