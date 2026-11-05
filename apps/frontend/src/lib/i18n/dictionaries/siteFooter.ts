// The site footer's links and the /team page they lead to. Kept beside the
// landing page's strings, the other public pages, so they are translated
// together. The members themselves come from the deployment's site files.

export const siteFooterEn = {
  'footer.terms': 'Terms',
  'footer.team': 'Team',
  'team.title': 'Team',
  'team.eyebrow': 'Our team',
  'team.intro': 'Building better ways to work with coding agents.',
  'team.description': 'The people behind {app_name}.',
  'team.none': 'This deployment has not published its team.',
  'team.website': 'Website',
  'team.photoAlt': 'Photo of {name}',
};

export const siteFooterZh: Record<keyof typeof siteFooterEn, string> = {
  'footer.terms': '服务条款',
  'footer.team': '团队',
  'team.title': '团队',
  'team.eyebrow': '我们的团队',
  'team.intro': '让编程智能体更好地融入每个人的工作。',
  'team.description': '{app_name} 背后的团队。',
  'team.none': '本部署尚未公布团队信息。',
  'team.website': '个人主页',
  'team.photoAlt': '{name} 的照片',
};
