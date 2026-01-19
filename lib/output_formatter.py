"""
Output formatting for Pinglet task output.

Provides configurable output parsing for rich notification summaries,
supporting JSON extraction and template formatting.
"""
import json
from dataclasses import dataclass
from typing import Optional


@dataclass
class OutputConfig:
    """Configuration for task output formatting."""
    format: str = "text"  # "text" or "json"
    summary_template: Optional[str] = None  # Template string for JSON format

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "OutputConfig":
        """
        Create OutputConfig from a dictionary.

        Args:
            data: Dictionary with output config, or None for defaults

        Returns:
            OutputConfig instance
        """
        if not data:
            return cls()

        return cls(
            format=data.get("format", "text"),
            summary_template=data.get("summary_template"),
        )


def format_task_output(
    stdout: str,
    stderr: str,
    config: OutputConfig,
    is_error: bool = False
) -> str:
    """
    Format task output according to configuration.

    Args:
        stdout: Standard output from task
        stderr: Standard error from task
        config: Output formatting configuration
        is_error: Whether this is error output (prioritizes stderr)

    Returns:
        Formatted output string
    """
    if is_error:
        return _format_error_output(stdout, stderr, config)

    if config.format == "json":
        return _format_json_output(stdout, config)

    return _format_text_output(stdout)


def _format_text_output(stdout: str) -> str:
    """
    Format output as text (default formatter).

    Shows last 5 lines, truncated to 200 characters.

    Args:
        stdout: Standard output from task

    Returns:
        Formatted text output
    """
    if not stdout or not stdout.strip():
        return ""

    # Get last 5 lines
    lines = stdout.strip().split("\n")
    last_lines = lines[-5:]
    result = "\n".join(last_lines)

    # Truncate to 200 characters
    if len(result) > 200:
        result = result[:200] + "..."

    return result


def _format_json_output(stdout: str, config: OutputConfig) -> str:
    """
    Format JSON output using template string substitution.

    If JSON parsing fails, falls back to text format.

    Args:
        stdout: Standard output (expected to be JSON)
        config: Output configuration with template

    Returns:
        Formatted output string
    """
    try:
        data = json.loads(stdout)

        if config.summary_template:
            try:
                return config.summary_template.format(**data)
            except KeyError:
                # Missing template key - fall back to showing what we have
                # Try to show available keys
                available = {k: v for k, v in data.items() if not isinstance(v, dict)}
                result = json.dumps(available, indent=2)
                return result[:200] if len(result) > 200 else result

        # No template - show key fields truncated
        result = json.dumps(data, indent=2)
        return result[:200] if len(result) > 200 else result

    except json.JSONDecodeError:
        # Fall back to text format
        return _format_text_output(stdout)


def _format_error_output(stdout: str, stderr: str, config: OutputConfig) -> str:
    """
    Format error output with priority: stderr > stdout > JSON error key.

    Args:
        stdout: Standard output from task
        stderr: Standard error from task
        config: Output configuration

    Returns:
        Formatted error output (max 200 chars)
    """
    result = ""

    # Priority 1: stderr
    if stderr and stderr.strip():
        result = stderr.strip()[:200]
    # Priority 2: stdout
    elif stdout and stdout.strip():
        result = stdout.strip()[:200]

    # Check for JSON error key to append
    if config.format == "json" and stdout:
        try:
            data = json.loads(stdout)
            if "error" in data and data["error"]:
                json_error = str(data["error"])
                if result and json_error not in result:
                    # Append JSON error if different from stderr
                    combined = f"{result}\n{json_error}"
                    result = combined[:200]
                elif not result:
                    result = json_error[:200]
        except json.JSONDecodeError:
            pass

    # Truncate if needed
    if len(result) > 200:
        result = result[:200] + "..."

    return result
