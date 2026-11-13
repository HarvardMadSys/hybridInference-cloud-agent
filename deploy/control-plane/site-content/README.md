# Site content

The control plane mounts `AGENT_SITE_CONTENT_DIR` read-only at
`/etc/cloud-agent/site`. This directory is the default, and it configures
nothing: without `site.yaml` the footer hides the Team and source links, and
without `terms.<locale>.txt` the Terms of Service come from Admin settings.

A deployment keeps its own directory in its repository and points
`AGENT_SITE_CONTENT_DIR` at a copy on the control-plane host. The file formats
are in the configuration guide (`ENVIRONMENT.md`, "Site content files").

Members may also declare `photo_url`, an absolute HTTPS URL for their portrait:

```yaml
members:
  - name: Ada Lovelace
    photo_url: https://example.org/team/ada.jpg
```

The browser loads the photo directly without sending a referrer. If a photo is
absent or cannot load, the card shows the member's initials. Keep real member
names and photo URLs in the deployment's own `site.yaml`, not in this directory.

For a photo whose origin disallows embedding, `photo_url` also accepts an inline
`data:image/jpeg;base64,...` URL (PNG and WebP are also supported). Encode the
original file's bytes; the 256 KiB limit still applies to the whole `site.yaml`.
This keeps deployment-owned photos with the deployment's configuration without
adding an image proxy or relaxing another site's embedding policy.
