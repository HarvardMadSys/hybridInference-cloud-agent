"""Scan the exact Git snapshot file set, including untracked and binary additions."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from cloud_agent_host.sandbox import SandboxError
from cloud_agent_shared.secret_redaction import secret_patterns

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import RetainedChatBackend
    from cloud_agent_shared.environment import BoundEnvironment

SCAN_SCRIPT = r"""import base64,json,os,pathlib,subprocess,sys,tempfile
root=pathlib.Path(sys.argv[1])
patterns=[base64.b64decode(v) for k,v in os.environ.items() if k.startswith("AGENT_SECRET_PATTERN_")]
with tempfile.TemporaryDirectory(prefix='agent-secret-scan-') as temporary:
 env=dict(os.environ,GIT_INDEX_FILE=temporary+'/index')
 subprocess.run(['git','read-tree',sys.argv[2]],cwd=root,env=env,check=True)
 subprocess.run(['git','add','-A','-N'],cwd=root,env=env,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 paths=subprocess.check_output(['git','diff','--name-only','-z','--diff-filter=ACMRT',sys.argv[2]],cwd=root,env=env).split(b'\0')
for raw in paths:
 if not raw: continue
 path=root / raw.decode('utf-8')
 data=os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
 old=subprocess.run(['git','show',sys.argv[2]+':'+raw.decode('utf-8')],cwd=root,capture_output=True).stdout
 if any(data.count(p)>old.count(p) for p in patterns): sys.exit(3)
"""


def check_snapshot_secrets(
    backend: RetainedChatBackend,
    base_sha: str,
    environment: BoundEnvironment,
    *,
    workdir: str = "/workspace",
) -> None:
    """Reject new known credential bytes without sending file contents outside the sandbox."""
    patterns = {
        f"AGENT_SECRET_PATTERN_{i}": base64.b64encode(value.encode()).decode()
        for i, value in enumerate(
            pattern
            for value in environment.secrets().values()
            for pattern in secret_patterns(value)
        )
    }
    if not patterns:
        return
    check = backend.manager.exec_capture(
        backend.spec,
        ["python3", "-c", SCAN_SCRIPT, workdir, base_sha],
        env=patterns,
        workdir=workdir,
        timeout_s=60,
    )
    if check.returncode:
        raise SandboxError("Snapshot contains a Secret value or could not be safely checked")
