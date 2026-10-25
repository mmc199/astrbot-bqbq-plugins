"""解析结果数据模型（dataclass，无 msgspec 依赖）。"""

from dataclasses import dataclass, field


@dataclass
class VideoContent:
    """视频内容"""

    video_url: str


@dataclass
class ImageContent:
    """图片内容"""

    pic_urls: list[str] = field(default_factory=list)
    dynamic_urls: list[str] = field(default_factory=list)


@dataclass
class ParseResult:
    """完整的解析结果"""

    title: str
    author: str | None = None
    cover_url: str | None = None
    content: VideoContent | ImageContent | None = None

    @property
    def video_url(self) -> str | None:
        return self.content.video_url if isinstance(self.content, VideoContent) else None

    @property
    def pic_urls(self) -> list[str] | None:
        return self.content.pic_urls if isinstance(self.content, ImageContent) else None

    @property
    def dynamic_urls(self) -> list[str] | None:
        return self.content.dynamic_urls if isinstance(self.content, ImageContent) else None
