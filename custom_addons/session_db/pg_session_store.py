# Copyright (c) Odoo SA 2017
# @author Nicolas Seinlet
# Copyright (c) ACSONE SA 2022
# @author Stephane Bidoul
# Ported to Odoo 19 by Ethara
import base64
import functools
import json
import logging
import os
import re
import time
from hashlib import sha512

import psycopg2

import odoo
from odoo import http
from odoo.tools._vendor import sessions

_logger = logging.getLogger(__name__)

# Mirror Odoo 19 session ID constants (http.py lines 327, 947-948)
_STORED_SESSION_BYTES = 42
_base64_urlsafe_re = re.compile(r"^[A-Za-z0-9_-]{84}$")
_session_identifier_re = re.compile(r"^[A-Za-z0-9_-]{%s}$" % _STORED_SESSION_BYTES)

lock = None
if hasattr(odoo, "evented") and odoo.evented:
    import gevent.lock

    lock = gevent.lock.RLock()
elif odoo.tools.config["workers"] == 0:
    import threading

    lock = threading.RLock()


def with_lock(func):
    def wrapper(*args, **kwargs):
        try:
            if lock is not None:
                lock.acquire()
            return func(*args, **kwargs)
        finally:
            if lock is not None:
                lock.release()

    return wrapper


def with_cursor(func):
    def wrapper(self, *args, **kwargs):
        tries = 0
        while True:
            tries += 1
            try:
                self._ensure_connection()
                return func(self, *args, **kwargs)
            except (psycopg2.InterfaceError, psycopg2.OperationalError):
                self._close_connection()
                if tries > 4:
                    _logger.warning(
                        "session_db operation try %s/5 failed, aborting", tries
                    )
                    raise
                _logger.info("session_db operation try %s/5 failed, retrying", tries)

    return wrapper


class PGSessionStore(sessions.SessionStore):
    def __init__(self, uri, session_class=None):
        super().__init__(session_class)
        self._uri = uri
        self._cr = None
        self._open_connection()
        self._setup_db()

    def __del__(self):
        self._close_connection()

    @with_lock
    def _ensure_connection(self):
        if self._cr is None:
            self._open_connection()

    @with_lock
    def _open_connection(self):
        self._close_connection()
        cnx = odoo.sql_db.db_connect(self._uri, allow_uri=True)
        self._cr = cnx.cursor()
        self._cr._cnx.autocommit = True

    @with_lock
    def _close_connection(self):
        """Return cursor to the pool."""
        if self._cr is not None:
            try:
                self._cr.close()
            except Exception:  # pylint: disable=except-pass
                pass
            self._cr = None

    @with_lock
    @with_cursor
    def _setup_db(self):
        self._cr.execute(
            """
                CREATE TABLE IF NOT EXISTS http_sessions (
                    sid varchar PRIMARY KEY,
                    write_date timestamp without time zone NOT NULL,
                    payload text NOT NULL
                )
            """
        )

    # ------------------------------------------------------------------
    # Odoo 19: Override generate_key and is_valid_key to produce/accept
    # 84-char base64url session IDs (replaces the vendored 40-char hex).
    # Mirrors FilesystemSessionStore (http.py lines 1033-1053).
    # ------------------------------------------------------------------

    def generate_key(self, salt=None):
        key = str(time.time()).encode() + os.urandom(64)
        hash_key = sha512(key).digest()[:-1]  # prevent base64 padding
        return base64.urlsafe_b64encode(hash_key).decode("utf-8")

    def is_valid_key(self, key):
        return _base64_urlsafe_re.match(key) is not None

    @with_lock
    @with_cursor
    def save(self, session):
        payload = json.dumps(dict(session))
        self._cr.execute(
            """
                INSERT INTO http_sessions(sid, write_date, payload)
                    VALUES (%(sid)s, now() at time zone 'UTC', %(payload)s)
                ON CONFLICT (sid)
                DO UPDATE SET payload = %(payload)s,
                              write_date = now() at time zone 'UTC'
            """,
            dict(sid=session.sid, payload=payload),
        )

    @with_lock
    @with_cursor
    def delete(self, session):
        self._cr.execute("DELETE FROM http_sessions WHERE sid=%s", (session.sid,))

    @with_lock
    @with_cursor
    def get(self, sid):
        if not self.is_valid_key(sid):
            return self.new()
        self._cr.execute("SELECT payload FROM http_sessions WHERE sid=%s", (sid,))
        try:
            data = json.loads(self._cr.fetchone()[0])
        except Exception:
            return self.new()

        return self.session_class(data, sid, False)

    # ------------------------------------------------------------------
    # Borrow rotate from FilesystemSessionStore. It calls self.get(),
    # self.save(), self.delete(), self.generate_key() which all resolve
    # to our PG implementations.
    # ------------------------------------------------------------------
    rotate = http.FilesystemSessionStore.rotate

    # Odoo 19: New methods called by the session/device infrastructure

    def delete_old_sessions(self, session):
        """Clean up pre-rotation session after the SESSION_DELETION_TIMER
        window (120s). Called by Session._delete_old_sessions() on every
        authenticated request.
        """
        if "gc_previous_sessions" in session:
            if session["create_time"] + http.SESSION_DELETION_TIMER < time.time():
                self.delete_from_identifiers([session.sid[:_STORED_SESSION_BYTES]])
                del session["gc_previous_sessions"]
                self.save(session)

    @with_lock
    @with_cursor
    def get_missing_session_identifiers(self, identifiers):
        """Return the subset of session identifiers (first 42 chars of SID)
        that do NOT have a corresponding session in the DB.
        Called by res.device._check_revoked_sessions().
        """
        identifiers = set(identifiers)
        if not identifiers:
            return identifiers
        self._cr.execute(
            """
                SELECT DISTINCT LEFT(sid, %s)
                FROM http_sessions
                WHERE LEFT(sid, %s) IN %s
            """,
            (_STORED_SESSION_BYTES, _STORED_SESSION_BYTES, tuple(identifiers)),
        )
        found = {row[0] for row in self._cr.fetchall()}
        return identifiers - found

    @with_lock
    @with_cursor
    def delete_from_identifiers(self, identifiers):
        """Delete all sessions whose SID starts with any of the given
        42-char identifiers. Called by res.device._revoke_sessions()
        and by delete_old_sessions().
        """
        for identifier in identifiers:
            if not _session_identifier_re.match(identifier):
                raise ValueError(
                    "Identifier format incorrect, "
                    "did you pass in a string instead of a list?"
                )
        if identifiers:
            patterns = [ident + "%" for ident in identifiers]
            self._cr.execute(
                "DELETE FROM http_sessions WHERE sid LIKE ANY(%s)",
                (patterns,),
            )

    @with_lock
    @with_cursor
    def vacuum(self, max_lifetime=http.SESSION_LIFETIME):
        self._cr.execute(
            "DELETE FROM http_sessions "
            "WHERE now() at time zone 'UTC' - write_date > %s",
            (f"{max_lifetime} seconds",),
        )


_original_session_store = http.root.__class__.session_store


@functools.cached_property
def session_store(self):
    session_db_uri = os.environ.get("SESSION_DB_URI")
    if session_db_uri:
        _logger.debug("HTTP sessions stored in: db")
        return PGSessionStore(session_db_uri, session_class=http.Session)
    return _original_session_store.__get__(self, self.__class__)


_logger.debug("Monkey patching session store")
http.root.__class__.session_store = session_store
session_store.__set_name__(http.root.__class__, "session_store")
# Reset the cached_property cache so next access uses the new descriptor
vars(http.root).pop("session_store", None)
