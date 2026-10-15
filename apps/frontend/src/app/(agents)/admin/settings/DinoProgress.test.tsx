// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import DinoProgress from './DinoProgress';

afterEach(cleanup);

describe('DinoProgress', () => {
  it('reports a whole percentage inside 0–100 to assistive technology', () => {
    const { rerender } = render(<DinoProgress progress={42.6} label="Downloading" />);
    const bar = screen.getByRole('progressbar', { name: 'Downloading' });
    expect(bar).toHaveAttribute('aria-valuenow', '43');
    expect(screen.getByText('43%')).toBeInTheDocument();
    rerender(<DinoProgress progress={140} label="Downloading" />);
    expect(bar).toHaveAttribute('aria-valuenow', '100');
    rerender(<DinoProgress progress={-3} label="Downloading" />);
    expect(bar).toHaveAttribute('aria-valuenow', '0');
  });

  it('draws a dinosaur with two strides that the stylesheet alternates', () => {
    const { container } = render(<DinoProgress progress={10} label="Downloading" />);
    const svg = container.querySelector('svg');
    expect(svg).toHaveAttribute('aria-hidden', 'true');
    expect(container.querySelector('.ui-dino-stride-a')).not.toBeNull();
    expect(container.querySelector('.ui-dino-stride-b')).not.toBeNull();
  });
});
