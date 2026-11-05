# Gemini Bridge contributor instructions

This repository is PUBLIC and MIT licensed. Never include credentials, account caches, environment files, private task logs, machine-specific home paths, or user projects.

Preserve the division of work: the host agent owns architecture, UI/UX, icons/important visuals and final decisions; local Antigravity executes bounded routine tasks. No private API calls, OAuth token extraction, silent model fallback, paid-credit fallback, or blanket permission bypass.

Validate changes with `python3 -m unittest discover -s tests -v` and `python3 scripts/release_check.py` when present. Tests must use fake CLIs and temporary state, never real provider quota or user configuration. Keep Linux and macOS compatibility; Windows support is not implemented.

A successful worker exit is not evidence of product correctness. Review actual results. Keep install instructions truthful about desktop-local execution and separate quotas. Do not claim ChatGPT web/mobile support or guaranteed quota savings.

The persistent MCP dispatcher relies on the bridge rpc-one/serve entry points and unchanged argument forwarding. Preserve these compatibility contracts so already-open transports can select newer installed engines without losing task access declarations.
