import importlib.util
import io
import unittest
from email.message import Message
from urllib.request import Request, HTTPRedirectHandler
from urllib.error import HTTPError


class PublicHTTPTests(unittest.TestCase):
    def test_redirects_are_never_followed_even_from_official_host(self):
        self.assertIsNotNone(importlib.util.find_spec('public_http'),'redirect-safe public transport missing')
        import public_http
        req=Request('https://api.elections.kalshi.com/trade-api/v2/markets/WX')
        handler=public_http.NoRedirect()
        for target in ('http://127.0.0.1:8080/private','https://example.org/false-result',
                       'https://api.elections.kalshi.com/trade-api/v2/markets/other'):
            for code in (301,302,303,307,308):
                with self.subTest(target=target,code=code):
                    self.assertIsNone(handler.redirect_request(req,io.BytesIO(),code,'Redirect',Message(),target))
        installed=[h for h in public_http.OPENER.handlers if isinstance(h,HTTPRedirectHandler)]
        self.assertEqual(len(installed),1)
        self.assertIsInstance(installed[0],public_http.NoRedirect)
