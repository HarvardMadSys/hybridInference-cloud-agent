// The Terms of Service reader: the standalone page, and the dialog sign-up
// opens so the Terms are read and agreed to without leaving the form.
export const termsReaderEn = {
  'terms.eyebrow': 'Legal',
  'terms.loadFailed': 'The Terms of Service could not be loaded.',
  'terms.shownIn': 'Not published in your language. Shown in {language}.',
  'terms.textLabel': 'Terms of Service text',
  'terms.externalIntro': 'This deployment publishes its Terms of Service on another site.',
  'terms.externalOpen': 'Open the Terms of Service',
  'terms.externalNote':
    'It opens in a new tab and this page stays as it is. This page cannot show that document or tell whether you have read it: agree only once you have.',
  'terms.alsoPublished': 'Also published on another site',
  'terms.noneConsent': 'Creating an account still requires agreeing to them.',
  'terms.scrollHint': 'Read to the end of the Terms to agree.',
  'terms.reachedEnd': 'You have reached the end of the Terms.',
  'terms.agreeButton': 'Agree',
  'terms.disagreeButton': 'Disagree',
  'terms.close': 'Close',
  'terms.statusPending':
    'Read the Terms of Service and agree to them before you create your account.',
  'terms.statusAgreed': 'You agreed to the Terms of Service.',
  'terms.statusDeclined': 'You did not agree. An account cannot be created without agreeing.',
  'terms.statusChanged':
    'The Terms of Service changed after you agreed. Read them again to continue.',
  'terms.read': 'Read and agree',
  'terms.readAgain': 'Read again',
  'signup.termsRequired': 'Agree to the Terms of Service before you create your account.',
  'signup.captchaRequired': 'Complete the check before you create your account.',
  'signup.codeRequired': 'Enter the six-digit code sent to your phone.',
  'signup.optionsFailed': 'Sign-up could not be loaded.',
} as const;

export const termsReaderZh: Record<keyof typeof termsReaderEn, string> = {
  'terms.eyebrow': '法律条款',
  'terms.loadFailed': '服务条款加载失败。',
  'terms.shownIn': '未提供你所用语言的版本，以下为{language}版本。',
  'terms.textLabel': '服务条款正文',
  'terms.externalIntro': '本部署的服务条款发布在其他网站上。',
  'terms.externalOpen': '打开服务条款',
  'terms.externalNote':
    '条款会在新标签页中打开，本页保持不变。本页无法显示该文档，也无法判断你是否读过，请读完后再同意。',
  'terms.alsoPublished': '在其他网站上查看发布版本',
  'terms.noneConsent': '创建账号仍需同意服务条款。',
  'terms.scrollHint': '请阅读至条款末尾，然后才能同意。',
  'terms.reachedEnd': '你已阅读至条款末尾。',
  'terms.agreeButton': '同意',
  'terms.disagreeButton': '不同意',
  'terms.close': '关闭',
  'terms.statusPending': '创建账号前，请阅读并同意服务条款。',
  'terms.statusAgreed': '你已同意服务条款。',
  'terms.statusDeclined': '你没有同意服务条款。不同意则无法创建账号。',
  'terms.statusChanged': '你同意之后，服务条款有了变更。请重新阅读后继续。',
  'terms.read': '阅读并同意',
  'terms.readAgain': '再次阅读',
  'signup.termsRequired': '请先同意服务条款，再创建账号。',
  'signup.captchaRequired': '请先完成人机验证，再创建账号。',
  'signup.codeRequired': '请输入发送到手机的 6 位验证码。',
  'signup.optionsFailed': '注册页面加载失败。',
};
