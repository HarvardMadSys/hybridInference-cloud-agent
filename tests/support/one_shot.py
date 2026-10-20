"""One-shot invocations of the pinned CLIs, for the external image tests.

The platform launches only resident processes, which take their prompts over
stdin, RPC or HTTP. A few external tests check a CLI itself with one prompt and
an exit. These turn a resident launch from ``prepare_live`` into that CLI's own
headless form, keeping every flag the resident launch carries that the headless
form has too, so the property under test is the production one.
"""

from __future__ import annotations


def one_shot_argv(
    runtime: str,
    live_argv: list[str],
    *,
    prompt: str,
    model: str,
    provides_isolation: bool = True,
    resume_session_id: str | None = None,
) -> list[str]:
    """The headless invocation of ``runtime`` matching its resident ``live_argv``."""
    if runtime == "claude-code":
        argv = list(live_argv)
        input_at = argv.index("--input-format")
        del argv[input_at : input_at + 2]
        argv.remove("--replay-user-messages")
        tool_at = argv.index("--permission-prompt-tool")
        del argv[tool_at : tool_at + 2]
        argv.insert(argv.index("-p") + 1, prompt)
        return argv
    if runtime == "codex":
        resume = ["resume", resume_session_id] if resume_session_id else []
        return [
            live_argv[0],
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--sandbox",
            "danger-full-access" if provides_isolation else "workspace-write",
            "--model",
            model,
            # Every -c override of the resident launch, without ``app-server``.
            *live_argv[1:-1],
            *resume,
            prompt,
        ]
    if runtime == "pi":
        argv = list(live_argv)
        argv[argv.index("--mode") + 1] = "json"
        prompt_at = argv.index("--thinking") if "--thinking" in argv else len(argv)
        argv[prompt_at:prompt_at] = ["-p", prompt]
        return argv
    if runtime in {"opencode", "kilo"}:
        session = ["--session", resume_session_id] if resume_session_id else []
        return [
            f"{runtime}-freeinference",
            "run",
            "--format",
            "json",
            "--auto",
            "-m",
            f"freeinference/{model}",
            *session,
            prompt,
        ]
    raise KeyError(runtime)


def one_shot_env(env: dict[str, str]) -> dict[str, str]:
    """A resident launch's environment without the HTTP bridge's own variables."""
    return {name: value for name, value in env.items() if not name.startswith("AGENT_LIVE_")}
