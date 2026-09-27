"""The panel's pages by section: the overview, then each bot's persona, behaviour and memory."""

from backseat.web.routes import behaviour, dashboard, memory, persona

ROUTERS = (dashboard.router, persona.router, behaviour.router, memory.router)
