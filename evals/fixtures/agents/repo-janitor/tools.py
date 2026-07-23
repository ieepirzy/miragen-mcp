from miragen import register


@register
async def prune_branches(ctx, older_than_days: int = 30) -> str:
    """Delete local branches already merged into the default branch."""
    return "pruned"
