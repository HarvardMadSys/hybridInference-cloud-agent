// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import * as api from '@/lib/api/skills';
import { getAgentConfig } from '@/lib/api/agents';
import { DEFAULT_SKILL_LIMITS } from '@/lib/skillLimits';
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
vi.mock('@/lib/api/agents', () => ({ getAgentConfig: vi.fn() }));
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
  vi.mocked(getAgentConfig).mockResolvedValue({
    repos: [],
    runtimes: [],
    models: [],
    github_connected: false,
    github_install_url: null,
    skill_limits: DEFAULT_SKILL_LIMITS,
  });
  vi.mocked(api.listSkills).mockResolvedValue([skill]);
  vi.mocked(api.getSkill).mockResolvedValue(skill);
});

it('advertises and checks the current server skill size in the editor', async () => {
  vi.mocked(getAgentConfig).mockResolvedValue({
    repos: [],
    runtimes: [],
    models: [],
    github_connected: false,
    github_install_url: null,
    skill_limits: { ...DEFAULT_SKILL_LIMITS, max_skill_bytes: 8 },
  });
  render(<SkillsSection />);
  await screen.findByRole('button', { name: 'Manage Code review' });
  fireEvent.click(screen.getByRole('button', { name: 'Create skill' }));
  const dialog = within(screen.getByRole('dialog', { name: 'Create skill' }));
  await dialog.findByText('Markdown supported · up to 8 B.');
  fireEvent.change(dialog.getByLabelText('Name'), { target: { value: 'Review' } });
  fireEvent.change(dialog.getByLabelText(/^Description/), { target: { value: 'Review' } });
  fireEvent.change(dialog.getByLabelText(/^Instructions/), { target: { value: '字字字' } });
  fireEvent.click(dialog.getByRole('button', { name: 'Create skill' }));
  expect(await dialog.findByRole('alert')).toHaveTextContent('no larger than 8 B');
  expect(api.createSkill).not.toHaveBeenCalled();
});

it('checks the current server upload limit before reading a skill document', async () => {
  vi.mocked(getAgentConfig).mockResolvedValue({
    repos: [],
    runtimes: [],
    models: [],
    github_connected: false,
    github_install_url: null,
    skill_limits: { ...DEFAULT_SKILL_LIMITS, max_skill_bytes: 4 },
  });
  render(<SkillImportDialog onClose={vi.fn()} onSaved={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: 'Upload SKILL.md' }));
  await screen.findByText(/Up to 4 B/);
  const file = new File(['hello'], 'SKILL.md', { type: 'text/markdown' });
  const read = vi.fn().mockResolvedValue('hello');
  Object.defineProperty(file, 'text', { value: read });
  fireEvent.change(screen.getByLabelText(/^Skill file/), { target: { files: [file] } });
  fireEvent.click(screen.getByRole('button', { name: 'Preview skill' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('no larger than 4 B');
  expect(read).not.toHaveBeenCalled();
  expect(api.previewSkillImport).not.toHaveBeenCalled();
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
  fireEvent.click(await screen.findByRole('button', { name: 'Manage Code review' }));
  fireEvent.click(screen.getByRole('menuitem', { name: 'Remove skill' }));
  expect(api.deleteSkill).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm removal' }));
  await screen.findByText('Your workflows, ready to reuse');
  expect(api.deleteSkill).toHaveBeenCalledWith(1, 1);
});

it('opens the current instructions for editing from the menu', async () => {
  render(<SkillsSection />);
  fireEvent.click(await screen.findByRole('button', { name: 'Manage Code review' }));
  fireEvent.click(screen.getByRole('menuitem', { name: 'Edit skill' }));
  expect(await screen.findByLabelText(/^Instructions/)).toHaveValue(skill.instructions);
  expect(api.getSkill).toHaveBeenCalledWith(skill.id);
  expect(screen.queryByRole('menu')).not.toBeInTheDocument();
});

it('supports keyboard menu navigation, dismissal, and returning focus', async () => {
  render(<SkillsSection />);
  const trigger = await screen.findByRole('button', { name: 'Manage Code review' });
  fireEvent.click(trigger);
  expect(screen.getByRole('menuitem', { name: 'View details' })).toHaveFocus();
  fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
  expect(screen.getByRole('menuitem', { name: 'Edit skill' })).toHaveFocus();
  fireEvent.keyDown(document.activeElement!, { key: 'End' });
  expect(screen.getByRole('menuitem', { name: 'Remove skill' })).toHaveFocus();
  fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
  expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
  fireEvent.click(trigger);
  fireEvent.pointerDown(document.body);
  expect(screen.queryByRole('menu')).not.toBeInTheDocument();
});

it('shows when a skill was added and keeps its complete description in details', async () => {
  const added = new Date();
  added.setDate(added.getDate() - 3);
  vi.mocked(api.listSkills).mockResolvedValue([
    { ...skill, created_at: added.toISOString(), updated_at: new Date().toISOString() },
  ]);
  render(<SkillsSection />);
  expect((await screen.findAllByText('Added 3 days ago'))[0]).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'View Code review' }));
  expect(within(screen.getByRole('dialog')).getByText(skill.description)).toBeInTheDocument();
  expect(await screen.findByText(skill.instructions)).toBeInTheDocument();
});

it('shows only names and switches in the conversation picker without changing account defaults', async () => {
  const changed = vi.fn();
  render(<SkillPicker selected={[]} onChange={changed} />);
  fireEvent.click(screen.getByRole('button', { name: /0 selected/ }));
  const toggle = await screen.findByRole('switch', { name: skill.name });
  expect(toggle).not.toBeChecked();
  expect(screen.queryByText(skill.description)).not.toBeInTheDocument();
  expect(screen.queryByText(/KB selected/)).not.toBeInTheDocument();
  fireEvent.click(toggle);
  expect(toggle).toBeChecked();
  expect(changed).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
  expect(changed).toHaveBeenCalledWith([skill]);
  expect(api.updateSkill).not.toHaveBeenCalled();
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
  fireEvent.click(await screen.findByRole('switch'));
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(changed).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: /1 selected/ }));
  expect(await screen.findByRole('switch')).toBeChecked();
  fireEvent.click(screen.getByRole('switch'));
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
  fireEvent.click(screen.getByRole('switch'));
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
  await screen.findByRole('switch');
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
  await screen.findByRole('switch');
  fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
  expect(changed).not.toHaveBeenCalled();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('blocks oversized combinations and allows a selection at the limit', async () => {
  const changed = vi.fn();
  vi.mocked(api.listSkills).mockResolvedValue([
    { ...skill, instruction_bytes: 64 * 1024 },
    { ...skill, id: 2, name: 'Large two', instruction_bytes: 64 * 1024 },
    { ...skill, id: 3, name: 'Large three', instruction_bytes: 1 },
  ]);
  render(<SkillPicker selected={[]} onChange={changed} />);
  fireEvent.click(screen.getByRole('button', { name: /0 selected/ }));
  const checkboxes = await screen.findAllByRole('switch');
  for (const checkbox of checkboxes) fireEvent.click(checkbox);
  expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled();
  expect(screen.getByRole('alert')).toHaveTextContent('Unselect a skill');
  fireEvent.click(checkboxes[2]);
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled();
  fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
  expect(changed).toHaveBeenCalledWith([
    expect.objectContaining({ id: 1 }),
    expect.objectContaining({ id: 2 }),
  ]);
});
