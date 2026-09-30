"""The panel's pages by section: the overview and spending, then each bot's persona,
behaviour, memory and moderation log."""

from backseat.web.routes import behaviour, dashboard, memory, moderation, persona, spending

ROUTERS = (dashboard.router, spending.router, persona.router, behaviour.router, memory.router, moderation.router)
