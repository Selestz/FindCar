from app.sources.transport import PublicTransport


class DromTransport(PublicTransport):
    def __init__(self) -> None:
        super().__init__("auto.drom.ru")
