from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from misaka_claude_provider.native import NativeClaudeClient, NativeClaudeOptions, NativeClaudeSdk


class ClaudeAgentSdk(NativeClaudeSdk):
    """Small adapter that keeps the third-party SDK outside provider logic."""

    def create_client(self, options: NativeClaudeOptions) -> NativeClaudeClient:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        sdk_options = ClaudeAgentOptions(
            model=options.model,
            effort=cast(Any, options.effort),
            cwd=options.cwd,
            resume=options.resume,
            session_id=options.session_id,
            cli_path=options.cli_path,
            env=dict(options.env),
            tools=list(options.tools),
            allowed_tools=[],
            disallowed_tools=[],
            permission_mode="default",
            setting_sources=[],
            strict_mcp_config=True,
            output_format=options.output_format,
            include_partial_messages=True,
            forward_subagent_text=True,
            can_use_tool=self._permission_callback(options),
        )
        return cast(NativeClaudeClient, ClaudeSDKClient(sdk_options))

    @staticmethod
    def _permission_callback(options: NativeClaudeOptions) -> Any:
        async def can_use_tool(
            tool_name: str,
            tool_input: dict[str, Any],
            _context: object,
        ) -> object:
            from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

            if options.tool_policy is None:
                return PermissionResultDeny(
                    behavior="deny",
                    message=f"tool is not permitted by the V3 policy: {tool_name}",
                )
            allowed = await options.tool_policy(tool_name, cast(Mapping[str, object], tool_input))
            if allowed:
                return PermissionResultAllow()
            return PermissionResultDeny(
                behavior="deny",
                message=f"tool is not permitted by the V3 policy: {tool_name}",
            )

        return can_use_tool
