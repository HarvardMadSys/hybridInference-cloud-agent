"""Compatibility entry point for deployments using the former service name."""

from cloud_agent_host.sandbox_manager import create_app as create_app, main as main

if __name__ == "__main__":
    main()
