import { modelConnectionsEn, modelConnectionsZhCN } from './modelConnections';
import { modelSourcesEn, modelSourcesZhCN } from './modelSources';
import { providedModelsEn, providedModelsZhCN } from './providedModels';

export const modelSelectionEn = {
  ...modelConnectionsEn,
  ...modelSourcesEn,
  ...providedModelsEn,
  'settings.model.title': 'Default model',
  'settings.model.description':
    'Choose a model for new tasks. You can change it for any individual task.',
  'settings.model.label': 'Default model',
  'settings.model.choose': 'Choose a model',
  'settings.model.reset': 'Reset to default',
  'settings.model.saving': 'Saving…',
  'settings.model.noneApplies':
    'Nothing is chosen for new tasks yet. Pick a model here or in the composer.',
  'settings.model.retired': '{model} (no longer available)',
  'settings.model.effective': 'New tasks start on {model}.',
  'settings.model.noModels': 'No model is available yet. A new task waits until one is offered.',
  'settings.model.loading': 'Loading your default model…',
  'settings.model.error': 'Could not load your default model.',
  'settings.model.saveError': 'Could not save. Your previous choice is unchanged.',
  'settings.model.retry': 'Try again',
  'composer.chooseModel': 'Choose a model first',
  'composer.chooseModelPlaceholder': 'Choose model',
  'picker.search': 'Search…',
  'picker.noResults': 'No matching options',
  'composer.model': 'Model',
  'composer.reasoning': 'Reasoning',
  'composer.reasoningForTurn': 'Reasoning for this turn',
  'effort.auto': 'Auto',
  'effort.autoOnly': 'Auto only',
  'effort.unknown':
    "This model's provider does not declare which reasoning levels it accepts, so it runs at the provider's default. Refresh its reasoning levels in Settings once the provider declares them.",
  'effort.unsupported':
    "This model's provider declares no reasoning level this agent can send, so it runs at the provider's default.",
  'effort.dropped': '{level} is not offered for this model; reasoning is back on Auto.',
} as const;

export const modelSelectionZhCN = {
  ...modelConnectionsZhCN,
  ...modelSourcesZhCN,
  ...providedModelsZhCN,
  'settings.model.title': '默认模型',
  'settings.model.description': '设置新任务使用的模型，创建任务时仍可更改。',
  'settings.model.label': '默认模型',
  'settings.model.choose': '选择模型',
  'settings.model.reset': '恢复默认',
  'settings.model.saving': '正在保存…',
  'settings.model.noneApplies':
    '新任务还没有默认模型。可以在这里选一个，也可以创建任务时在输入框里选。',
  'settings.model.retired': '{model}（已不可用）',
  'settings.model.effective': '新任务将使用 {model}。',
  'settings.model.noModels': '暂时没有可用模型。在有可用模型之前，新任务无法开始。',
  'settings.model.loading': '正在加载默认模型…',
  'settings.model.error': '无法加载默认模型。',
  'settings.model.saveError': '保存失败，之前的设置未改变。',
  'settings.model.retry': '重试',
  'composer.chooseModel': '先选择模型',
  'composer.chooseModelPlaceholder': '选择模型',
  'picker.search': '搜索…',
  'picker.noResults': '没有匹配的选项',
  'composer.model': '模型',
  'composer.reasoning': '推理强度',
  'composer.reasoningForTurn': '本轮推理强度',
  'effort.auto': '自动',
  'effort.autoOnly': '仅自动',
  'effort.unknown':
    '此模型的服务商没有声明支持哪些推理强度，因此按服务商默认值运行。服务商声明后，可在设置中刷新推理强度。',
  'effort.unsupported': '此模型的服务商没有声明此 Agent 可发送的推理强度，因此按服务商默认值运行。',
  'effort.dropped': '此模型不提供 {level}，推理强度已改回自动。',
} as const;
