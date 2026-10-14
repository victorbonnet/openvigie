"""OpenTelemetry GenAI semantic-convention attribute names used across openvigie.

Checked against semantic conventions v1.37 (GenAI, status: Development). These names still move
between releases; re-check them when bumping and keep every use going through this module.
Names under ``openvigie.`` are our own, used only where no convention exists yet.
"""

from typing import Final

# Attributes
GEN_AI_PROVIDER_NAME: Final = "gen_ai.provider.name"
GEN_AI_OPERATION_NAME: Final = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL: Final = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL: Final = "gen_ai.response.model"
GEN_AI_RESPONSE_ID: Final = "gen_ai.response.id"
GEN_AI_CONVERSATION_ID: Final = "gen_ai.conversation.id"
GEN_AI_AGENT_NAME: Final = "gen_ai.agent.name"
GEN_AI_AGENT_ID: Final = "gen_ai.agent.id"
GEN_AI_TOOL_NAME: Final = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID: Final = "gen_ai.tool.call.id"
GEN_AI_USAGE_INPUT_TOKENS: Final = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS: Final = "gen_ai.usage.output_tokens"
GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS: Final = "gen_ai.usage.cache_read.input_tokens"
GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS: Final = "gen_ai.usage.cache_creation.input_tokens"
SESSION_ID: Final = "session.id"
ERROR_TYPE: Final = "error.type"

# Content attributes: only set when content export is on, always redacted (#60)
GEN_AI_OUTPUT_MESSAGES: Final = "gen_ai.output.messages"
GEN_AI_TOOL_CALL_ARGUMENTS: Final = "gen_ai.tool.call.arguments"
GEN_AI_TOOL_CALL_RESULT: Final = "gen_ai.tool.call.result"
CONTENT_ATTRIBUTES: Final = frozenset(
    {GEN_AI_OUTPUT_MESSAGES, GEN_AI_TOOL_CALL_ARGUMENTS, GEN_AI_TOOL_CALL_RESULT}
)

# openvigie extensions (no convention yet)
OPENVIGIE_USAGE_REASONING_TOKENS: Final = "openvigie.usage.reasoning_tokens"
OPENVIGIE_USAGE_QUALITY: Final = "openvigie.usage.quality"
OPENVIGIE_AGENT_TYPE: Final = "openvigie.agent.type"
OPENVIGIE_AGENT_ID: Final = "openvigie.agent.id"

# gen_ai.operation.name values
OP_CHAT: Final = "chat"
OP_INVOKE_AGENT: Final = "invoke_agent"
OP_EXECUTE_TOOL: Final = "execute_tool"
