class StreamDownloader:
    """Stub downloader — direct stream downloading is not restored yet."""

    def __init__(self):
        self._sessions = {}

    async def get_livestream_info(self, *args, **kwargs):
        return None

    async def download(self, *args, **kwargs):
        raise NotImplementedError("Direct stream downloading is not restored yet")

    def has_session(self, user_id: int) -> bool:
        return user_id in self._sessions

    def get_session(self, user_id: int):
        return self._sessions.get(user_id)


stream_downloader = StreamDownloader()
