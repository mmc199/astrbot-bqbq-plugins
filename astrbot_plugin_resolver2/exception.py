"""解析异常定义。"""


class ResolverException(Exception):
    """解析/下载基础异常"""


class ParseException(ResolverException):
    """解析失败"""


class DownloadException(ResolverException):
    """下载失败"""
