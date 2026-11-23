"""Forward only MCP content to the model. Structured metadata is for clients."""


def model_text(result):
    text = "\n".join(block["text"] for block in result.get("content", [])
                     if block.get("type") == "text")
    return ("Tool error.\n" if result.get("isError") else "") + text
