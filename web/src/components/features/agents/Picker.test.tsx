// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { Picker } from './Picker';

describe('Picker', () => {
  afterEach(() => cleanup());

  it('renders nothing when there is nothing to choose between', () => {
    const { container } = render(<Picker label="Model" value="" options={[]} onChange={vi.fn()} />);

    expect(container).toBeEmptyDOMElement();
  });

  it('draws the selected value, so its width follows the selection', () => {
    // A bare <select> is as wide as its *widest* option, which made the model
    // picker show `glm-5.3` followed by blank space sized for a model nobody
    // had chosen. The drawn value is what fixes that, so it is what is pinned.
    render(
      <Picker
        label="Model"
        value="glm-5.3"
        options={['glm-5.3', 'kimi-k2.7-code-highspeed']}
        onChange={vi.fn()}
      />,
    );

    const drawn = screen.getByText('glm-5.3', { selector: 'span' });
    expect(drawn).toBeInTheDocument();
    expect(drawn).toHaveAttribute('aria-hidden');
    // Long ids are truncated rather than allowed to push the run button away.
    expect(drawn).toHaveClass('truncate');
  });

  it('still selects through a real, named, keyboard-reachable control', () => {
    // The value is painted, but the <select> underneath keeps its options and
    // its accessible name — the picker must not become a div that looks like
    // a control.
    const onChange = vi.fn();
    render(
      <Picker
        label="Model"
        value="glm-5.3"
        options={['glm-5.3', 'qwen-next']}
        onChange={onChange}
      />,
    );

    const select = screen.getByLabelText('Model');
    expect(Array.from(select.querySelectorAll('option')).map((o) => o.textContent)).toEqual([
      'glm-5.3',
      'qwen-next',
    ]);

    fireEvent.change(select, { target: { value: 'qwen-next' } });
    expect(onChange).toHaveBeenCalledWith('qwen-next');
  });
});
