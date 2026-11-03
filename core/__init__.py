"""Universal core of Shoal: the hub that runs on every device of a tailnet.

A single runtime (Python 3.9+, no dependencies) for any machine: Linux / Raspberry Pi,
macOS, Android (Termux) and Windows. On startup it detects the platform and probes what the
machine can do (adapters in core/adapters); the hub declares those capabilities in api/caps and the
standard interface (site/) shows only what applies.
"""

# The release of Shoal itself (a git tag v<this>, and pyproject.toml). Not the same as a mesh's
# release sequence: every mesh counts its own signed releases from 1.
__version__ = "0.1.0-alpha"
