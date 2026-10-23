"""Page handlers, one module per area of the UI."""
from . import account, admin, connections, dashboard, oauth

routes = [*account.routes, *dashboard.routes, *connections.routes, *admin.routes, *oauth.routes]
