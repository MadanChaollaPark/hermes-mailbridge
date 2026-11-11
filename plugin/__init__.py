"""Native Hermes entry point; registration never touches mail or credentials."""
from .hermes_tools import register

__all__ = ["register"]
