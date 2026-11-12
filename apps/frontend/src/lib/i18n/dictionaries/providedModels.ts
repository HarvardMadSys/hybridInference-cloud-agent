// Models a deployment provides: the read-only card an owner sees in
// Settings → Models (`provided.*`), and the Admin → Settings → Models section
// where an administrator sets them up (`adminModels.*`). Nothing here names a
// provider or a deployment: the connection's own name is the only name shown.

export const providedModelsEn = {
  'provided.title': 'Provided by this deployment',
  'provided.description':
    'Models this deployment offers you with no setup. Each has a daily request allowance, counted per model; an agent sends many requests in one turn. Nothing here can be changed.',
  'provided.default': 'New tasks start here',
  'provided.model': 'Model',
  'provided.allowance': 'Daily allowance',
  'provided.used': 'Used today',
  'provided.left': 'Left today',
  'provided.unlimited': 'Unlimited',
  'provided.noneLeft': 'None until {time}',
  'provided.resets': 'Counts are per UTC day and start over at {time}, your time.',
  'provided.noModels': 'No model on this connection is offered to your account.',
  'adminModels.tab': 'Models',
  'adminModels.title': 'Models this deployment provides',
  'adminModels.description':
    'Connections every admitted account can run tasks on with no setup of its own. The API key stays on the control plane and never enters a sandbox. Each account may send a number of requests per model per UTC day; set that allowance for every model, then enable the connection.',
  'adminModels.featureOff':
    'Model connections are switched off on this deployment. Nothing here is offered to anyone, and a connection cannot be added, verified or enabled until they are switched on.',
  'adminModels.noRelayHosts':
    'No server can run provided models yet. A turn on one waits until a server that reaches the model relay is enrolled.',
  'adminModels.add': 'Add a provided connection',
  'adminModels.empty': 'No provided connections yet.',
  'adminModels.search.label': 'Provided models',
  'adminModels.search.description':
    'Connections this deployment offers to every account, with a daily request allowance per model.',
  'adminModels.error.nameTaken': 'Another provided connection already has this name.',
  'adminModels.error.notFound':
    'This connection no longer exists. Reload the page to see the current list.',
  'adminModels.error.limitsMissing':
    'Not enabled: every model needs a default allowance first. Set one for each model, then enable it.',
  'adminModels.error.accountNotFound': 'No account has that email address or id.',
  'adminModels.error.featureOff':
    'Model connections are switched off on this deployment. Switch them on before adding, verifying or enabling a provided connection.',
  'adminModels.error.generic': 'The request failed. Try again.',
  'adminModels.invalid.name': 'Enter a name of up to 64 characters.',
  'adminModels.invalid.defaultModel': 'Choose one of the listed models, or none.',
  'adminModels.invalid.header':
    'Enter a header name of up to 64 characters, such as X-End-User, without spaces or a colon.',
  'adminModels.invalid.userConcurrency': 'Enter a whole number from 1 to 64.',
  'adminModels.form.addTitle': 'New provided connection',
  'adminModels.form.editTitle': 'Edit provided connection',
  'adminModels.form.name': 'Name',
  'adminModels.form.nameHint': 'Shown to every account in the model picker.',
  'adminModels.form.keyHint':
    'Write-only: it is encrypted, never shown again and never sent to a sandbox. Use a key dedicated to this deployment, with a budget and model list set at the provider.',
  'adminModels.form.defaultModel': 'Default model',
  'adminModels.form.defaultModelHint':
    'The model an account with no history starts on, when it may use it.',
  'adminModels.form.noDefaultModel': 'None',
  'adminModels.form.userConcurrency': 'Requests in flight per account',
  'adminModels.form.userConcurrencyHint':
    'How many requests one account may have running at once, from 1 to {max}.',
  'adminModels.form.header': 'End-user header (optional)',
  'adminModels.form.headerHint':
    'When set, each relayed request carries this header with an opaque tag for the account, so the provider can attribute or limit use per account. The tag is never an email address or an account id.',
  'adminModels.form.isDefault': 'Default for new users',
  'adminModels.form.isDefaultHint':
    'An account with no history starts on this connection. Only one connection can be the default.',
  'adminModels.form.verifying':
    'Verifying every model with the provider… this can take up to four minutes. Keep this page open.',
  'adminModels.form.createNote':
    'Saving verifies every model with a few small requests the provider may bill. The connection is saved disabled: set each model’s default allowance, then enable it.',
  'adminModels.form.verifyNote':
    'A new key, a new Base URL or an added model is verified with the provider before anything is saved, with a few small requests it may bill.',
  'adminModels.form.defaultFailed':
    'The connection was saved, but it could not be made the default for new users: {reason}',
  'adminModels.limits.title': 'Default allowances',
  'adminModels.limits.description':
    'What every account gets for each model unless it has an allowance of its own. An allowance counts requests the relay accepted per UTC day, not tokens or cost.',
  'adminModels.limits.allowance': 'Daily allowance',
  'adminModels.limits.choose': 'Choose…',
  'adminModels.limits.limited': 'A number of requests',
  'adminModels.limits.unlimited': 'Unlimited',
  'adminModels.limits.named': 'Only named accounts (0)',
  'adminModels.limits.requestsPerDay': 'Requests per day',
  'adminModels.limits.unset': 'No default allowance yet.',
  'adminModels.limits.savedUnlimited': 'Unlimited requests per account per day.',
  'adminModels.limits.savedNamed': 'Offered only to accounts given their own allowance.',
  'adminModels.limits.savedLimited': '{count} requests per account per day.',
  'adminModels.limits.savedWithCap': '{allowance} At most {count} in flight across the deployment.',
  'adminModels.limits.globalConcurrency': 'Deployment-wide requests in flight (optional)',
  'adminModels.limits.noCap': 'No cap',
  'adminModels.limits.saveFor': 'Save the allowance for {model}',
  'adminModels.limits.invalid':
    'Enter whole numbers: 1 to {requests} requests per day, and 1 to {concurrency} in flight or blank for no cap.',
  'adminModels.accounts.title': 'Accounts',
  'adminModels.accounts.description':
    'Accounts with an allowance of their own, or with use today. Everyone else follows the defaults.',
  'adminModels.accounts.show': 'Show accounts and their allowances',
  'adminModels.accounts.hide': 'Hide accounts',
  'adminModels.accounts.empty': 'No account has its own allowance or has used a model today.',
  'adminModels.accounts.account': 'Account',
  'adminModels.accounts.accountPlaceholder': 'Email address or account id',
  'adminModels.accounts.model': 'Model',
  'adminModels.accounts.ownAllowance': 'Own daily allowance',
  'adminModels.accounts.usedToday': 'Used today',
  'adminModels.accounts.actions': 'Actions',
  'adminModels.accounts.followsDefault': 'Follows the default',
  'adminModels.accounts.withheld': 'Withheld (0)',
  'adminModels.accounts.withhold': 'Withhold the model (0)',
  'adminModels.accounts.setTitle': 'Set an allowance for an account',
  'adminModels.accounts.set': 'Set allowance',
  'adminModels.accounts.clear': 'Clear',
  'adminModels.accounts.clearFor': 'Put {account} back on the default for {model}',
  'adminModels.accounts.loading': 'Loading accounts…',
  'adminModels.accounts.more': 'Load more accounts',
  'adminModels.usage.title': 'Requests',
  'adminModels.usage.description':
    'Requests this deployment’s relay accepted, per UTC day, account and model. These are request counts, not tokens and not cost: one request can be small or large, and what it costs is the provider’s to say.',
  'adminModels.usage.showSection': 'Show requests by day',
  'adminModels.usage.hide': 'Hide requests',
  'adminModels.usage.from': 'From (UTC day)',
  'adminModels.usage.to': 'To (UTC day)',
  'adminModels.usage.show': 'Show requests',
  'adminModels.usage.loading': 'Loading…',
  'adminModels.usage.invalid': 'Choose a first day that is not after the last day.',
  'adminModels.usage.empty': 'No requests were counted in these days.',
  'adminModels.usage.day': 'Day (UTC)',
  'adminModels.usage.requests': 'Requests',
  'adminModels.usage.truncated':
    'More rows matched than are shown. Choose a shorter range to see the rest.',
  'adminModels.card.isDefault': 'Default for new users',
  'adminModels.card.defaultModel': 'Default model: {model}',
  'adminModels.card.userConcurrency': 'Up to {count} requests in flight per account',
  'adminModels.card.header': 'End-user header: {header}',
  'adminModels.card.enableBlocked':
    'It cannot be enabled until every model has a default allowance. Still without one: {models}.',
  'adminModels.card.enableFeatureOff':
    'It cannot be enabled while model connections are switched off on this deployment.',
  'adminModels.card.keyRefused':
    'The provider has refused the stored API key since {since}. Requests on this connection fail until a working key is saved.',
  'adminModels.card.confirmDelete':
    'Delete {name}? Agent processes running on it stop for every account, and chats that used it will need another model. This does not revoke the key at the provider.',
  'adminModels.card.confirmDisable':
    'Disable {name}? Agent processes running on it stop for every account, and no turn can start on it until it is enabled again.',
} as const;

export const providedModelsZhCN: Record<keyof typeof providedModelsEn, string> = {
  'provided.title': '由本部署提供',
  'provided.description':
    '本部署提供给你的模型，无需任何设置。每个模型有各自的每日请求次数上限；Agent 的一轮对话通常会发出很多次请求。这里的内容不可修改。',
  'provided.default': '新任务默认使用',
  'provided.model': '模型',
  'provided.allowance': '每日上限',
  'provided.used': '今日已用',
  'provided.left': '今日剩余',
  'provided.unlimited': '不限',
  'provided.noneLeft': '已用完，{time} 重置',
  'provided.resets': '次数按 UTC 日统计，将在你当地时间 {time} 重置。',
  'provided.noModels': '此连接没有向你的账号开放的模型。',
  'adminModels.tab': '模型',
  'adminModels.title': '本部署提供的模型',
  'adminModels.description':
    '每个已准入的账号无需自行设置就能用来运行任务的连接。API key 只留在控制面，不会进入沙箱。每个账号在每个模型上每个 UTC 日可以发出一定次数的请求；为每个模型设好上限后，再启用连接。',
  'adminModels.featureOff':
    '此部署已关闭模型连接功能。这里的内容不会提供给任何人；重新开启之前，无法添加、验证或启用连接。',
  'adminModels.noRelayHosts':
    '目前没有服务器可以运行本部署提供的模型。在有能连到模型中继的服务器加入之前，使用这些模型的对话轮次会一直等待。',
  'adminModels.add': '添加提供的连接',
  'adminModels.empty': '还没有提供的连接。',
  'adminModels.search.label': '提供的模型',
  'adminModels.search.description': '本部署向所有账号提供的连接，每个模型有每日请求次数上限。',
  'adminModels.error.nameTaken': '已有同名的提供的连接。',
  'adminModels.error.notFound': '此连接已不存在。请重新加载页面查看当前列表。',
  'adminModels.error.limitsMissing': '未启用：每个模型都要先设置默认上限。为每个模型设置后再启用。',
  'adminModels.error.accountNotFound': '没有使用该邮箱或 ID 的账号。',
  'adminModels.error.featureOff':
    '此部署已关闭模型连接功能。请先开启，再添加、验证或启用提供的连接。',
  'adminModels.error.generic': '请求失败，请重试。',
  'adminModels.invalid.name': '请输入名称，最多 64 个字符。',
  'adminModels.invalid.defaultModel': '请选择列表中的一个模型，或不选。',
  'adminModels.invalid.header':
    '请输入请求头名称，最多 64 个字符，例如 X-End-User，不含空格和冒号。',
  'adminModels.invalid.userConcurrency': '请输入 1 到 64 之间的整数。',
  'adminModels.form.addTitle': '新建提供的连接',
  'adminModels.form.editTitle': '编辑提供的连接',
  'adminModels.form.name': '名称',
  'adminModels.form.nameHint': '所有账号都会在模型选择器里看到这个名称。',
  'adminModels.form.keyHint':
    '只写：加密保存，之后不再显示，也不会发送到沙箱。请使用专供此部署的 key，并在服务商处设置预算和可用模型。',
  'adminModels.form.defaultModel': '默认模型',
  'adminModels.form.defaultModelHint': '没有历史记录的账号在可以使用它时，默认从这个模型开始。',
  'adminModels.form.noDefaultModel': '无',
  'adminModels.form.userConcurrency': '每个账号进行中的请求数',
  'adminModels.form.userConcurrencyHint': '一个账号同时可以有多少个请求在进行，1 到 {max}。',
  'adminModels.form.header': '用户标签请求头（可选）',
  'adminModels.form.headerHint':
    '设置后，每个中继请求都会带上这个请求头，值是代表该账号的不透明标签，服务商可以据此按账号归因或限额。标签不包含邮箱和账号 ID。',
  'adminModels.form.isDefault': '新用户的默认连接',
  'adminModels.form.isDefaultHint':
    '没有历史记录的账号从这个连接开始。整个部署只能有一个默认连接。',
  'adminModels.form.verifying': '正在向服务商验证每个模型……最长可能需要四分钟，请不要关闭此页面。',
  'adminModels.form.createNote':
    '保存时会为每个模型向服务商发送几个小请求进行验证，服务商可能对此计费。连接保存后处于停用状态：为每个模型设置默认上限后再启用。',
  'adminModels.form.verifyNote':
    '更换 key、更换 Base URL 或增加模型时，会先向服务商发送几个小请求进行验证，然后才保存，服务商可能对此计费。',
  'adminModels.form.defaultFailed': '连接已保存，但未能设为新用户的默认连接：{reason}',
  'adminModels.limits.title': '默认上限',
  'adminModels.limits.description':
    '账号没有单独设置时，在每个模型上使用的上限。上限统计的是中继每个 UTC 日接受的请求次数，不是 token，也不是费用。',
  'adminModels.limits.allowance': '每日上限',
  'adminModels.limits.choose': '请选择…',
  'adminModels.limits.limited': '指定请求次数',
  'adminModels.limits.unlimited': '不限',
  'adminModels.limits.named': '仅限指定账号（0）',
  'adminModels.limits.requestsPerDay': '每日请求次数',
  'adminModels.limits.unset': '还没有设置默认上限。',
  'adminModels.limits.savedUnlimited': '每个账号每天请求次数不限。',
  'adminModels.limits.savedNamed': '只提供给单独设置了上限的账号。',
  'adminModels.limits.savedLimited': '每个账号每天 {count} 次请求。',
  'adminModels.limits.savedWithCap': '{allowance}全部署同时最多 {count} 个请求在进行。',
  'adminModels.limits.globalConcurrency': '全部署进行中的请求数（可选）',
  'adminModels.limits.noCap': '不限',
  'adminModels.limits.saveFor': '保存 {model} 的上限',
  'adminModels.limits.invalid':
    '请输入整数：每日请求次数为 1 到 {requests}；进行中的请求数为 1 到 {concurrency}，留空表示不限。',
  'adminModels.accounts.title': '账号',
  'adminModels.accounts.description': '单独设置了上限或今天有用量的账号。其他账号使用默认上限。',
  'adminModels.accounts.show': '显示账号及其上限',
  'adminModels.accounts.hide': '收起账号',
  'adminModels.accounts.empty': '没有账号单独设置上限，今天也没有账号使用过模型。',
  'adminModels.accounts.account': '账号',
  'adminModels.accounts.accountPlaceholder': '邮箱或账号 ID',
  'adminModels.accounts.model': '模型',
  'adminModels.accounts.ownAllowance': '单独的每日上限',
  'adminModels.accounts.usedToday': '今日已用',
  'adminModels.accounts.actions': '操作',
  'adminModels.accounts.followsDefault': '使用默认上限',
  'adminModels.accounts.withheld': '不提供（0）',
  'adminModels.accounts.withhold': '不提供此模型（0）',
  'adminModels.accounts.setTitle': '为账号设置上限',
  'adminModels.accounts.set': '设置上限',
  'adminModels.accounts.clear': '清除',
  'adminModels.accounts.clearFor': '让 {account} 在 {model} 上恢复使用默认上限',
  'adminModels.accounts.loading': '正在加载账号…',
  'adminModels.accounts.more': '加载更多账号',
  'adminModels.usage.title': '请求次数',
  'adminModels.usage.description':
    '本部署的中继接受的请求次数，按 UTC 日、账号和模型统计。这是请求次数，不是 token，也不是费用：一次请求可大可小，费用以服务商为准。',
  'adminModels.usage.showSection': '按天显示请求次数',
  'adminModels.usage.hide': '收起请求次数',
  'adminModels.usage.from': '起始日（UTC）',
  'adminModels.usage.to': '结束日（UTC）',
  'adminModels.usage.show': '显示请求次数',
  'adminModels.usage.loading': '加载中…',
  'adminModels.usage.invalid': '起始日不能晚于结束日。',
  'adminModels.usage.empty': '这些天没有统计到请求。',
  'adminModels.usage.day': '日期（UTC）',
  'adminModels.usage.requests': '请求次数',
  'adminModels.usage.truncated': '匹配的行数超过了显示的数量。请缩短日期范围查看其余部分。',
  'adminModels.card.isDefault': '新用户的默认连接',
  'adminModels.card.defaultModel': '默认模型：{model}',
  'adminModels.card.userConcurrency': '每个账号最多 {count} 个请求同时进行',
  'adminModels.card.header': '用户标签请求头：{header}',
  'adminModels.card.enableBlocked': '每个模型都设置默认上限后才能启用。尚未设置的模型：{models}。',
  'adminModels.card.enableFeatureOff': '此部署已关闭模型连接功能，暂时无法启用。',
  'adminModels.card.keyRefused':
    '服务商自 {since} 起拒绝了已保存的 API key。在保存可用的 key 之前，此连接上的请求都会失败。',
  'adminModels.card.confirmDelete':
    '删除 {name}？所有账号在它上面运行的 Agent 进程都会停止，使用它的会话需要选择其他模型。此操作不会在服务商处撤销密钥。',
  'adminModels.card.confirmDisable':
    '停用 {name}？所有账号在它上面运行的 Agent 进程都会停止，重新启用之前无法在它上面开始新的对话轮次。',
};
