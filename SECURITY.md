# Security

## Reporting a vulnerability

Report vulnerabilities through GitHub's private vulnerability reporting for
this repository. Select "Report a vulnerability" under the Security tab. Do
not open a public issue for anything you believe is exploitable.

Expect an acknowledgement within a few days. We will tell you what we found,
what we changed, and when the change shipped.

## What is in scope

- The control plane, web app, runner host, and sandbox image in this repository.
- The account system, its sessions and its waiting list.
- The committed API contract under `api_contracts/`.

A model provider that owners connect is not part of this service; report a
provider's issues to that provider.

## What a deployment must not assume

`AGENTS.md` and `docs/developer/operations.md` describe the threat model for this service.
Two deployment requirements bear repeating:

- The plain Docker runner profile isolates trusted repositories from each
  other. It does not isolate hostile code from the host. Do not offer it to
  untrusted users.
- The sandbox never holds a platform or administration credential. It holds
  the owner's own provider key for the turn it runs, and short-lived scoped
  tokens. Putting an admin token into a sandbox removes the security property
  that this design rests on.
