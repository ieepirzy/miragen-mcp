from miragen import register


@register
async def summarize_inbox(ctx, folder: str = "INBOX") -> str:
    """Summarize the unread threads in a mail folder."""
    return "summary"


@register
async def draft_reply(ctx, thread_id: str, tone: str = "neutral") -> str:
    """Draft a reply to a thread without sending it."""
    return "draft"


@register
async def delete_message(ctx, message_id: str) -> str:
    """Move a message to trash."""
    return "deleted"
