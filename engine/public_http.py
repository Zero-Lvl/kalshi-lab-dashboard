"""Do not let public-data redirects silently change source identity or scheme."""
from urllib.request import HTTPRedirectHandler, build_opener, Request


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Returning None causes urllib to raise the 3xx HTTPError, not follow it.
        return None


OPENER = build_opener(NoRedirect())


def open_public(request, timeout=7):
    """HTTPS only, no redirects. Callers additionally constrain endpoint hosts."""
    url = request.full_url if isinstance(request,Request) else str(request)
    from urllib.parse import urlsplit
    p = urlsplit(url)
    if p.scheme != 'https' or p.username or p.password or p.port not in (None,443):
        raise ValueError('HTTPS public sources only; no credentials or alternate ports')
    return OPENER.open(request,timeout=timeout)
