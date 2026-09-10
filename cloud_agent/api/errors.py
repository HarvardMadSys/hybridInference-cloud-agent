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
