# A disposable compute node for tests: its own Docker daemon, and the tools
# the control plane's node scripts expect (python3, sudo, ssh, rsync).
FROM docker:29-dind
RUN apk add --no-cache python3 sudo openssh-server openssh-client rsync bash
