"""Public error categories that tell clients when operator action is needed."""

from fastapi import HTTPException


class AgentConfigurationError(HTTPException):
    """The deployment cannot verify access until its configuration is corrected."""

    def __init__(self) -> None:
        """Return a stable category without exposing configuration or credentials."""
        super().__init__(
            status_code=503,
            detail={
                "error": {
                    "type": "agent_configuration_error",
                    "message": "Agent sign-in is not configured correctly. Contact the service administrator.",
                }
            },
        )


def typed_error(status_code: int, error_type: str, message: str, **headers: str) -> HTTPException:
    """An ``HTTPException`` in the ``{"error": {"type", "message"}}`` envelope."""
    return HTTPException(
        status_code=status_code,
        detail={"error": {"type": error_type, "message": message}},
        headers=headers or None,
    )
