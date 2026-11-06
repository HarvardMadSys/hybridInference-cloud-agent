import { boundedLine } from './trajectory';
import type { AssistantBlock } from './trajectorySteps';

/**
 * Grow an answer's step by one streamed piece, or start its text over.
 *
 * A one-line preview cut at its limit is final for text that only grows:
 * whatever is appended lies past the part it shows. So an answer in `cut` keeps
 * its preview until a piece rewrites the text, and a long answer is not read
 * whole again for every piece that lands.
 */
export function growAnswer(
  step: { blocks?: AssistantBlock[]; detail: string },
  piece: string,
  rewrite: boolean,
  cut: WeakSet<object>,
): void {
  const text = rewrite ? piece : (step.blocks?.[0]?.text ?? '') + piece;
  step.blocks = [{ type: 'text', text, channel: 'summary' }];
  if (rewrite || !cut.has(step)) {
    const line = boundedLine(text);
    step.detail = line.text;
    if (line.full) cut.add(step);
    else cut.delete(step);
  }
}
