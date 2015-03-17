# -*- coding: utf-8 -*-

import time
from contextlib import contextmanager
from .connection_interface import ConnectionInterface
from ..query.grammars.grammar import QueryGrammar
from ..query.builder import QueryBuilder
from ..query.expression import QueryExpression
from ..query.processors.processor import QueryProcessor
from ..exceptions.query import QueryException


class Connection(ConnectionInterface):

    def __init__(self, connection, database='', table_prefix='', config=None):
        """
        :param connection: A dbapi connection instance
        :type connection: mixed

        :param database: The database name
        :type database: str

        :param table_prefix: The table prefix
        :type table_prefix: str

        :param config: The connection configuration
        :type config: dict
        """
        self._connection = connection
        self._cursor = None

        self._read_connection = None

        self._database = database

        self._table_prefix = table_prefix

        if config is None:
            config = {}

        self._config = config

        self._reconnector = None

        self._transactions = 0

        self._pretending = False

        self._logging_queries = False

        self._query_grammar = self.get_default_query_grammar()

        self._schema_grammar = None

        self._post_processor = self.get_default_post_processor()

        self.use_default_query_grammar()

    def use_default_query_grammar(self):
        self._query_grammar = self.get_default_query_grammar()

    def get_default_query_grammar(self):
        return QueryGrammar()

    def use_default_post_processor(self):
        self._post_processor = self.get_default_post_processor()

    def get_default_post_processor(self):
        return QueryProcessor()

    def table(self, table):
        query = QueryBuilder(self, self._query_grammar, self._post_processor)

        return query.from_(table)

    def raw(self, value):
        return QueryExpression(value)

    def select_one(self, query, bindings=None):
        if bindings is None:
            bindings = {}

        records = self.select(query, bindings)

        if len(records):
            return records[1]

        return None

    def select_from_write_connection(self, query, bindings=None):
        if bindings is None:
            bindings = {}

        return self.select(query, bindings)

    def select(self, query, bindings=None, use_read_connection=True):
        def callback(me, query_, bindings_):
            if me.pretending():
                return []

            bindings_ = me.prepare_bindings(bindings_)
            cursor = self._get_cursor_for_select(use_read_connection)
            cursor.execute(query_, bindings_)

            return cursor.fetchall()

        return self._run(query, bindings, callback)

    def _get_cursor_for_select(self, use_read_connection=True):
        if use_read_connection:
            self._cursor = self.get_read_connection().cursor()
        else:
            self._cursor = self.get_connection().cursor()

        return self._cursor

    def insert(self, query, bindings=None):
        return self.statement(query, bindings)

    def update(self, query, bindings=None):
        return self.affecting_statement(query, bindings)

    def delete(self, query, bindings=None):
        return self.affecting_statement(query, bindings)

    def statement(self, query, bindings=None):
        def callback(me, query_, bindings_):
            if me.pretending():
                return True

            bindings_ = me.prepare_bindings(bindings_)

            return me._get_cursor().execute(query_, bindings_)

        return self._run(query, bindings, callback)

    def affecting_statement(self, query, bindings=None):
        def callback(me, query_, bindings_):
            if me.pretending():
                return True

            bindings_ = me.prepare_bindings(bindings_)

            cursor = me.get_connection().cursor()
            cursor.execute(query_, bindings_)

            return cursor.rowcount

        return self._run(query, bindings, callback)

    def _get_cursor(self):
        self._cursor = self.get_connection().cursor()

        return self._cursor

    def unprepared(self, query):
        def callback(me, query_, _):
            if me.pretending():
                return True

            return bool(me.get_connection().execute(query_))

        return self._run(query, {}, callback)

    def prepare_bindings(self, bindings):
        if bindings is None:
            return []

        return bindings

    @contextmanager
    def transaction(self):
        self.begin_transaction()

        try:
            yield self
        except Exception as e:
            self.rollback()
            raise

        try:
            self.commit()
        except Exception:
            self.rollback()
            raise

    def begin_transaction(self):
        self._transactions += 1

    def commit(self):
        if self._transactions == 1:
            self._connection.commit()

        self._transactions -= 1

    def rollback(self):
        if self._transactions == 1:
            self._transactions = 0

            self._connection.rollback()
        else:
            self._transactions -= 1

    def transaction_level(self):
        return self._transactions

    def pretend(self, callback):
        logging_queries = self._logging_queries

        self.enable_query_log()

        self._pretending = True

        callback(self)

        self._pretending = False

        self._logging_queries = logging_queries

    def _run(self, query, bindings, callback):
        self._reconnect_if_missing_connection()

        start = time.time()

        try:
            result = self._run_query_callback(query, bindings, callback)
        except QueryException as e:
            result = self._try_again_if_caused_by_lost_connection(
                e, query, bindings, callback
            )

        t = self._get_elapsed_time(start)

        self.log_query(query, bindings, t)

        return result

    def _run_query_callback(self, query, bindings, callback):
        try:
            result = callback(self, query, bindings)
        except Exception as e:
            raise QueryException(query, self.prepare_bindings(bindings), e)

        return result

    def _try_again_if_caused_by_lost_connection(self, e, query,
                                                bindings, callback):
        if self._caused_by_lost_connection(e):
            self.reconnect()

            return self._run_query_callback(query, bindings, callback)

        raise e

    def _caused_by_lost_connection(self, e):
        message = e.message

        for s in ['server has gone away',
                  'no connection to the server',
                  'Lost Connection']:
            if s in message:
                return True

    def disconnect(self):
        self.set_connection(None).set_read_connection(None)

    def reconnect(self):
        if self._reconnector is not None and callable(self._reconnector):
            return self._reconnector(self)

        raise Exception('Lost connection and no reconnector available')

    def _reconnect_if_missing_connection(self):
        if self.get_connection() is None or self.get_read_connection() is None:
            self.reconnect()

    def log_query(self, query, bindings, time_=None):
        if not self._logging_queries:
            return

    def _get_elapsed_time(self, start):
        return round((time.time() - start) * 1000, 2)

    def get_connection(self):
        return self._connection

    def get_read_connection(self):
        if self._transactions >= 1:
            return self.get_connection()

        if self._read_connection is not None:
            return self._read_connection

        return self._connection

    def set_connection(self, connection):
        if self._transactions >= 1:
            raise RuntimeError("Can't swap dbapi connection"
                               "while within transaction.")

        self._connection = connection

        return self

    def set_read_connection(self, connection):
        self._read_connection = connection

        return self

    def set_reconnector(self, reconnector):
        self._reconnector = reconnector

        return self

    def get_name(self):
        return self._config.get('name')

    def get_query_grammar(self):
        return self._query_grammar

    def set_query_grammar(self, grammar):
        self._query_grammar = grammar

    def get_schema_grammar(self):
        return self._schema_grammar

    def set_schema_grammar(self, grammar):
        self._schema_grammar = grammar

    def get_post_processor(self):
        """
        Get the query post processor used by the connection

        :return: The query post processor
        :rtype: QueryProcessor
        """
        return self._post_processor

    def set_post_processor(self, processor):
        """
        Set the query post processor used by the connection

        :param processor: The query post processor
        :type processor: QueryProcessor
        """
        self._post_processor = processor

    def pretending(self):
        return self._pretending

    def enable_query_log(self):
        self._logging_queries = True

    def disable_query_log(self):
        self._logging_queries = False

    def logging(self):
        return self._logging_queries

    def get_database_name(self):
        return self._database

    def get_table_prefix(self):
        return self._table_prefix

    def set_table_prefix(self, prefix):
        self._table_prefix = prefix

        self.get_query_grammar().set_table_prefix(prefix)

    def with_table_prefix(self, grammar):
        grammar.set_table_prefix(self._table_prefix)

        return grammar

    def __enter__(self):
        self.begin_transaction()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            try:
                self.commit()
            except Exception:
                self.rollback()
                raise
        else:
            self.rollback()
            raise (exc_type, exc_val, exc_tb)