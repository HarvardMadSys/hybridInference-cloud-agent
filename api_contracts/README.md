# Cloud Agent's public API contract

[agent-control-plane.openapi.json](agent-control-plane.openapi.json) is the
committed snapshot of the API this service serves. The web client's types and
the contract tests are checked against it, so a route or schema change shows up
in review as a change to this file.

Regenerate it from the repository root after changing a route or schema:

```bash
.venv/bin/python -c "import json,pathlib;from cloud_agent.app import create_app;pathlib.Path('api_contracts/agent-control-plane.openapi.json').write_text(json.dumps(create_app().openapi(), sort_keys=True, indent=2, ensure_ascii=False) + '\n')"
```
