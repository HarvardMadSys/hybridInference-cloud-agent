// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import type { AgentConfigApi } from '@/lib/api/agents';
import { providedSource } from '@/test/connectionConfig';

import { ModelConnectionNotice } from './ModelConnectionNotice';

function config(overrides: Partial<AgentConfigApi> = {}): AgentConfigApi {
  return {
    repos: [],
    runtimes: ['codex'],
    github_connected: true,
    github_install_url: null,
    model_connections_enabled: true,
    model_connections: [],
    ...overrides,
  };
}

afterEach(cleanup);

describe('ModelConnectionNotice', () => {
  it('asks an owner with nothing to run on to set up a connection', () => {
    render(<ModelConnectionNotice config={config({ model_connection_required: true })} />);
    expect(screen.getByRole('status')).toHaveTextContent('Connect your model to start a task');
    expect(screen.getByRole('link', { name: 'Set up a model connection' })).toHaveAttribute(
      'href',
      '/settings/models#settings-connections',
    );
  });

  it('says nothing to an owner the deployment provides a model to', () => {
    const provided = [providedSource({ 'house-fast': [100, 0] })];
    const { container, rerender } = render(
      <ModelConnectionNotice
        config={config({ model_connection_required: false, provided_sources: provided })}
      />,
    );
    expect(container).toBeEmptyDOMElement();
    // A provided model that can run is enough, whatever else an answer says.
    rerender(
      <ModelConnectionNotice
        config={config({ model_connection_required: true, provided_sources: provided })}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('names the provided connection, never a provider, when it offers this owner nothing', () => {
    render(
      <ModelConnectionNotice
        config={config({
          model_connection_required: true,
          provided_sources: [providedSource({}, { name: 'House models' })],
        })}
      />,
    );
    const notice = screen.getByRole('status');
    expect(notice).toHaveTextContent(
      'House models, which this deployment provides, has no model this account can use right now.',
    );
    expect(notice).toHaveTextContent('add a connection of your own');
    expect(screen.getByRole('link', { name: 'Set up a model connection' })).toBeInTheDocument();
  });

  it('offers no setup where the deployment has switched model connections off', () => {
    render(
      <ModelConnectionNotice
        config={config({ model_connection_required: true, model_connections_enabled: false })}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent('Model connections are unavailable');
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('says nothing before the configuration has loaded', () => {
    const { container } = render(<ModelConnectionNotice config={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
