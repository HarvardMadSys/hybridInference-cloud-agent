# Site content

The control plane mounts `AGENT_SITE_CONTENT_DIR` read-only at
`/etc/cloud-agent/site`. This directory is the default, and it configures
nothing: without `site.yaml` the footer hides the Team and source links, and
without `terms.<locale>.txt` the Terms of Service come from Admin settings.

A deployment keeps its own directory in its repository and points
`AGENT_SITE_CONTENT_DIR` at a copy on the control-plane host. The file formats
are in the configuration guide (`ENVIRONMENT.md`, "Site content files").
