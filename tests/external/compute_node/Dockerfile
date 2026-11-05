# A disposable compute node for tests, shaped like a CloudLab node: Ubuntu
# with systemd as PID 1, Docker from the same packages the deploy installs,
# and sshd for the control plane's management user.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends systemd systemd-sysv dbus openssh-server sudo \
      python3 rsync docker.io docker-compose-v2 iproute2 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && systemctl enable docker containerd ssh \
 && useradd -m -s /bin/bash agent \
 && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent \
 && chmod 0440 /etc/sudoers.d/agent \
 && passwd -d agent
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
