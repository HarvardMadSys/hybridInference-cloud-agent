import type { AgentAdminSettings } from '@/lib/api/adminSettings';

export function applicationSnapshot(over: Partial<AgentAdminSettings> = {}): AgentAdminSettings {
  return {
    revision: 1,
    definitions: [
      {
        key: 'site.app_name',
        label: 'Application name',
        description: 'Name shown in the application.',
        category: 'Appearance',
        kind: 'string',
        default: 'Cloud Agent',
        secret: false,
      },
      {
        key: 'identity.registration_enabled',
        label: 'Registration enabled',
        description: 'Allow new users to register.',
        category: 'Identity & access',
        kind: 'boolean',
        default: true,
        secret: false,
      },
      {
        key: 'future.maximum',
        label: 'Future limit',
        description: 'A setting introduced by the server.',
        category: 'Future / category ✓',
        kind: 'integer',
        default: 10,
        minimum: 1,
        maximum: 100,
        secret: false,
      },
    ],
    values: {
      'site.app_name': 'Cloud Agent',
      'identity.registration_enabled': true,
      'future.maximum': 10,
    },
    secret_configured: {},
    bootstrap: [],
    ...over,
  };
}
