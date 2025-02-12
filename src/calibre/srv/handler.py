#!/usr/bin/env python2
# vim:fileencoding=utf-8
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__ = 'GPL v3'
__copyright__ = '2015, Kovid Goyal <kovid at kovidgoyal.net>'

import os
from binascii import hexlify
from collections import OrderedDict
from importlib import import_module
from threading import Lock

from calibre.db.cache import Cache
from calibre.db.legacy import create_backend, LibraryDatabase
from calibre.srv.routes import Router
from calibre.srv.session import Sessions
from calibre.utils.date import utcnow

def init_library(library_path):
    db = Cache(create_backend(library_path))
    db.init()
    return db

class LibraryBroker(object):

    def __init__(self, libraries):
        self.lock = Lock()
        self.lmap = {}
        seen = set()
        for i, path in enumerate(os.path.abspath(p) for p in libraries):
            if path in seen:
                continue
            seen.add(path)
            if not LibraryDatabase.exists_at(path):
                continue
            bname = library_id = hexlify(os.path.basename(path).encode('utf-8')).decode('ascii')
            c = 0
            while library_id in self.lmap:
                c += 1
                library_id = bname + '%d' % c
            if i == 0:
                self.default_library = library_id
            self.lmap[library_id] = path
        self.category_caches = {lid:OrderedDict() for lid in self.lmap}
        self.search_caches = {lid:OrderedDict() for lid in self.lmap}

    def get(self, library_id=None):
        with self.lock:
            library_id = library_id or self.default_library
            ans = self.lmap.get(library_id)
            if ans is None:
                return
            if not callable(getattr(ans, 'init', None)):
                try:
                    self.lmap[library_id] = ans = init_library(ans)
                    ans.server_library_id = library_id
                except Exception:
                    self.lmap[library_id] = ans = None
                    raise
            return ans

    def close(self):
        for db in self.lmap.itervalues():
            getattr(db, 'close', lambda : None)()
        self.lmap = {}

class Context(object):

    log = None
    url_for = None
    CATEGORY_CACHE_SIZE = 25
    SEARCH_CACHE_SIZE = 100
    SESSION_COOKIE = 'calibre_session'

    def __init__(self, libraries, opts, testing=False):
        self.opts = opts
        self.library_broker = LibraryBroker(libraries)
        self.testing = testing
        self.lock = Lock()
        self.sessions = Sessions()

    def init_session(self, endpoint, data):
        data.session = self.sessions.get_or_create(key=data.cookies.get(self.SESSION_COOKIE), username=data.username)

    def finalize_session(self, endpoint, data, output):
        data.outcookie[self.SESSION_COOKIE] = data.session.key
        data.outcookie[self.SESSION_COOKIE]['path'] = self.url_for(None)

    def get_library(self, library_id=None):
        return self.library_broker.get(library_id)

    def allowed_book_ids(self, data, db):
        # TODO: Implement this based on data.username caching result on the
        # data object
        with self.lock:
            ans = data.allowed_book_ids.get(db.server_library_id)
            if ans is None:
                ans = data.allowed_book_ids[db.server_library_id] = db.all_book_ids()
            return ans

    def get_categories(self, data, db, restrict_to_ids=None):
        if restrict_to_ids is None:
            restrict_to_ids = self.allowed_book_ids(data, db)
        with self.lock:
            cache = self.library_broker.category_caches[db.server_library_id]
            old = cache.pop(restrict_to_ids, None)
            if old is None or old[0] <= db.last_modified():
                categories = db.get_categories(book_ids=restrict_to_ids)
                cache[restrict_to_ids] = old = (utcnow(), categories)
                if len(cache) > self.CATEGORY_CACHE_SIZE:
                    cache.popitem(last=False)
            else:
                cache[restrict_to_ids] = old
            return old[1]

    def search(self, data, db, query, restrict_to_ids=None):
        if restrict_to_ids is None:
            restrict_to_ids = self.allowed_book_ids(data, db)
        with self.lock:
            cache = self.library_broker.search_caches[db.server_library_id]
            key = (query, restrict_to_ids)
            old = cache.pop(key, None)
            if old is None or old[0] < db.clear_search_cache_count:
                matches = db.search(query, book_ids=restrict_to_ids)
                cache[key] = old = (db.clear_search_cache_count, matches)
                if len(cache) > self.SEARCH_CACHE_SIZE:
                    cache.popitem(last=False)
            else:
                cache[key] = old
            return old[1]

class Handler(object):

    def __init__(self, libraries, opts, testing=False):
        self.router = Router(ctx=Context(libraries, opts, testing=testing), url_prefix=opts.url_prefix)
        for module in ('content', 'ajax', 'code'):
            module = import_module('calibre.srv.' + module)
            self.router.load_routes(vars(module).itervalues())
        self.router.finalize()
        self.router.ctx.url_for = self.router.url_for
        self.dispatch = self.router.dispatch

    def set_log(self, log):
        self.router.ctx.log = log

    def close(self):
        self.router.ctx.library_broker.close()

