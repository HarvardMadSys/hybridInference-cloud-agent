"""Do not accept an empty native result as a working conversation."""

from types import SimpleNamespace

import pytest

from deploy.local.smoke import require_assistant_reply


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"job_id": "job", "role": "user", "content": "hi"}],
        [{"job_id": "job", "role": "assistant", "content": "  "}],
        [{"job_id": "earlier-job", "role": "assistant", "content": "earlier reply"}],
    ],
)
def test_success_without_this_turns_assistant_reply_is_rejected(messages):
    client = SimpleNamespace(request=lambda _path: {"messages": messages})
    with pytest.raises(RuntimeError, match="without an assistant reply"):
        require_assistant_reply(client, "job")


def test_current_assistant_reply_is_accepted():
    client = SimpleNamespace(
        request=lambda _path: {
            "messages": [{"job_id": "job", "role": "assistant", "content": "confirmed"}],
        }
    )
    require_assistant_reply(client, "job")
