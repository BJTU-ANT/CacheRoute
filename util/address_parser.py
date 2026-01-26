import re
from typing import Tuple

def parse_host_port(addr: str, default_port: int) -> Tuple[str, int]:
    """
        将类似 '10.0.0.11:8000' 的字符串解析成 ('10.0.0.11', 8000)。
        支持格式：
          - '10.0.0.11:8000'
          - 'http://10.0.0.11:8000'
          - '10.0.0.11'（无端口 → 使用 default_port）
        参数：
          addr: str 原始地址字符串
          default_port: int 如果没有端口号，则使用此端口（可选）
        返回：
          (ip_or_host, port)
    """

    # 去掉前缀协议
    addr = addr.replace("http://", "").replace("https://", "")

    # 是否带有端口
    if ":" in addr:
        host, port_str = addr.split(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            raise ValueError(f"端口号必须为整数：{addr}")
    else:
        host = addr
        port = default_port

    return host, port
