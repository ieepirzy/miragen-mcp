from miragen import register


@register
async def collect_metrics(ctx, source: str) -> str:
    """Collect raw metrics from a named data source."""
    return "metrics"


@register
async def render_chart(ctx, series: str, kind: str = "line") -> str:
    """Render a chart image from a metric series."""
    return "chart"


@register
async def write_report(ctx, title: str) -> str:
    """Write the assembled report to the workspace."""
    return "written"


@register
async def publish_report(ctx, destination: str) -> str:
    """Publish the finished report to a destination."""
    return "published"
