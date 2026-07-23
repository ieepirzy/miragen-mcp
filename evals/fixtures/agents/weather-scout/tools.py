from miragen import register


@register
async def get_current_weather(ctx, city: str) -> str:
    """Return the current temperature and conditions for a city."""
    return f"weather for {city}"


@register("fetch_forecast")
async def get_forecast(ctx, city: str, days: int = 7) -> str:
    """Return the 7-day forecast for a city."""
    return f"forecast for {city}"
