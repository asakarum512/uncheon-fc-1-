#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""운천 FC 커뮤니티: Python WSGI, server-rendered HTML, SQLite dev/PostgreSQL prod.

실제 인터넷에서는 DATABASE_URL, APP_SECRET_KEY, OWNER_EMAIL, OWNER_PASSWORD
환경변수를 반드시 설정하세요. Render 무료 로컬 파일은 영구 저장되지 않습니다.
"""
import copy
import hashlib
import hmac
import html
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote

from uncheon_fc import CSS, EMBLEM, ARTWORK, APPLICATION, load_data, render_home, robots_text, sitemap_xml, h

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / 'uncheon_local.sqlite3'
CONTACT = 'dudcns6405@naver.com'
OWNER_EMAIL = os.environ.get('OWNER_EMAIL', '').strip().lower()
DB_URL = os.environ.get('DATABASE_URL', '').strip()
APP_SECRET = os.environ.get('APP_SECRET_KEY', '').strip()
IS_RENDER = bool(os.environ.get('RENDER_EXTERNAL_URL') or os.environ.get('RENDER_SERVICE_ID'))
IS_PRODUCTION = IS_RENDER or bool(os.environ.get('PUBLIC_SITE_URL'))
LOCK = threading.RLock()
READY = False
SESSION_DAYS = 7
COOKIE_AUTH = 'uf_session'
COOKIE_CSRF = 'uf_csrf'

CSS_EXTRA = r'''
.community-nav { display:inline-block; font-size:13px; }
.community-hero { padding:46px 0; background:#081932;color:white; }
.community-hero h1 { font-size:33px;margin:8px 0; }
.community-shell {padding:40px 0 75px; min-height:460px;}
.community-inner { width:92%;max-width:950px;margin:auto; }
.community-card {background:#fff; border:1px solid #dce5f2;border-radius:14px;padding:24px;margin:15px 0;box-shadow:0 6px 22px rgba(9,35,77,.05);}
.community-card h2,.community-card h3{margin-top:0;line-height:1.4;color:#102e59;}
.community-card p {white-space:pre-wrap;overflow-wrap:break-word;}
.community-card .meta{color:#657997;font-size:13px;}
.community-card a.story-title {color:#0b549b;text-decoration:underline; font-size:19px;font-weight:700;}
.u-form{max-width:650px;}
.u-field{display:block;width:100%;padding:12px;margin:6px 0 16px;border:1px solid #aebfd5;border-radius:8px;font-family:inherit;font-size:16px;background:white;color:#172842;}
.u-label{font-weight:700;font-size:14px;color:#13345f;}
.u-btn{background:#166aca;color:white;border:0;border-radius:8px;padding:11px 19px;display:inline-block;font-size:15px;cursor:pointer;margin:3px 5px 3px 0;}
.u-btn:hover{background:#0d4d97;}
.u-btn.warn{background:#9b2534;}
.u-btn.gray{background:#3c5679;}
.u-msg{border-left:4px solid #206fbd;background:#ecf5ff;padding:13px 17px;margin:15px 0;}
.u-err{border-color:#bd2632;background:#fff0f0;}
.u-muted{color:#526c8d;}
.u-toolbar{margin:13px 0 22px;}
.u-toolbar a{display:inline-block;margin:4px 10px 4px 0;color:#14579a;text-decoration:underline;}
.u-filters a{margin-right:13px;color:#125596;text-decoration:underline;}
.u-row{border-bottom:1px solid #e5eaf1;padding:16px 0;}
.u-flex{display:-webkit-flex;display:flex;-webkit-flex-wrap:wrap;flex-wrap:wrap;gap:12px;align-items:center;}
.u-flex > * {max-width:100%;}
.u-smallform{display:inline-block;margin:4px 8px 4px 0;}
.u-smallform select{padding:7px;}
.u-table{width:100%;border-collapse:collapse;font-size:13px;}
.u-table td,.u-table th{border-bottom:1px solid #dce5f2;padding:12px 7px;text-align:left;word-break:break-word;}
.u-tablewrap{overflow-x:auto;}
.u-comment{border-top:1px solid #e0eaf5;padding:17px 0;}
.u-alert{background:#fff3df;border-left:4px solid #bf7416;padding:14px;margin:20px 0;}
.u-type{display:inline-block;font-size:12px;background:#e2f0fc;border-radius:6px;padding:3px 7px;color:#124477;margin-right:8px;}
@media(max-width:700px){.community-card{padding:16px}.community-hero h1{font-size:26px}.u-form{max-width:100%}}
'''

DDL = [
    '''CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'member',
    can_comment INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
    expires_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
    body TEXT NOT NULL, author_id TEXT NOT NULL, created_at TEXT NOT NULL,
    edited_at TEXT)''',
    '''CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY, post_id TEXT NOT NULL, user_id TEXT NOT NULL,
    body TEXT NOT NULL, created_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS rate_limits (
    token TEXT PRIMARY KEY, count INTEGER NOT NULL, since INTEGER NOT NULL)''',
    'CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id, created_at)',
    'CREATE INDEX IF NOT EXISTS idx_posts_date ON posts(created_at)',
    'CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)',
]


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id():
    return secrets.token_hex(16)


def hash_password(password):
    salt = secrets.token_bytes(16)
    rounds = 480000
    result = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, rounds)
    return 'pbkdf2_sha256${}${}${}'.format(rounds, salt.hex(), result.hex())


def check_password(password, encoded):
    try:
        scheme, rounds, salt, saved = encoded.split('$')
        if scheme != 'pbkdf2_sha256' or not 100000 <= int(rounds) <= 2000000:
            return False
        result = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(result, bytes.fromhex(saved))
    except (ValueError, TypeError):
        return False


def production_config_error():
    if IS_PRODUCTION and not DB_URL:
        return '영구 저장 데이터베이스(DATABASE_URL)가 아직 연결되지 않았습니다.'
    if not APP_SECRET or len(APP_SECRET) < 32:
        return 'APP_SECRET_KEY(32자 이상)를 환경변수에 등록해야 합니다.'
    if not OWNER_EMAIL or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', OWNER_EMAIL):
        return 'OWNER_EMAIL 환경변수를 등록해야 합니다.'
    if not os.environ.get('OWNER_PASSWORD') or len(os.environ['OWNER_PASSWORD']) < 12:
        return 'OWNER_PASSWORD(12자 이상)를 환경변수에 등록해야 합니다.'
    return ''


def using_postgres():
    return bool(DB_URL)


@contextmanager
def db():
    if using_postgres():
        import psycopg
        from psycopg.rows import dict_row
        connection = psycopg.connect(DB_URL, row_factory=dict_row, connect_timeout=7)
    else:
        connection = sqlite3.connect(str(DB_PATH), timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=20000')
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def run(conn, sql, args=()):
    if using_postgres():
        sql = sql.replace('?', '%s')
    return conn.execute(sql, tuple(args))


def one(conn, sql, args=()):
    row = run(conn, sql, args).fetchone()
    return dict(row) if row is not None else None


def all_rows(conn, sql, args=()):
    return [dict(r) for r in run(conn, sql, args).fetchall()]


def ensure_db():
    global READY
    if READY:
        return
    with LOCK:
        if READY:
            return
        with db() as conn:
            for item in DDL:
                run(conn, item)
            owner = one(conn, "SELECT id,role FROM users WHERE email=?", (OWNER_EMAIL,))
            if owner is None:
                run(conn, '''INSERT INTO users
                    (id,email,display_name,password_hash,role,can_comment,active,created_at)
                    VALUES(?,?,?,?,?,?,?,?)''', (
                    new_id(), OWNER_EMAIL, '운천 FC 운영자',
                    hash_password(os.environ['OWNER_PASSWORD']), 'owner', 1, 1, now_utc()))
            elif owner['role'] != 'owner':
                raise ValueError('OWNER_EMAIL address already belongs to a non-owner account')
            run(conn, 'DELETE FROM sessions WHERE expires_at<?', (now_utc(),))
            run(conn, 'DELETE FROM rate_limits WHERE since<?', (int(time.time()) - 86400,))
        READY = True


def secret():
    return APP_SECRET.encode('utf-8')


def make_csrf():
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(20)
    body = timestamp + '.' + nonce
    signature = hmac.new(secret(), ('csrf:' + body).encode(), hashlib.sha256).hexdigest()
    return body + '.' + signature


def valid_csrf(value):
    try:
        ts, nonce, mac = value.split('.')
        if len(nonce) != 40 or len(mac) != 64 or not ts.isdecimal():
            return False
        age = time.time() - int(ts)
        if age < -120 or age > 86400 * 2:
            return False
        expected = hmac.new(secret(), ('csrf:' + ts + '.' + nonce).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, expected)
    except (ValueError, AttributeError):
        return False


def encode_cookie(name, value, max_age=None, delete=False):
    cookie = SimpleCookie()
    cookie[name] = value
    cookie[name]['path'] = '/'
    cookie[name]['httponly'] = True
    cookie[name]['samesite'] = 'Lax'
    if IS_PRODUCTION:
        cookie[name]['secure'] = True
    if delete:
        cookie[name]['max-age'] = 0
        cookie[name]['expires'] = 'Thu, 01 Jan 1970 00:00:00 GMT'
    elif max_age is not None:
        cookie[name]['max-age'] = max_age
    return cookie.output(header='').strip()


class Request:
    def __init__(self, environ):
        self.environ = environ
        self.method = environ.get('REQUEST_METHOD', 'GET').upper()
        self.path = environ.get('PATH_INFO', '/') or '/'
        self.query = parse_qs(environ.get('QUERY_STRING') or '', keep_blank_values=True)
        cookies = SimpleCookie()
        try:
            cookies.load(environ.get('HTTP_COOKIE', ''))
        except Exception:
            cookies = SimpleCookie()
        self.cookies = {k: v.value for k, v in cookies.items()}
        self.extra_cookies = []
        self.csrf = self.cookies.get(COOKIE_CSRF, '')
        if not valid_csrf(self.csrf) and not production_config_error():
            self.csrf = make_csrf()
            self.extra_cookies.append(encode_cookie(COOKIE_CSRF, self.csrf, 86400))
        self.data = {}
        self.user = None

    def form(self):
        if self.method != 'POST':
            return False
        content_type = self.environ.get('CONTENT_TYPE', '').split(';')[0].strip().lower()
        if content_type != 'application/x-www-form-urlencoded':
            return False
        try:
            length = int(self.environ.get('CONTENT_LENGTH') or '0')
        except ValueError:
            return False
        if length < 1 or length > 30000:
            return False
        raw = self.environ['wsgi.input'].read(length)
        try:
            pairs = parse_qs(raw.decode('utf-8'), keep_blank_values=True, max_num_fields=15)
        except (UnicodeError, ValueError):
            return False
        self.data = {k: values[0] for k, values in pairs.items()}
        return self.csrf and valid_csrf(self.csrf) and hmac.compare_digest(self.csrf, self.data.get('_csrf', ''))

    def param(self, key):
        return self.query.get(key, [''])[0]


def load_user(req):
    token = req.cookies.get(COOKIE_AUTH, '')
    if not re.fullmatch(r'[0-9a-f]{64}', token):
        return
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db() as conn:
        req.user = one(conn, '''SELECT u.id,u.email,u.display_name,u.role,u.can_comment,u.active
            FROM sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.expires_at>? AND u.active=1''', (token_hash, now_utc()))


def csrf_hidden(req):
    return '<input type="hidden" name="_csrf" value="{}">'.format(h(req.csrf))


def form_post(path, fields, req, label, button_class='u-btn'):
    return '<form method="post" action="{}" class="u-smallform">{}{}<button class="{}" type="submit">{}</button></form>'.format(
        h(path), csrf_hidden(req), fields, h(button_class), h(label))


def current_nav(req):
    if not req.user:
        return '<a href="/board">공지·뉴스</a><a href="/login">로그인</a><a href="/register">회원가입</a>'
    msg = '<a href="/board">공지·뉴스</a>'
    if req.user['role'] == 'owner':
        msg += '<a href="/admin">관리자</a>'
    msg += '<a href="/account">내 계정</a><a href="/logout">로그아웃</a>'
    return msg


def layout(req, title, body, status=200, noindex=False):
    data = load_data()
    nav = current_nav(req)
    seo = '<meta name="robots" content="noindex,nofollow">' if noindex else ''
    headline = h(title) + ' | 운천 FC'
    head = ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta http-equiv="X-UA-Compatible" content="IE=edge">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + headline + '</title>' + seo +
            '<link rel="icon" type="image/png" href="/media/emblem.png">'
            '<style>' + CSS + '\n' + CSS_EXTRA + '</style></head><body>')
    top = ('<div class="topline"></div><header class="site-header"><div class="container header-inner">'
           '<a class="brand" href="/"><img src="/media/emblem.png" alt="운천 FC 엠블럼">'
           '<span class="brand-words"><strong>운천 FC</strong><small>OFFICIAL WEBSITE</small></span></a>'
           '<input class="menu-check" id="menuCheck" type="checkbox">'
           '<label class="menu-toggle" for="menuCheck">메뉴 열기 &#9776;</label>'
           '<nav class="nav-shell"><a href="/">홈</a>' + nav + '</nav></div></header>'
           '<div class="community-hero"><div class="community-inner">'
           '<span>UNCHEON FC / MEMBERS COMMUNITY</span><h1>' + h(title) + '</h1></div></div>'
           '<main class="community-shell"><div class="community-inner">')
    footer = ('</div></main><footer class="footer"><div class="container footer-row">'
              '<div class="footer-left"><strong>UNCHEON FC</strong><p>OFFICIAL WEBSITE</p></div>'
              '<div class="footer-right"><p>공식 문의: <a href="mailto:' + h(CONTACT) + '">' + h(CONTACT) +
              '</a></p><p><a href="/privacy">개인정보 처리 안내</a> · 회원 이메일은 공개되지 않습니다.</p></div></div></footer></body></html>')
    return status, (head + top + body + footer).encode('utf-8'), 'text/html; charset=utf-8', []


def notice(text, bad=False):
    return '<div class="u-msg{}" role="alert">{}</div>'.format(' u-err' if bad else '', h(text))


def fail(req, message, status=400):
    return layout(req, '안내', '<div class="community-card">' + notice(message, True) + '<a href="/">홈으로 돌아가기</a></div>', status, True)


def redirect(path, cookies=None):
    return 303, b'', 'text/plain; charset=utf-8', [('Location', path)] + [('Set-Cookie', value) for value in (cookies or [])]


def is_owner(req):
    return bool(req.user and req.user['role'] == 'owner' and req.user['email'] == OWNER_EMAIL and req.user['active'])


def date_view(text):
    return h(text[:16].replace('T', ' '))


def list_posts(limit=30, kind=''):
    with db() as conn:
        if kind in ('news', 'notice'):
            return all_rows(conn, '''SELECT p.*,u.display_name AS author_name FROM posts p
                JOIN users u ON p.author_id=u.id WHERE p.kind=? ORDER BY p.created_at DESC LIMIT ?''', (kind, limit))
        return all_rows(conn, '''SELECT p.*,u.display_name AS author_name FROM posts p
            JOIN users u ON p.author_id=u.id ORDER BY p.created_at DESC LIMIT ?''', (limit,))


def post_card(post):
    typename = '공지사항' if post['kind'] == 'notice' else '구단 뉴스'
    body = post['body'][:140] + ('…' if len(post['body']) > 140 else '')
    return ('<article class="community-card"><span class="u-type">' + typename + '</span>'
            '<a class="story-title" href="/post/' + h(post['id']) + '">' + h(post['title']) + '</a>'
            '<p class="u-muted">' + h(body) + '</p><div class="meta">' + date_view(post['created_at']) +
            ' · ' + h(post['author_name']) + '</div></article>')


def home(req):
    items = list_posts(5)
    data = load_data()
    data['contact_email'] = CONTACT
    data['news'] = [{'date':p['created_at'][:10], 'title':p['title'], 'summary':p['body'][:90]} for p in items]
    page = render_home(data).decode('utf-8')
    page = page.replace('</style>', CSS_EXTRA + '</style>', 1)
    page = page.replace('</nav>', '<a href="/board">공지·뉴스 게시판</a>' + current_nav(req).replace('<a href="/board">공지·뉴스</a>', '') + '</nav>', 1)
    cards = ''.join(post_card(p) for p in items) if items else '<div class="community-card"><p>등록된 뉴스와 공지사항이 없습니다. 구단 운영자가 직접 등록할 예정입니다.</p></div>'
    panel = '<section class="section section-light"><div class="community-inner"><h2>운천 FC 공식 게시판</h2><p>공식 발표는 운영자만 작성하고, 가입 회원은 댓글로 참여할 수 있습니다.</p>' + cards + '<div class="u-toolbar"><a href="/board">게시판 전체 보기 →</a></div></div></section>'
    page = page.replace('<section class="section section-dark" id="about">', panel + '<section class="section section-dark" id="about">', 1)
    return 200, page.encode('utf-8'), 'text/html; charset=utf-8', []


def board(req):
    kind = req.param('type')
    if kind not in ('notice', 'news'):
        kind = ''
    posts = list_posts(70, kind)
    filters = '<div class="u-filters"><a href="/board">전체</a><a href="/board?type=notice">공지사항</a><a href="/board?type=news">구단 뉴스</a></div>'
    intro = '<p>공식 글은 구단 운영자만 작성합니다. 일반 회원은 로그인 후 댓글만 작성할 수 있습니다.</p>'
    toolbar = '<a class="u-btn" href="/admin/post/new">새 글 작성</a>' if is_owner(req) else ''
    content = ''.join(post_card(p) for p in posts) if posts else '<div class="community-card"><p>등록된 글이 없습니다.</p></div>'
    return layout(req, '공지사항 · 구단 뉴스', filters + intro + toolbar + content)


def get_post(postid):
    with db() as conn:
        return one(conn, '''SELECT p.*,u.display_name AS author_name FROM posts p
            JOIN users u ON p.author_id=u.id WHERE p.id=?''', (postid,))


def post_page(req, postid, msg=''):
    post = get_post(postid)
    if not post:
        return fail(req, '존재하지 않는 게시글입니다.', 404)
    with db() as conn:
        comments = all_rows(conn, '''SELECT c.*, u.display_name, u.active FROM comments c
            JOIN users u ON c.user_id=u.id WHERE c.post_id=? ORDER BY c.created_at ASC LIMIT 300''', (postid,))
    title_type = '공지사항' if post['kind'] == 'notice' else '구단 뉴스'
    text = '<p><span class="u-type">' + title_type + '</span><span class="u-muted">' + date_view(post['created_at']) + '</span></p>'
    text += '<div class="community-card"><h2>' + h(post['title']) + '</h2><p>' + h(post['body']) + '</p><div class="meta">' + h(post['author_name']) + '</div>'
    if is_owner(req):
        text += '<div class="u-toolbar"><a href="/admin/post/' + h(postid) + '/edit">게시글 수정</a>'
        text += form_post('/admin/post/' + postid + '/delete', '', req, '게시글 삭제', 'u-btn warn') + '</div>'
    text += '</div><h2 id="comments">댓글</h2>'
    if msg:
        text += notice(msg)
    if comments:
        for c in comments:
            text += '<div class="community-card u-comment"><strong>' + h(c['display_name']) + '</strong><span class="u-muted"> · ' + date_view(c['created_at']) + '</span><p>' + h(c['body']) + '</p>'
            if is_owner(req) or (req.user and req.user['id'] == c['user_id']):
                text += form_post('/comment/' + h(c['id']) + '/delete', '', req, '댓글 삭제', 'u-btn warn')
            text += '</div>'
    else:
        text += '<p>등록된 댓글이 없습니다.</p>'
    if req.user and (is_owner(req) or req.user['can_comment'] == 1):
        text += '<div class="community-card"><h3>댓글 작성</h3><form method="post" action="/post/' + h(postid) + '/comment" class="u-form">'
        text += csrf_hidden(req) + '<label for="body" class="u-label">댓글 (최대 1000자)</label><textarea id="body" name="body" class="u-field" rows="4" maxlength="1000" required></textarea><button class="u-btn">댓글 등록</button></form></div>'
    elif req.user:
        text += notice('관리자 설정에 따라 이 계정의 댓글 작성이 제한되어 있습니다.', True)
    else:
        text += '<p>댓글을 쓰려면 <a href="/login">로그인</a>하거나 <a href="/register">회원가입</a>해 주세요.</p>'
    text += '<p><a href="/board">← 게시판으로 돌아가기</a></p>'
    return layout(req, post['title'], text)


def login_page(req, error=''):
    body = ('<div class="community-card u-form"><p>가입한 이메일과 비밀번호로 로그인하세요.</p>' + (notice(error, True) if error else '') +
            '<form method="post" action="/login">' + csrf_hidden(req) +
            '<label class="u-label" for="email">이메일</label><input class="u-field" type="email" id="email" name="email" autocomplete="username" maxlength="254" required>'
            '<label class="u-label" for="password">비밀번호</label><input class="u-field" type="password" id="password" name="password" autocomplete="current-password" required>'
            '<button class="u-btn">로그인</button></form><p>계정이 없다면 <a href="/register">회원가입</a>해 주세요.</p></div>')
    return layout(req, '로그인', body, noindex=True)


def register_page(req, error=''):
    body = ('<div class="community-card u-form"><p>회원가입하면 구단 게시물에 댓글을 작성할 수 있습니다. 일반 회원은 뉴스와 공지사항을 작성할 수 없습니다.</p>' + (notice(error, True) if error else '') +
            '<form method="post" action="/register">' + csrf_hidden(req) +
            '<label class="u-label" for="name">표시 이름 (2~25자)</label><input class="u-field" type="text" id="name" name="name" maxlength="25" minlength="2" autocomplete="nickname" required>'
            '<label class="u-label" for="email">이메일</label><input class="u-field" type="email" id="email" name="email" maxlength="254" autocomplete="email" required>'
            '<label class="u-label" for="password">비밀번호 (12~128자)</label><input class="u-field" type="password" id="password" name="password" minlength="12" maxlength="128" autocomplete="new-password" required>'
            '<label class="u-label" for="password2">비밀번호 확인</label><input class="u-field" type="password" id="password2" name="password2" minlength="12" maxlength="128" autocomplete="new-password" required>'
            '<div style="display:none"><label>Leave blank<input name="website" tabindex="-1" autocomplete="off"></label></div>'
            '<p class="u-muted">이메일 인증·비밀번호 재설정 메일은 아직 제공되지 않습니다. 본인이 사용하는 이메일로 가입해 주세요.</p>'
            '<p><label><input type="checkbox" name="privacy_agree" value="1" required> <a href="/privacy">개인정보 처리 안내</a>를 읽고 동의합니다. (만 14세 이상만 가입)</label></p>'
            '<button class="u-btn">회원가입</button></form></div>')
    return layout(req, '회원가입', body, noindex=True)


def attempt_allowed(conn, label, cap=10, seconds=900):
    token = hashlib.sha256((APP_SECRET + label).encode('utf-8')).hexdigest()
    now = int(time.time())
    existing = one(conn, 'SELECT count,since FROM rate_limits WHERE token=?', (token,))
    if existing and now - existing['since'] < seconds:
        if existing['count'] >= cap:
            return False
        run(conn, 'UPDATE rate_limits SET count=count+1 WHERE token=?', (token,))
    else:
        run(conn, '''INSERT INTO rate_limits(token,count,since) VALUES(?,?,?)
                ON CONFLICT(token) DO UPDATE SET count=1,since=excluded.since''', (token, 1, now))
    return True


def auth_user(email, password):
    with db() as conn:
        allowed = attempt_allowed(conn, 'login:' + email, 10, 900)
        user = one(conn, 'SELECT * FROM users WHERE email=? AND active=1', (email,))
    if not allowed:
        return None, '로그인 시도가 많습니다. 잠시 후 다시 시도해 주세요.'
    if not user or not check_password(password, user['password_hash']):
        return None, '이메일 또는 비밀번호를 확인해 주세요.'
    return user, ''


def do_login(req):
    if req.user:
        return redirect('/account')
    if not req.form():
        return fail(req, '요청이 유효하지 않습니다. 페이지를 새로고침한 후 다시 시도해 주세요.', 403)
    email = req.data.get('email', '').strip().lower()
    password = req.data.get('password', '')
    if len(email) > 254 or len(password) > 128:
        return login_page(req, '입력 내용을 확인해 주세요.')
    user, error = auth_user(email, password)
    if not user:
        return login_page(req, error)
    session = secrets.token_hex(32)
    expire = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).replace(microsecond=0).isoformat()
    with db() as conn:
        run(conn, 'INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)', (hashlib.sha256(session.encode()).hexdigest(), user['id'], expire))
    return redirect('/admin' if user['role'] == 'owner' else '/board', [encode_cookie(COOKIE_AUTH, session, SESSION_DAYS * 86400)])


def do_register(req):
    if not req.form():
        return fail(req, '요청이 유효하지 않습니다. 새로고침 후 다시 시도해 주세요.', 403)
    if req.data.get('website', ''):
        return fail(req, '회원가입 요청을 처리할 수 없습니다.', 400)
    if req.data.get('privacy_agree') != '1':
        return register_page(req, '개인정보 처리 안내 동의가 필요합니다.')
    name = req.data.get('name', '').strip()
    email = req.data.get('email', '').strip().lower()
    password = req.data.get('password', '')
    again = req.data.get('password2', '')
    if not 2 <= len(name) <= 25 or any(ord(x) < 32 for x in name):
        return register_page(req, '표시 이름은 2~25자로 입력해 주세요.')
    if not re.fullmatch(r'[^\s@]{1,64}@[^\s@]{1,180}\.[^\s@]+', email) or len(email) > 254:
        return register_page(req, '이메일 형식을 확인해 주세요.')
    if email == OWNER_EMAIL:
        return register_page(req, '이 이메일 주소는 가입에 사용할 수 없습니다.')
    if not 12 <= len(password) <= 128:
        return register_page(req, '비밀번호를 12~128자로 입력해 주세요.')
    if password != again:
        return register_page(req, '비밀번호 확인이 일치하지 않습니다.')
    with db() as conn:
        if not attempt_allowed(conn, 'register:' + email, 5, 3600):
            return register_page(req, '가입 요청이 너무 많습니다. 나중에 다시 시도해 주세요.')
        existing = one(conn, 'SELECT id FROM users WHERE email=?', (email,))
        if existing:
            return register_page(req, '이미 가입된 이메일입니다.')
        run(conn, '''INSERT INTO users(id,email,display_name,password_hash,role,can_comment,active,created_at)
             VALUES(?,?,?,?,?,?,?,?)''', (new_id(), email, name, hash_password(password), 'member', 1, 1, now_utc()))
    return redirect('/login?registered=1')


def admin(req):
    with db() as conn:
        stats_users = one(conn, 'SELECT COUNT(*) AS n FROM users')['n']
        stats_posts = one(conn, 'SELECT COUNT(*) AS n FROM posts')['n']
        stats_comments = one(conn, 'SELECT COUNT(*) AS n FROM comments')['n']
        members = all_rows(conn, 'SELECT id,email,display_name,can_comment,active,created_at FROM users WHERE role=? ORDER BY created_at DESC LIMIT 200', ('member',))
    body = '<div class="u-msg">관리자만 공지사항·구단 뉴스 작성 및 회원 권한 변경이 가능합니다. 일반 회원은 게시글 작성 권한이 없습니다.</div>'
    body += '<p>총 회원 {}명 · 게시글 {}개 · 댓글 {}개</p>'.format(stats_users, stats_posts, stats_comments)
    body += '<div class="u-toolbar"><a class="u-btn" href="/admin/post/new">공지·뉴스 작성</a><a href="/board">게시판 확인</a></div>'
    body += '<div class="community-card"><h2>회원별 댓글 권한 설정</h2><p>댓글 작성 허용·금지 및 계정 활성화 여부를 관리합니다. 게시글 작성 권한은 운영자에게만 고정됩니다.</p><div class="u-tablewrap"><table class="u-table"><thead><tr><th>회원</th><th>댓글 권한</th><th>계정 상태</th><th>변경</th></tr></thead><tbody>'
    for m in members:
        comment = '<option value="1"{}>댓글 허용</option><option value="0"{}>댓글 금지</option>'.format(' selected' if m['can_comment'] else '', ' selected' if not m['can_comment'] else '')
        active = '<option value="1"{}>활성</option><option value="0"{}>정지</option>'.format(' selected' if m['active'] else '', ' selected' if not m['active'] else '')
        body += ('<tr><td><strong>' + h(m['display_name']) + '</strong><br><small>' + h(m['email']) + '</small></td>'
                 '<td colspan="3"><form method="post" action="/admin/user/' + h(m['id']) + '/update">' + csrf_hidden(req) +
                 '<select aria-label="댓글 권한" name="can_comment">' + comment + '</select> '
                 '<select aria-label="계정 상태" name="active">' + active + '</select> '
                 '<button class="u-btn gray" type="submit">저장</button></form></td></tr>')
    body += '</tbody></table></div></div>'
    return layout(req, '운천 FC 관리자', body, noindex=True)


def edit_post(req, postid=None, error=''):
    post = get_post(postid) if postid else None
    if postid and not post:
        return fail(req, '해당 게시글을 찾을 수 없습니다.', 404)
    headline = '글 수정' if post else '공지사항·구단 뉴스 작성'
    kind = post['kind'] if post else 'notice'
    title = post['title'] if post else ''
    content = post['body'] if post else ''
    action = '/admin/post/{}/edit'.format(postid) if post else '/admin/post/new'
    body = '<div class="community-card u-form">' + (notice(error, True) if error else '')
    body += '<form method="post" action="' + h(action) + '">' + csrf_hidden(req)
    body += '<label class="u-label" for="kind">글 구분</label><select class="u-field" id="kind" name="kind">'
    body += '<option value="notice"{}>공지사항</option><option value="news"{}>구단 뉴스</option>'.format(' selected' if kind == 'notice' else '', ' selected' if kind == 'news' else '')
    body += '</select><label class="u-label" for="title">제목 (최대 120자)</label><input class="u-field" type="text" id="title" name="title" maxlength="120" value="' + h(title) + '" required>'
    body += '<label class="u-label" for="body">내용 (최대 10,000자)</label><textarea class="u-field" id="body" name="body" maxlength="10000" rows="12" required>' + h(content) + '</textarea>'
    body += '<button class="u-btn" type="submit">' + ('변경사항 저장' if post else '게시글 등록') + '</button></form></div>'
    return layout(req, headline, body, noindex=True)


def do_post_save(req, postid=None):
    if not req.form():
        return fail(req, '잘못된 요청입니다.', 403)
    title = req.data.get('title', '').strip()
    body = req.data.get('body', '').strip()
    kind = req.data.get('kind', '')
    if kind not in ('notice', 'news') or not 2 <= len(title) <= 120 or not 1 <= len(body) <= 10000:
        return edit_post(req, postid, '제목·본문 길이 또는 글 구분을 확인해 주세요.')
    with db() as conn:
        if postid:
            exists = one(conn, 'SELECT id FROM posts WHERE id=?', (postid,))
            if not exists:
                return fail(req, '게시글을 찾을 수 없습니다.', 404)
            run(conn, 'UPDATE posts SET kind=?,title=?,body=?,edited_at=? WHERE id=?', (kind, title, body, now_utc(), postid))
        else:
            postid = new_id()
            run(conn, '''INSERT INTO posts(id,kind,title,body,author_id,created_at)
                VALUES(?,?,?,?,?,?)''', (postid, kind, title, body, req.user['id'], now_utc()))
    return redirect('/post/' + postid)


def do_comment(req, postid):
    if not req.user:
        return fail(req, '로그인이 필요합니다.', 401)
    if not (is_owner(req) or req.user['can_comment']):
        return fail(req, '댓글 작성 권한이 없습니다.', 403)
    if not req.form():
        return fail(req, '잘못된 요청입니다.', 403)
    body = req.data.get('body', '').strip()
    if not 1 <= len(body) <= 1000:
        return fail(req, '댓글은 1~1000자로 작성해 주세요.')
    with db() as conn:
        if not one(conn, 'SELECT id FROM posts WHERE id=?', (postid,)):
            return fail(req, '게시글이 존재하지 않습니다.', 404)
        if not attempt_allowed(conn, 'comment:' + req.user['id'], 12, 600):
            return fail(req, '댓글 등록이 너무 잦습니다. 잠시 후 다시 시도해 주세요.', 429)
        run(conn, 'INSERT INTO comments(id,post_id,user_id,body,created_at) VALUES(?,?,?,?,?)', (new_id(), postid, req.user['id'], body, now_utc()))
    return redirect('/post/' + postid + '#comments')


def do_comment_delete(req, comment_id):
    if not req.form():
        return fail(req, '잘못된 요청입니다.', 403)
    with db() as conn:
        c = one(conn, 'SELECT id,post_id,user_id FROM comments WHERE id=?', (comment_id,))
        if not c:
            return fail(req, '댓글이 없습니다.', 404)
        if not is_owner(req) and req.user['id'] != c['user_id']:
            return fail(req, '본인 댓글만 삭제할 수 있습니다.', 403)
        run(conn, 'DELETE FROM comments WHERE id=?', (comment_id,))
    return redirect('/post/' + c['post_id'])


def account(req, message=''):
    body = ('<div class="community-card"><h2>' + h(req.user['display_name']) + '</h2><p>' + h(req.user['email']) + '</p>'
            '<p>계정 유형: ' + ('운영자' if is_owner(req) else '일반 회원') + '</p><p>댓글 권한: ' + ('허용' if is_owner(req) or req.user['can_comment'] else '제한') + '</p></div>')
    body += '<div class="community-card u-form"><h2>비밀번호 변경</h2>' + (notice(message) if message else '')
    body += '<form method="post" action="/account/password">' + csrf_hidden(req)
    body += '<label class="u-label" for="old">현재 비밀번호</label><input class="u-field" type="password" id="old" name="old" required autocomplete="current-password">'
    body += '<label class="u-label" for="new">새 비밀번호 (12~128자)</label><input class="u-field" type="password" id="new" name="new" minlength="12" maxlength="128" required autocomplete="new-password">'
    body += '<button class="u-btn">비밀번호 변경</button></form></div>'
    if not is_owner(req):
        body += '<div class="community-card u-form"><h2>회원 탈퇴</h2><p>탈퇴하면 계정과 작성한 댓글이 삭제되며 복구할 수 없습니다.</p><form method="post" action="/account/delete">' + csrf_hidden(req)
        body += '<label class="u-label" for="deletepw">비밀번호 확인</label><input class="u-field" type="password" name="password" id="deletepw" required><button class="u-btn warn" type="submit">회원 탈퇴</button></form></div>'
    return layout(req, '내 계정', body, noindex=True)


def privacy_page(req):
    body = ('<div class="community-card"><h2>개인정보 처리 안내</h2>'
            '<p><strong>관리자 및 문의:</strong> ' + h(CONTACT) + '</p>'
            '<p><strong>수집 항목:</strong> 회원가입 이메일, 표시 이름, 비밀번호의 단방향 해시. 댓글과 게시물 이용 기록, 로그인을 유지하기 위한 세션 쿠키 및 CSRF 방지 쿠키.</p>'
            '<p><strong>이용 목적:</strong> 회원 식별, 로그인, 댓글 운영, 부정 이용 방지 및 관리자에 의한 권한 설정.</p>'
            '<p><strong>보유 기간:</strong> 회원 탈퇴 시 계정 정보와 회원이 작성한 댓글을 삭제합니다. 탈퇴하지 않은 계정과 댓글은 서비스 운영 중 유지됩니다. 세션은 발급 후 7일 이내 만료됩니다.</p>'
            '<p><strong>외부 서비스:</strong> Render의 웹호스팅과 별도 PostgreSQL 데이터베이스 제공업체를 이용합니다. 실제 서비스가 사용하는 제공업체와 국외 보관 여부는 구단 운영자가 별도 확인하고 공지해야 합니다.</p>'
            '<p><strong>회원 권리:</strong> 로그인 후 내 계정에서 비밀번호 변경 및 회원 탈퇴가 가능합니다. 별도 문의는 위 이메일로 할 수 있습니다.</p>'
            '<p><strong>안내:</strong> 이메일 본인 확인은 제공되지 않으며, 만 14세 미만은 가입하지 않도록 안내합니다. 본 안내는 기능 설명용 초안으로 실제 서비스 운영 전에 법적 검토와 제공업체 정보를 보완해야 합니다.</p></div>')
    return layout(req, '개인정보 처리 안내', body, noindex=True)


def delete_account(req):
    if not req.form():
        return fail(req, '잘못된 요청입니다.', 403)
    if is_owner(req):
        return fail(req, '소유자 계정은 탈퇴할 수 없습니다.', 403)
    with db() as conn:
        row = one(conn, 'SELECT password_hash FROM users WHERE id=?', (req.user['id'],))
        if not row or not check_password(req.data.get('password',''), row['password_hash']):
            return fail(req, '비밀번호가 일치하지 않습니다.', 403)
        run(conn, 'DELETE FROM comments WHERE user_id=?', (req.user['id'],))
        run(conn, 'DELETE FROM sessions WHERE user_id=?', (req.user['id'],))
        run(conn, 'DELETE FROM users WHERE id=? AND role=?', (req.user['id'], 'member'))
    return redirect('/', [encode_cookie(COOKIE_AUTH, '', delete=True)])


def change_password(req):
    if not req.form():
        return fail(req, '잘못된 요청입니다.', 403)
    old = req.data.get('old', '')
    new = req.data.get('new', '')
    if not 12 <= len(new) <= 128:
        return fail(req, '새 비밀번호는 12~128자여야 합니다.', 400)
    with db() as conn:
        user = one(conn, 'SELECT password_hash FROM users WHERE id=?', (req.user['id'],))
        if not user or not check_password(old, user['password_hash']):
            return fail(req, '현재 비밀번호가 일치하지 않습니다.', 403)
        run(conn, 'UPDATE users SET password_hash=? WHERE id=?', (hash_password(new), req.user['id']))
        run(conn, 'DELETE FROM sessions WHERE user_id=?', (req.user['id'],))
    return redirect('/login?password_changed=1', [encode_cookie(COOKIE_AUTH, '', delete=True)])


def post_route(req, method, path):
    if method == 'GET' and path in ('/', '/index.html'):
        return home(req)
    if method == 'GET' and path == '/board':
        return board(req)
    if method == 'GET' and path == '/privacy':
        return privacy_page(req)
    if method == 'GET' and path == '/login':
        if req.user:
            return redirect('/account')
        return login_page(req, '비밀번호가 변경됐습니다. 다시 로그인해 주세요.' if req.param('password_changed') else '회원가입이 완료됐습니다. 로그인해 주세요.' if req.param('registered') else '')
    if method == 'GET' and path == '/register':
        if req.user:
            return redirect('/account')
        return register_page(req)
    if method == 'POST' and path == '/login':
        return do_login(req)
    if method == 'POST' and path == '/register':
        if req.user:
            return redirect('/account')
        return do_register(req)
    if method == 'GET' and path == '/logout':
        return layout(req, '로그아웃', '<div class="community-card"><p>로그아웃하시겠습니까?</p>' + form_post('/logout', '', req, '로그아웃') + '</div>', noindex=True)
    if method == 'POST' and path == '/logout':
        if not req.form():
            return fail(req, '잘못된 요청입니다.', 403)
        token = req.cookies.get(COOKIE_AUTH, '')
        if re.fullmatch(r'[0-9a-f]{64}', token):
            with db() as conn:
                run(conn, 'DELETE FROM sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),))
        return redirect('/', [encode_cookie(COOKIE_AUTH, '', delete=True)])
    if method == 'GET' and path == '/account':
        return account(req) if req.user else redirect('/login')
    if method == 'POST' and path == '/account/password':
        return change_password(req) if req.user else fail(req, '로그인이 필요합니다.', 401)
    if method == 'POST' and path == '/account/delete':
        return delete_account(req) if req.user else fail(req, '로그인이 필요합니다.', 401)
    if method == 'GET' and re.fullmatch(r'/post/[0-9a-f]{32}', path):
        return post_page(req, path.rsplit('/', 1)[1])
    if method == 'POST' and re.fullmatch(r'/post/[0-9a-f]{32}/comment', path):
        return do_comment(req, path.split('/')[2])
    if method == 'POST' and re.fullmatch(r'/comment/[0-9a-f]{32}/delete', path):
        if not req.user:
            return fail(req, '로그인이 필요합니다.', 401)
        return do_comment_delete(req, path.split('/')[2])
    if path.startswith('/admin'):
        if not is_owner(req):
            return fail(req, '구단 운영자만 이용할 수 있습니다.', 403)
        if method == 'GET' and path == '/admin':
            return admin(req)
        if method == 'GET' and path == '/admin/post/new':
            return edit_post(req)
        if method == 'POST' and path == '/admin/post/new':
            return do_post_save(req)
        m = re.fullmatch(r'/admin/post/([0-9a-f]{32})/(edit|delete)', path)
        if m:
            postid, operation = m.groups()
            if operation == 'edit' and method == 'GET':
                return edit_post(req, postid)
            if operation == 'edit' and method == 'POST':
                return do_post_save(req, postid)
            if operation == 'delete' and method == 'POST':
                if not req.form():
                    return fail(req, '잘못된 요청입니다.', 403)
                with db() as conn:
                    run(conn, 'DELETE FROM comments WHERE post_id=?', (postid,))
                    run(conn, 'DELETE FROM posts WHERE id=?', (postid,))
                return redirect('/board')
        m = re.fullmatch(r'/admin/user/([0-9a-f]{32})/update', path)
        if m and method == 'POST':
            if not req.form():
                return fail(req, '잘못된 요청입니다.', 403)
            if req.data.get('active') not in ('0','1') or req.data.get('can_comment') not in ('0','1'):
                return fail(req, '설정값을 확인해 주세요.')
            with db() as conn:
                run(conn, '''UPDATE users SET active=?,can_comment=? WHERE id=? AND role='member' ''',
                    (int(req.data['active']), int(req.data['can_comment']), m.group(1)))
                if req.data['active'] == '0':
                    run(conn, 'DELETE FROM sessions WHERE user_id=?', (m.group(1),))
            return redirect('/admin')
    return fail(req, '페이지가 존재하지 않습니다.', 404)


def app(environ, start_response):
    method = environ.get('REQUEST_METHOD', 'GET').upper()
    path = environ.get('PATH_INFO') or '/'
    req = None
    headers = [('X-Content-Type-Options', 'nosniff'),
               ('Referrer-Policy', 'same-origin'),
               ('X-Frame-Options', 'DENY'),
               ('Content-Security-Policy', "default-src 'self'; script-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'"),
               ('Cache-Control', 'no-store')]
    try:
        # Preserve the old public routes even before account setup.
        if method in ('GET', 'HEAD') and path in ('/media/emblem.png', '/favicon.ico', '/media/artwork.jpg', '/join-form.pdf', '/robots.txt', '/sitemap.xml', '/healthz'):
            if path in ('/media/emblem.png', '/favicon.ico'):
                status, body, mime = 200, EMBLEM, 'image/png'
                headers[-1] = ('Cache-Control', 'public, max-age=3600')
            elif path == '/media/artwork.jpg':
                status, body, mime = 200, ARTWORK, 'image/jpeg'
                headers[-1] = ('Cache-Control', 'public, max-age=3600')
            elif path == '/join-form.pdf':
                status, body, mime = 200, APPLICATION, 'application/pdf'
                headers.append(('Content-Disposition', 'attachment; filename="UncheonFC_Application.pdf"'))
            elif path == '/robots.txt':
                robots = robots_text().decode('utf-8')
                robots += 'Disallow: /login\nDisallow: /register\nDisallow: /account\nDisallow: /admin\nDisallow: /logout\n'
                status, body, mime = 200, robots.encode(), 'text/plain; charset=utf-8'
            elif path == '/sitemap.xml':
                xml = sitemap_xml()
                status, body, mime = (200, xml, 'application/xml; charset=utf-8') if xml else (503, b'Site URL not configured', 'text/plain; charset=utf-8')
            else:
                status, body, mime = 200, b'ok', 'text/plain; charset=utf-8'
            extra_headers = []
        else:
            error = production_config_error()
            if error:
                if method in ('GET','HEAD') and path in ('/', '/index.html'):
                    data = load_data()
                    data['contact_email'] = CONTACT
                    page = render_home(data).decode()
                    extra = '<section class="section"><div class="container"><p class="small-alert">회원가입과 관리자 기능 설정이 진행 중입니다. 구단 소개와 입단지원서는 이용할 수 있습니다.</p></div></section>'
                    page = page.replace('</main>', extra + '</main>')
                    status, body, mime, extra_headers = 200, page.encode(), 'text/html; charset=utf-8', []
                else:
                    status, body, mime, extra_headers = 503, ('서비스 설정 중: ' + error).encode('utf-8'), 'text/plain; charset=utf-8', []
            else:
                ensure_db()
                req = Request(environ)
                load_user(req)
                if method not in ('GET', 'HEAD', 'POST'):
                    status, body, mime, extra_headers = fail(req, '지원하지 않는 요청 방식입니다.', 405)
                else:
                    status, body, mime, extra_headers = post_route(req, 'GET' if method == 'HEAD' else method, path)
    except Exception as exc:
        # Never expose database DSNs, secrets or traceback to the public.
        import logging
        logging.exception('UncheonFC request failed: %s %s', method, path)
        if method in ('GET', 'HEAD') and path in ('/', '/index.html'):
            try:
                data = load_data()
                data['contact_email'] = CONTACT
                page = render_home(data).decode('utf-8')
                page = page.replace('</main>', '<div class="container"><p class="small-alert">게시판 연결을 확인 중입니다. 잠시 후 다시 이용해 주세요.</p></div></main>')
                status, body, mime, extra_headers = 200, page.encode('utf-8'), 'text/html; charset=utf-8', []
            except Exception:
                status, body, mime, extra_headers = 503, b'Service temporarily unavailable', 'text/plain; charset=utf-8', []
        else:
            status, body, mime, extra_headers = 503, '일시적인 서버 오류입니다. 운영자에게 문의해 주세요.'.encode(), 'text/plain; charset=utf-8', []
    code = HTTPStatus(status).phrase if status in HTTPStatus._value2member_map_ else 'Error'
    headers += [('Content-Type', mime), ('Content-Length', str(len(body)))] + extra_headers
    if req:
        headers += [('Set-Cookie', cookie) for cookie in req.extra_cookies]
    start_response('{} {}'.format(status, code), headers)
    return [] if method == 'HEAD' else [body]


if __name__ == '__main__':
    from wsgiref.simple_server import make_server
    port = int(os.environ.get('PORT', '8000'))
    print('운천 FC 홈페이지: http://127.0.0.1:{}/'.format(port))
    print('운영 주소: https://uncheon-fc-1.onrender.com')
    with make_server('127.0.0.1', port, app) as srv:
        srv.serve_forever()
