# Local agent demo

This is the `local/demo` repository used by Cloud Agent's local development
stack. It contains a small Python greeting function that agents can inspect
and edit during development.

`deploy/local/Dockerfile.repo` copies this directory into a Git repository on
`main` and serves a read-only bare copy at `git://local-repo/local/demo.git`.
The trusted runner clones it through the private control-plane network. The
Git server has no published host port and does not accept pushes.

The fixture lets the runner clone source without a GitHub account, token, or
host workspace mount. To change the source offered to new clones, edit this
directory and rebuild the local stack with `./deploy/local/dev.sh up` from the
Cloud Agent repository root. Existing retained workspaces keep their own state.
