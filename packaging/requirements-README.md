# EMRG Phase 4 — requirements lock (rant #12 §3 R100).
#
# Platform-specific lock files because C-extension wheels are platform
# dependent (pyyaml/markupsafe/websockets). Generated with:
#
#   uv pip compile pyproject.toml --python-version 3.13 --platform <p> \
#       -o requirements-<platform>.lock
#
# The runtime build (build-runtime.sh) uses pip --target directly; these
# locks pin the exact versions for reproducibility across CI matrix hosts.
#
# Placeholders — regenerate on first CI run per platform:
#   requirements-macos-arm64.lock
#   requirements-linux-x86_64.lock
#   requirements-linux-aarch64.lock
#   requirements-windows-x64.lock
