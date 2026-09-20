"""The control plane's HTTP surface: the job API and the admin host pool.

Route paths are unchanged from before the split (``/v1/agent/...``,
``/admin/agent/...``) — the web app and the runner are both existing clients,
and a migration that also renamed their endpoints would be two changes at once.
"""
