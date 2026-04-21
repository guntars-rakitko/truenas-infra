"""Phase modules.

Each phase implements a single function:

    def run(cli, ctx, only=None) -> int | None: ...

`cli` is the authenticated `truenas_api_client.Client` (shared WebSocket).
`ctx` is `truenas_infra.cli.Context` (config, apply flag, logger).
`only` optionally limits the phase to a named sub-item.

Return `None` / `0` on success; non-zero on failure (will surface as the
CLI exit code).
"""
