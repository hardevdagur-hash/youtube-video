from __future__ import annotations


class TestYouTubeURLParser:
    def test_parse_valid_url(self):
        from services.youtube_url_parser import YouTubeURLParser
        parser = YouTubeURLParser()
        result = parser.parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert result.video_id == "dQw4w9WgXcQ"
        assert result.url_type == "watch"

    def test_parse_short_url(self):
        from services.youtube_url_parser import YouTubeURLParser
        parser = YouTubeURLParser()
        result = parser.parse("https://youtu.be/dQw4w9WgXcQ")
        assert result.video_id == "dQw4w9WgXcQ"

    def test_parse_embed_url(self):
        from services.youtube_url_parser import YouTubeURLParser
        parser = YouTubeURLParser()
        result = parser.parse("https://www.youtube.com/embed/dQw4w9WgXcQ")
        assert result.video_id == "dQw4w9WgXcQ"

    def test_parse_invalid_url(self):
        from services.youtube_url_parser import YouTubeURLParser
        parser = YouTubeURLParser()
        result = parser.parse("https://example.com")
        assert result.error is not None

    def test_parse_empty_string(self):
        from services.youtube_url_parser import YouTubeURLParser
        parser = YouTubeURLParser()
        result = parser.parse("")
        assert result.error is not None

    def test_extract_channel_handle(self):
        from services.youtube_url_parser import YouTubeURLParser
        parser = YouTubeURLParser()
        result = parser.parse("https://www.youtube.com/@testchannel")
        assert result.error is not None and "Channel" in result.error

    def test_parse_with_extra_params(self):
        from services.youtube_url_parser import YouTubeURLParser
        parser = YouTubeURLParser()
        result = parser.parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s&list=PLabc")
        assert result.video_id == "dQw4w9WgXcQ"
