// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AgentRunnerHost } from '@/lib/api/admin';
import type { SandboxArchiveSettings } from '@/lib/api/adminArchiveSettings';
import { SANDBOX_ARCHIVE_SETTINGS_CAPABILITY } from '@/lib/api/adminArchiveSettings';

import SandboxArchiveSettingsCard from './SandboxArchiveSettings';

const KMS_ARN = 'arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000';

function settings(over: Partial<SandboxArchiveSettings> = {}): SandboxArchiveSettings {
  return {
    enabled: false,
    s3_uri: 's3://freeinference/cloud-agent/sandbox',
    region: 'us-east-1',
    kms_key_arn: KMS_ARN,
    expected_bucket_owner: '123456789012',
    max_archive_bytes: 2_147_483_648,
    multipart_threshold_bytes: 8_388_608,
    multipart_part_bytes: 8_388_608,
    revision: 7,
    updated_at: null,
    ...over,
  };
}

afterEach(cleanup);

describe('sandbox archive settings card', () => {
  it('shows the fixed destination read-only and never asks for AWS credentials', () => {
    render(<SandboxArchiveSettingsCard settings={settings()} busy={false} onSave={vi.fn()} />);

    expect(screen.getByLabelText('S3 destination')).toHaveValue(
      's3://freeinference/cloud-agent/sandbox',
    );
    expect(screen.getByLabelText('S3 destination')).toHaveAttribute('readonly');
    expect(screen.getByLabelText('S3 destination')).toHaveAccessibleDescription(
      'Fixed by the deployment security policy and not included in update requests.',
    );
    expect(screen.queryByLabelText(/access key/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/secret/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save archive settings' })).toBeDisabled();
    expect(screen.getByLabelText('Maximum archive bytes')).toHaveAccessibleDescription(
      'Maximum compressed size of one archive. Hard limit: 4 GiB. Current: 2 GiB.',
    );
    expect(screen.getByLabelText('Enable sandbox archives').closest('details')).toBeNull();
    expect(screen.getByLabelText('AWS region').closest('details')).toBeNull();
    expect(screen.getByLabelText('Expected bucket owner').closest('details')).toBeNull();
    expect(screen.getByLabelText('Customer-managed KMS key ARN').closest('details')).toBeNull();
    const advanced = screen.getByText('Advanced').closest('details');
    expect(advanced).not.toHaveAttribute('open');
    expect(advanced).toContainElement(screen.getByLabelText('Maximum archive bytes'));
    expect(advanced).toContainElement(screen.getByLabelText('Multipart threshold bytes'));
    expect(advanced).toContainElement(screen.getByLabelText('Multipart part bytes'));
  });

  it('separates saved policy from broker rollout by revision and capability', () => {
    const hosts = [
      {
        host: 'runner-a',
        capabilities: [SANDBOX_ARCHIVE_SETTINGS_CAPABILITY],
        sandbox_archive_settings_applied_revision: 7,
      },
      {
        host: 'runner-b',
        capabilities: [SANDBOX_ARCHIVE_SETTINGS_CAPABILITY],
        sandbox_archive_settings_applied_revision: 6,
      },
      { host: 'runner-c', capabilities: [] },
    ] as AgentRunnerHost[];
    render(
      <SandboxArchiveSettingsCard
        settings={settings()}
        hosts={hosts}
        busy={false}
        onSave={vi.fn()}
      />,
    );

    expect(screen.getByText(/Applied on 1 of 2 compatible servers/)).toHaveTextContent(
      'Applied on 1 of 2 compatible servers. Waiting for runner-b. runner-c needs a redeploy.',
    );
  });

  it('requires region, owner, and a customer-managed KMS key before enabling', () => {
    const onSave = vi.fn();
    render(
      <SandboxArchiveSettingsCard
        settings={settings({ region: '', kms_key_arn: '', expected_bucket_owner: '' })}
        busy={false}
        onSave={onSave}
      />,
    );

    fireEvent.click(screen.getByLabelText('Enable sandbox archives'));
    fireEvent.click(screen.getByRole('button', { name: 'Save archive settings' }));

    expect(screen.getByText('Enter the AWS region.')).toBeInTheDocument();
    expect(screen.getByText('Enter the expected bucket owner.')).toBeInTheDocument();
    expect(screen.getByText('Enter the customer-managed KMS key ARN.')).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
  });

  it('saves all seven mutable values and the revision together', () => {
    const onSave = vi.fn();
    render(<SandboxArchiveSettingsCard settings={settings()} busy={false} onSave={onSave} />);

    fireEvent.click(screen.getByLabelText('Enable sandbox archives'));
    fireEvent.click(screen.getByRole('button', { name: 'Save archive settings' }));

    expect(onSave).toHaveBeenCalledWith({
      enabled: true,
      region: 'us-east-1',
      kms_key_arn: KMS_ARN,
      expected_bucket_owner: '123456789012',
      max_archive_bytes: 2_147_483_648,
      multipart_threshold_bytes: 8_388_608,
      multipart_part_bytes: 8_388_608,
      revision: 7,
    });
    expect(onSave.mock.calls[0][0]).not.toHaveProperty('s3_uri');
  });

  it('preserves collapsed upload edits and saves them with important settings', () => {
    const onSave = vi.fn();
    render(<SandboxArchiveSettingsCard settings={settings()} busy={false} onSave={onSave} />);

    const advanced = screen.getByText('Advanced').closest('details')!;
    const summary = advanced.querySelector('summary')!;
    fireEvent.click(summary);
    expect(advanced).toHaveAttribute('open');
    const maximum = screen.getByLabelText('Maximum archive bytes');
    fireEvent.change(maximum, { target: { value: '1073741824' } });
    fireEvent.click(summary);

    expect(advanced).not.toHaveAttribute('open');
    expect(screen.getByLabelText('Maximum archive bytes')).toBe(maximum);
    expect(maximum).toHaveValue(1_073_741_824);
    fireEvent.click(screen.getByLabelText('Enable sandbox archives'));
    fireEvent.click(screen.getByRole('button', { name: 'Save archive settings' }));

    expect(onSave).toHaveBeenCalledWith({
      enabled: true,
      region: 'us-east-1',
      kms_key_arn: KMS_ARN,
      expected_bucket_owner: '123456789012',
      max_archive_bytes: 1_073_741_824,
      multipart_threshold_bytes: 8_388_608,
      multipart_part_bytes: 8_388_608,
      revision: 7,
    });
  });

  it('requires the KMS region and account to match the related fields', () => {
    const onSave = vi.fn();
    render(
      <SandboxArchiveSettingsCard
        settings={settings({ enabled: true })}
        busy={false}
        onSave={onSave}
      />,
    );

    fireEvent.change(screen.getByLabelText('AWS region'), { target: { value: 'us-west-2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save archive settings' }));
    expect(
      screen.getByText('The KMS key region must match the archive region.'),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('AWS region'), { target: { value: 'us-east-1' } });
    fireEvent.change(screen.getByLabelText('Expected bucket owner'), {
      target: { value: '210987654321' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save archive settings' }));
    expect(
      screen.getByText('The KMS key account must match the expected bucket owner.'),
    ).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
  });

  it('enforces integer byte bounds and keeps the multipart threshold within the archive limit', () => {
    const onSave = vi.fn();
    render(<SandboxArchiveSettingsCard settings={settings()} busy={false} onSave={onSave} />);

    const advanced = screen.getByText('Advanced').closest('details')!;
    const summary = advanced.querySelector('summary')!;
    fireEvent.click(summary);
    fireEvent.change(screen.getByLabelText('Maximum archive bytes'), {
      target: { value: '100' },
    });
    fireEvent.change(screen.getByLabelText('Multipart threshold bytes'), {
      target: { value: '101' },
    });
    fireEvent.change(screen.getByLabelText('Multipart part bytes'), {
      target: { value: '5242879' },
    });
    fireEvent.click(summary);
    expect(advanced).not.toHaveAttribute('open');
    fireEvent.click(screen.getByRole('button', { name: 'Save archive settings' }));

    expect(advanced).toHaveAttribute('open');
    expect(screen.getByText('Enter a whole number from 1 to 100.')).toBeInTheDocument();
    expect(screen.getByText('Enter a whole number from 5242880 to 67108864.')).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();

    fireEvent.click(summary);
    expect(advanced).not.toHaveAttribute('open');
    fireEvent.click(screen.getByRole('button', { name: 'Save archive settings' }));
    expect(advanced).toHaveAttribute('open');
    expect(onSave).not.toHaveBeenCalled();
  });

  it('keeps an unsaved draft while a failed parent save toggles busy state', () => {
    const current = settings();
    const view = render(
      <SandboxArchiveSettingsCard settings={current} busy={false} onSave={vi.fn()} />,
    );
    fireEvent.change(screen.getByLabelText('AWS region'), { target: { value: 'us-west-2' } });

    view.rerender(<SandboxArchiveSettingsCard settings={current} busy onSave={vi.fn()} />);
    view.rerender(<SandboxArchiveSettingsCard settings={current} busy={false} onSave={vi.fn()} />);

    expect(screen.getByLabelText('AWS region')).toHaveValue('us-west-2');
  });

  it('preserves a draft when another admin advances the revision', () => {
    const current = settings();
    const view = render(
      <SandboxArchiveSettingsCard settings={current} busy={false} onSave={vi.fn()} />,
    );
    fireEvent.change(screen.getByLabelText('Maximum archive bytes'), {
      target: { value: '1073741824' },
    });

    view.rerender(
      <SandboxArchiveSettingsCard
        settings={settings({ revision: 8, max_archive_bytes: 536_870_912 })}
        busy={false}
        onSave={vi.fn()}
      />,
    );

    expect(screen.getByLabelText('Maximum archive bytes')).toHaveValue(1_073_741_824);
    expect(screen.getByRole('alert')).toHaveTextContent(
      'These settings changed while you were editing',
    );
    expect(screen.getByRole('button', { name: 'Save archive settings' })).toBeDisabled();

    fireEvent.click(
      screen.getByRole('button', { name: 'Discard the draft and load the latest settings' }),
    );
    expect(screen.getByLabelText('Maximum archive bytes')).toHaveValue(536_870_912);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('refreshes a clean editor when a newer revision arrives', async () => {
    const view = render(
      <SandboxArchiveSettingsCard settings={settings()} busy={false} onSave={vi.fn()} />,
    );

    view.rerender(
      <SandboxArchiveSettingsCard
        settings={settings({ revision: 8, max_archive_bytes: 536_870_912 })}
        busy={false}
        onSave={vi.fn()}
      />,
    );

    await waitFor(() =>
      expect(screen.getByLabelText('Maximum archive bytes')).toHaveValue(536_870_912),
    );
  });
});
