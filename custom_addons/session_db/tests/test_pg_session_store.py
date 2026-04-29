import logging
import re
import time
from unittest import mock

import psycopg2

from odoo import http
from odoo.sql_db import connection_info_for
from odoo.tests.common import TransactionCase
from odoo.tools import config

from odoo.addons.session_db.pg_session_store import (
    PGSessionStore,
    _STORED_SESSION_BYTES,
)


def _make_postgres_uri(
    user=None, password=None, host=None, port=None, database=None, **kwargs
):
    uri = ["postgres://"]
    if user:
        uri.append(user)
        if password:
            uri.append(f":{password}")
        uri.append("@")
    if host:
        uri.append(host)
        if port:
            uri.append(f":{port}")
    uri.append("/")
    if database:
        uri.append(database)
    return "".join(uri)


class TestPGSessionStore(TransactionCase):
    def setUp(self):
        super().setUp()
        _, connection_info = connection_info_for(config["db_name"])
        self.session_store = PGSessionStore(
            _make_postgres_uri(**connection_info), session_class=http.Session
        )

    def test_session_crud(self):
        session = self.session_store.new()
        assert len(session.sid) == 84
        assert re.match(r"^[A-Za-z0-9_-]{84}$", session.sid)
        session["test"] = "test"
        self.session_store.save(session)
        assert session.sid is not None
        assert self.session_store.get(session.sid)["test"] == "test"
        self.session_store.delete(session)
        assert self.session_store.get(session.sid).get("test") is None

    def test_generate_key(self):
        key = self.session_store.generate_key()
        assert len(key) == 84
        assert re.match(r"^[A-Za-z0-9_-]{84}$", key)
        assert key != self.session_store.generate_key()

    def test_is_valid_key(self):
        assert self.session_store.is_valid_key(self.session_store.generate_key())
        assert not self.session_store.is_valid_key("a" * 40)
        assert not self.session_store.is_valid_key("invalid")

    def test_delete_from_identifiers(self):
        s1 = self.session_store.new()
        s1["x"] = 1
        self.session_store.save(s1)
        identifier = s1.sid[:_STORED_SESSION_BYTES]
        self.session_store.delete_from_identifiers([identifier])
        assert self.session_store.get(s1.sid).get("x") is None

    def test_get_missing_session_identifiers(self):
        s1 = self.session_store.new()
        self.session_store.save(s1)
        existing_id = s1.sid[:_STORED_SESSION_BYTES]
        fake_id = self.session_store.generate_key()[:_STORED_SESSION_BYTES]
        missing = self.session_store.get_missing_session_identifiers(
            [existing_id, fake_id]
        )
        assert existing_id not in missing
        assert fake_id in missing

    def test_delete_old_sessions(self):
        s1 = self.session_store.new()
        s1["gc_previous_sessions"] = True
        s1["create_time"] = time.time() - 200
        self.session_store.save(s1)
        self.session_store.delete_old_sessions(s1)
        assert "gc_previous_sessions" not in s1

    def test_get_invalid_sid_returns_new(self):
        session = self.session_store.get("invalid_key")
        assert session.is_new

    def test_retry(self):
        valid_sid = self.session_store.generate_key()
        with (
            mock.patch("odoo.sql_db.Cursor.execute") as mock_execute,
            self.assertLogs(level=logging.WARNING) as logs,
        ):
            mock_execute.side_effect = psycopg2.OperationalError()
            try:
                self.session_store.get(valid_sid)
            except psycopg2.OperationalError:  # pylint: disable=except-pass
                pass
            else:
                raise AssertionError("expected psycopg2.OperationalError")
            assert mock_execute.call_count == 5
            self.assertEqual(len(logs.records), 1)
            self.assertEqual(logs.records[0].levelno, logging.WARNING)
            self.assertIn("operation try 5/5 failed, aborting", logs.output[0])
        self.session_store.get(valid_sid)

    def test_retry_connect_fail(self):
        valid_sid = self.session_store.generate_key()
        with (
            mock.patch("odoo.sql_db.Cursor.execute") as mock_execute,
            mock.patch("odoo.sql_db.db_connect") as mock_db_connect,
        ):
            mock_execute.side_effect = psycopg2.OperationalError()
            mock_db_connect.side_effect = RuntimeError("connection failed")
            try:
                self.session_store.get(valid_sid)
            except RuntimeError:  # pylint: disable=except-pass
                pass
            else:
                raise AssertionError("expected RuntimeError")
            assert mock_execute.call_count == 1
        self.session_store.get(valid_sid)

    def test_make_postgres_uri(self):
        connection_info = {
            "host": "localhost",
            "port": 5432,
            "database": "test",
            "user": "test",
            "password": "PASSWORD",
        }
        assert "postgres://test:PASSWORD@localhost:5432/test" == _make_postgres_uri(
            **connection_info
        )
