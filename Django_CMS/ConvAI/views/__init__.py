# _panel is a private module — the other view modules import its helpers
# directly. Only the fragment endpoint is a route, so only it is re-exported.
from ._panel import panel_fragment
from .account import *
from .agents import *
from .alerts import *
from .calls import *
from .chat import *
from .communications import *
from .dashboard import *
from .feedback import *
from .media import *
from .notes import *
from .patients import *
from .protocols import *
from .settings_views import *
from .summaries import *
from .users import *
