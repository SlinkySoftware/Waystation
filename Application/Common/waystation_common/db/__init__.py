"""MongoDB access shared by server and worker (ARCHITECTURE §3: only ``*.db`` packages touch MongoDB).

Nothing here connects at import time.
"""
