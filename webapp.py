#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""운천 FC 공개 배포용 WSGI 진입점 (Python + Gunicorn)."""
from urllib.parse import unquote
from uncheon_fc import (
    load_data, render_home, EMBLEM, ARTWORK, APPLICATION,
    robots_text, sitemap_xml,
)


def app(environ, start_response):
    method = environ.get('REQUEST_METHOD', 'GET').upper()
    path = environ.get('PATH_INFO') or '/'
    headers = [('X-Content-Type-Options', 'nosniff'),
               ('Referrer-Policy', 'strict-origin-when-cross-origin')]
    status = '200 OK'
    cache = False
    if method not in ('GET', 'HEAD'):
        status, body, ctype = '405 Method Not Allowed', b'Method Not Allowed', 'text/plain; charset=utf-8'
        headers.append(('Allow', 'GET, HEAD'))
    elif path in ('/', '/index.html'):
        try:
            body = render_home(load_data())
            ctype = 'text/html; charset=utf-8'
        except (OSError, ValueError, TypeError):
            status, body, ctype = '500 Internal Server Error', b'Site configuration error', 'text/plain; charset=utf-8'
    elif path in ('/media/emblem.png', '/favicon.ico'):
        body, ctype, cache = EMBLEM, 'image/png', True
    elif path == '/media/artwork.jpg':
        body, ctype, cache = ARTWORK, 'image/jpeg', True
    elif path == '/join-form.pdf':
        body, ctype = APPLICATION, 'application/pdf'
        headers.append(('Content-Disposition', 'attachment; filename="UncheonFC_Application.pdf"'))
    elif path == '/robots.txt':
        body, ctype = robots_text(), 'text/plain; charset=utf-8'
    elif path == '/sitemap.xml':
        body = sitemap_xml()
        if body is None:
            status, body, ctype = '503 Service Unavailable', b'Public URL not configured', 'text/plain; charset=utf-8'
        else:
            ctype = 'application/xml; charset=utf-8'
    elif path == '/healthz':
        body, ctype = b'ok', 'text/plain; charset=utf-8'
    else:
        status, body, ctype = '404 Not Found', '요청하신 페이지를 찾을 수 없습니다.'.encode('utf-8'), 'text/plain; charset=utf-8'
    headers.extend([
        ('Content-Type', ctype),
        ('Content-Length', str(len(body))),
        ('Cache-Control', 'public, max-age=3600' if cache else 'no-cache'),
    ])
    start_response(status, headers)
    return [] if method == 'HEAD' else [body]
