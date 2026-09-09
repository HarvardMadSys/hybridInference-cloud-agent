# Security

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository
("Report a vulnerability" under the Security tab). Please do not open a public
issue for anything you believe is exploitable.

You can expect an acknowledgement within a few days. We will tell you what we
found, what we changed, and when it shipped.

## What is in scope

- The control plane, the web app, the runner host and the sandbox image in
  this repository.
- The HTTP contracts under `contracts/` as this service consumes them.

The gateway this service authenticates against is a separate project
(HybridInference); report gateway issues there.

## What a deployment must not assume

The threat model this service is built to is described in `AGENTS.md` and
`docs/operations.md`. Two points bear repeating for anyone deploying it:

- The plain Docker runner profile isolates trusted repositories from each
  other, not hostile code from the host. Do not offer it to untrusted users.
- The sandbox never holds a long-lived gateway credential; it receives a
  short-lived, job-scoped grant. A deployment that puts an admin token into
  a sandbox has removed the property this design rests on.
