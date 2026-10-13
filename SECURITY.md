# Security

## Reporting a vulnerability

Report vulnerabilities through GitHub's private vulnerability reporting for
this repository. Select "Report a vulnerability" under the Security tab. Do
not open a public issue for anything you believe is exploitable.

Expect an acknowledgement within a few days. We will tell you what we found,
what we changed, and when the change shipped.

## What is in scope

- The control plane, web app, runner host, and sandbox image in this repository.
- The HTTP contracts under `gateway_contracts/`, as consumed by this service.

The gateway that this service authenticates against is a separate project:
HybridInference. Report gateway issues there.

## What a deployment must not assume

`AGENTS.md` and `docs/operations.md` describe the threat model for this service.
Two deployment requirements bear repeating:

- The plain Docker runner profile isolates trusted repositories from each
  other. It does not isolate hostile code from the host. Do not offer it to
  untrusted users.
- The sandbox never holds a long-lived gateway credential. It receives a
  short-lived, job-scoped grant. Putting an admin token into a sandbox removes
  the security property that this design rests on.
