import re
from typing import Tuple

def parse_host_port(addr: str, default_port: int) -> Tuple[str, int]:
    """
        Parse a string like '10.0.0.11:8000' into ('10.0.0.11', 8000).
        Supported formats:
          - '10.0.0.11:8000'
          - 'http://10.0.0.11:8000'
          - '10.0.0.11'(no port -> use default_port)
        Parameters:
          addr: str raw address string
          default_port: int use this port when no port is provided (optional)
        Returns:
          (ip_or_host, port)
    """

    # Remove the protocol prefix
    addr = addr.replace("http://", "").replace("https://", "")

    # Whether the address includes a port
    if ":" in addr:
        host, port_str = addr.split(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            raise ValueError(f"Port must be an integer: {addr}")
    else:
        host = addr
        port = default_port

    return host, port
