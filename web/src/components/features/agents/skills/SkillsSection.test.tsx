// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import * as api from '@/lib/api/skills';
import { SkillsSection } from './SkillsSection';
import { SkillPicker } from './SkillPicker';
import { SkillImportDialog } from './SkillImportDialog';

vi.mock('@/lib/api/skills', () => ({
  listSkills: vi.fn(),
  getSkill: vi.fn(),
  createSkill: vi.fn(),
  updateSkill: vi.fn(),
  deleteSkill: vi.fn(),
  previewSkillImport: vi.fn(),
  importGitHubSkill: vi.fn(),
}));
const skill: api.SkillApi = {
  id: 1,
  name: 'Code review',
  description: 'Review a pull request',
  instructions: 'Read the diff.',
  source: 'created',
  source_url: null,
  source_revision: null,
  revision: 1,
  default_enabled: false,
  created_at: '2026-09-17T00:00:00Z',
  updated_at: '2026-09-17T00:00:00Z',
};
beforeEach(() => {
  vi.mocked(api.listSkills).mockResolvedValue([skill]);
  vi.mocked(api.getSkill).mockResolvedValue(skill);
});
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

it('creates a skill from the empty state and saves its default', async () => {
  vi.mocked(api.listSkills).mockResolvedValue([]);
  vi.mocked(api.createSkill).mockResolvedValue({ ...skill, default_enabled: true });
  render(<SkillsSection />);
  await screen.findByText('Your workflows, ready to reuse');
  fireEvent.click(screen.getByRole('button', { name: 'Create skill' }));
  const dialog = within(screen.getByRole('dialog', { name: 'Create skill' }));
  fireEvent.change(dialog.getByLabelText('Name'), { target: { value: skill.name } });
  fireEvent.change(dialog.getByLabelText(/^Description/), { target: { value: skill.description } });
  fireEvent.change(dialog.getByLabelText(/^Instructions/), {
    target: { value: skill.instructions },
  });
  fireEvent.click(dialog.getByRole('checkbox', { name: /Use by default/ }));
  fireEvent.click(dialog.getByRole('button', { name: 'Create skill' }));
  expect(await screen.findByRole('switch', { name: /Code review/ })).toBeChecked();
  expect(api.createSkill).toHaveBeenCalledWith({
    name: skill.name,
    description: skill.description,
    instructions: skill.instructions,
    default_enabled: true,
    source: 'created',
  });
});

it('uses revision fencing and refreshes a conflicting default without claiming success', async () => {
  vi.mocked(api.updateSkill).mockRejectedValue(
    new Error('This skill changed. Refresh it before saving.'),
  );
  render(<SkillsSection />);
  fireEvent.click(await screen.findByRole('switch'));
  expect(await screen.findByRole('alert')).toHaveTextContent('This skill changed');
  await waitFor(() => expect(api.listSkills).toHaveBeenCalledTimes(2));
  expect(api.updateSkill).toHaveBeenCalledWith(1, { revision: 1, default_enabled: true });
  expect(screen.getByRole('switch')).not.toBeChecked();
  expect(screen.queryByText('Defaults updated for new chats.')).not.toBeInTheDocument();
});

it('requires explicit removal and sends the inspected revision', async () => {
  vi.mocked(api.deleteSkill).mockResolvedValue(undefined);
  render(<SkillsSection />);
  fireEvent.click(await screen.findByRole('button', { name: 'View Code review' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Remove skill' }));
  expect(api.deleteSkill).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Confirm removal' }));
  await screen.findByText('Your workflows, ready to reuse');
  expect(api.deleteSkill).toHaveBeenCalledWith(1, 1);
});

it('copies imported instructions into a custom skill and leaves the source unchanged', async () => {
  vi.mocked(api.getSkill).mockResolvedValue({ ...skill, source: 'github' });
  vi.mocked(api.createSkill).mockResolvedValue({ ...skill, id: 2, name: 'Code review copy' });
  render(<SkillsSection />);
  fireEvent.click(await screen.findByRole('button', { name: 'View Code review' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Create a copy' }));
  expect(screen.getByLabelText('Name')).toHaveValue('Code review copy');
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Create skill' }));
  await screen.findByRole('button', { name: 'View Code review copy' });
  expect(api.updateSkill).not.toHaveBeenCalled();
  expect(api.createSkill).toHaveBeenCalledWith(
    expect.objectContaining({
      source: 'created',
      instructions: skill.instructions,
      default_enabled: false,
    }),
  );
});

it('previews GitHub content before writing and confirms the reviewed digest', async () => {
  const preview: api.SkillImportPreviewApi = {
    name: skill.name,
    description: skill.description,
    instructions: skill.instructions,
    source: 'github',
    source_url: 'https://github.com/example/skills',
    source_revision: 'a'.repeat(40),
    content_digest: 'b'.repeat(64),
  };
  vi.mocked(api.previewSkillImport).mockResolvedValue(preview);
  vi.mocked(api.importGitHubSkill).mockResolvedValue(skill);
  const saved = vi.fn();
  render(<SkillImportDialog onClose={vi.fn()} onSaved={saved} />);
  fireEvent.change(screen.getByLabelText(/^GitHub URL/), { target: { value: preview.source_url } });
  fireEvent.click(screen.getByRole('button', { name: 'Preview skill' }));
  await screen.findByRole('dialog', { name: 'Review skill' });
  expect(api.importGitHubSkill).not.toHaveBeenCalled();
  expect(api.createSkill).not.toHaveBeenCalled();
  expect(screen.getByText(/Referenced files and scripts are not included/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Import skill' }));
  await waitFor(() => expect(saved).toHaveBeenCalledWith(skill));
  expect(api.importGitHubSkill).toHaveBeenCalledWith({
    url: preview.source_url,
    name: skill.name,
    content_digest: preview.content_digest,
    default_enabled: false,
  });
});

it('previews uploaded SKILL.md and saves the reviewed instructions only after confirmation', async () => {
  const markdown =
    '---\nname: Code review\ndescription: Review a pull request\n---\nRead the diff.';
  const file = new File([markdown], 'SKILL.md', { type: 'text/markdown' });
  // jsdom File does not implement Blob.text on every supported Node version.
  Object.defineProperty(file, 'text', { value: async () => markdown });
  vi.mocked(api.previewSkillImport).mockResolvedValue({
    ...skill,
    source: 'uploaded',
    content_digest: null,
  });
  vi.mocked(api.createSkill).mockResolvedValue(skill);
  const saved = vi.fn();
  render(<SkillImportDialog onClose={vi.fn()} onSaved={saved} />);
  fireEvent.click(screen.getByRole('button', { name: 'Upload SKILL.md' }));
  fireEvent.change(screen.getByLabelText(/^Skill file/), { target: { files: [file] } });
  fireEvent.click(screen.getByRole('button', { name: 'Preview skill' }));
  await screen.findByRole('dialog', { name: 'Review skill' });
  expect(api.previewSkillImport).toHaveBeenCalledWith({ content: markdown });
  expect(api.createSkill).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Import skill' }));
  await waitFor(() => expect(saved).toHaveBeenCalledWith(skill));
  expect(api.createSkill).toHaveBeenCalledWith(
    expect.objectContaining({ source: 'uploaded', instructions: skill.instructions }),
  );
});

it('keeps the current selection on cancel and applies an explicit empty selection', async () => {
  const changed = vi.fn();
  render(<SkillPicker selected={[skill]} onChange={changed} />);
  fireEvent.click(screen.getByRole('button', { name: /1 selected/ }));
  fireEvent.click(await screen.findByRole('checkbox'));
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(changed).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: /1 selected/ }));
  expect(await screen.findByRole('checkbox')).toBeChecked();
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
  expect(changed).toHaveBeenCalledWith([]);
});

it('requires deselecting a removed saved skill before applying changes', async () => {
  vi.mocked(api.listSkills).mockResolvedValue([]);
  const changed = vi.fn();
  render(<SkillPicker selected={[skill]} onChange={changed} />);
  fireEvent.click(screen.getByRole('button', { name: /1 selected/ }));
  await screen.findByText(/Removed from library/);
  expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
  expect(changed).toHaveBeenCalledWith([]);
});

it('closes on Escape without letting the conversation shortcut handle it', async () => {
  const outside = vi.fn();
  render(
    <div onKeyDown={outside}>
      <SkillPicker selected={[]} onChange={vi.fn()} />
    </div>,
  );
  fireEvent.click(screen.getByRole('button', { name: /0 selected/ }));
  await screen.findByRole('checkbox');
  fireEvent.keyDown(screen.getByRole('button', { name: 'Apply' }), { key: 'Escape' });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(outside).not.toHaveBeenCalled();
});

it('keeps inherited content when Apply leaves the selected IDs unchanged', async () => {
  const changed = vi.fn();
  vi.mocked(api.listSkills).mockResolvedValue([
    { ...skill, revision: 9, description: 'New revision' },
  ]);
  render(<SkillPicker selected={[skill]} onChange={changed} />);
  fireEvent.click(screen.getByRole('button', { name: /1 selected/ }));
  await screen.findByRole('checkbox');
  fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
  expect(changed).not.toHaveBeenCalled();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('shows selected bytes and blocks oversized combinations before submission', async () => {
  const changed = vi.fn();
  vi.mocked(api.listSkills).mockResolvedValue([
    { ...skill, instruction_bytes: 64 * 1024 },
    { ...skill, id: 2, name: 'Large two', instruction_bytes: 64 * 1024 },
    { ...skill, id: 3, name: 'Large three', instruction_bytes: 1 },
  ]);
  render(<SkillPicker selected={[]} onChange={changed} />);
  fireEvent.click(screen.getByRole('button', { name: /0 selected/ }));
  const checkboxes = await screen.findAllByRole('checkbox');
  for (const checkbox of checkboxes) fireEvent.click(checkbox);
  expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled();
  expect(screen.getByRole('alert')).toHaveTextContent('Unselect a skill');
  fireEvent.click(checkboxes[2]);
  expect(screen.getByText('128.0 / 128 KB selected')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled();
});
