import sys
from functools import partial

import pulsar
from pulsar.utils.internet import nice_address, format_address

from .futures import multi_async, in_loop, task, coroutine_return
from .events import EventHandler
from .access import asyncio, get_event_loop, new_event_loop


__all__ = ['ProtocolConsumer',
           'Protocol',
           'DatagramProtocol',
           'Connection',
           'Producer',
           'TcpServer',
           'DatagramServer']


BIG = 2**31


class ProtocolConsumer(EventHandler):
    '''The consumer of data for a server or client :class:`Connection`.

    It is responsible for receiving incoming data from an end point via the
    :meth:`Connection.data_received` method, decoding (parsing) and,
    possibly, writing back to the client or server via
    the :attr:`transport` attribute.

    .. note::

        For server consumers, :meth:`data_received` is the only method
        to implement.
        For client consumers, :meth:`start_request` should also be implemented.

    A :class:`ProtocolConsumer` is a subclass of :class:`.EventHandler` and it
    has two default :ref:`one time events <one-time-event>`:

    * ``pre_request`` fired when the request is received (for servers) or
      just before is sent (for clients).
      This occurs just before the :meth:`start_request` method.
    * ``post_request`` fired when the request is done. The
      :attr:`on_finished` attribute is a shortcut for the ``post_request``
      :class:`.OneTime` event and therefore can be used to wait for
      the request to have received a full response (clients).

    In addition, it has two :ref:`many times events <many-times-event>`:

    * ``data_received`` fired when new data is received from the transport but
      not yet processed (before the :meth:`data_received` method is invoked)
    * ``data_processed`` fired just after data has been consumed (after the
      :meth:`data_received` method)

    .. note::

        A useful example on how to use the ``data_received`` event is
        the :ref:`wsgi proxy server <tutorials-proxy-server>`.
    '''
    _connection = None
    _data_received_count = 0
    ONE_TIME_EVENTS = ('pre_request', 'post_request')
    MANY_TIMES_EVENTS = ('data_received', 'data_processed')

    @property
    def connection(self):
        '''The :class:`Connection` of this consumer.'''
        return self._connection

    @property
    def _loop(self):
        '''The event loop of this consumer.

        The same as the :attr:`connection` event loop.
        '''
        if self._connection:
            return self._connection._loop

    @property
    def request(self):
        '''The request.

        Used for clients only and available only after the
        :meth:`start` method is invoked.
        '''
        return getattr(self, '_request', None)

    @property
    def transport(self):
        '''The :class:`Transport` of this consumer'''
        if self._connection:
            return self._connection.transport

    @property
    def address(self):
        if self._connection:
            return self._connection.address

    @property
    def producer(self):
        '''The :class:`Producer` of this consumer.'''
        if self._connection:
            return self._connection.producer

    @property
    def on_finished(self):
        '''The ``post_request`` one time event.
        '''
        return self.event('post_request')

    def connection_made(self, connection):
        '''Called by a :class:`Connection` when it starts using this consumer.

        By default it does nothing.
        '''

    def data_received(self, data):
        '''Called when some data is received.

        **This method must be implemented by subclasses** for both server and
        client consumers.

        The argument is a bytes object.
        '''

    def start_request(self):
        '''Starts a new request.

        Invoked by the :meth:`start` method to kick start the
        request with remote server. For server :class:`ProtocolConsumer` this
        method is not invoked at all.

        **For clients this method should be implemented** and it is critical
        method where errors caused by stale socket connections can arise.
        **This method should not be called directly.** Use :meth:`start`
        instead. Typically one writes some data from the :attr:`request`
        into the transport. Something like this::

            self.transport.write(self.request.encode())
        '''
        raise NotImplementedError

    def start(self, request=None):
        '''Starts processing the request for this protocol consumer.

        There is no need to override this method,
        implement :meth:`start_request` instead.
        If either :attr:`connection` or :attr:`transport` are missing, a
        :class:`RuntimeError` occurs.

        For server side consumer, this method simply fires the ``pre_request``
        event.'''
        if hasattr(self, '_request'):
            raise RuntimeError('Consumer already started')
        conn = self._connection
        if not conn:
            raise RuntimeError('Cannot start new request. No connection.')
        if not conn._transport:
            raise RuntimeError('%s has no transport.' % conn)
        conn._processed += 1
        if conn._producer:
            p = getattr(conn._producer, '_requests_processed', 0)
            conn._producer._requests_processed = p + 1
        self.bind_event('post_request', self._finished)
        self._request = request
        self.fire_event('pre_request')
        if self._request is not None:
            try:
                self.start_request()
            except Exception as exc:
                self.finished(exc=exc)

    def connection_lost(self, exc):
        '''Called by the :attr:`connection` when the transport is closed.

        By default it calls the :meth:`finished` method. It can be overwritten
        to handle the potential exception ``exc``.'''
        return self.finished(exc)

    def finished(self, *arg, **kw):
        '''Fire the ``post_request`` event if it wasn't already fired.
        '''
        if not self.event('post_request').fired():
            return self.fire_event('post_request', *arg, **kw)

    def _data_received(self, data):
        # Called by Connection, it updates the counters and invoke
        # the high level data_received method which must be implemented
        # by subclasses
        if not hasattr(self, '_request'):
            self.start()
        self._data_received_count = self._data_received_count + 1
        self.fire_event('data_received', data=data)
        result = self.data_received(data)
        self.fire_event('data_processed', data=data)
        return result

    def _finished(self, _, exc=None):
        c = self._connection
        if c and c._current_consumer is self:
            c._current_consumer = None


class PulsarProtocol(EventHandler):
    '''A mixin class for both :class:`.Protocol` and
    :class:`.DatagramProtocol`.

    A :class:`PulsarProtocol` is an :class:`.EventHandler` which has
    two :ref:`one time events <one-time-event>`:

    * ``connection_made``
    * ``connection_lost``
    '''
    ONE_TIME_EVENTS = ('connection_made', 'connection_lost')

    _transport = None
    _idle_timeout = None
    _address = None
    _type = 'server'

    def __init__(self, session=1, producer=None, timeout=0):
        super(PulsarProtocol, self).__init__()
        self._session = session
        self._timeout = timeout
        self._producer = producer
        self.bind_event('connection_lost', self._cancel_timeout)

    def __repr__(self):
        address = self._address
        if address:
            return '%s %s session %s' % (self._type, nice_address(address),
                                         self._session)
        else:
            return '<pending> session %s' % self._session
    __str__ = __repr__

    @property
    def session(self):
        '''Connection session number.

        Passed during initialisation by the :attr:`producer`.
        Usually an integer representing the number of separate connections
        the producer has processed at the time it created this
        :class:`Protocol`.
        '''
        return self._session

    @property
    def transport(self):
        '''The :ref:`transport <asyncio-transport>` for this protocol.

        Available once the :meth:`connection_made` is called.'''
        return self._transport

    @property
    def sock(self):
        '''The socket of :attr:`transport`.
        '''
        if self._transport:
            return self._transport.get_extra_info('socket')

    @property
    def address(self):
        '''The address of the :attr:`transport`.
        '''
        return self._address

    @property
    def timeout(self):
        '''Number of seconds to keep alive this connection when idle.

        A value of ``0`` means no timeout.'''
        return self._timeout

    @property
    def _loop(self):
        '''The :attr:`transport` event loop.
        '''
        if self._transport:
            return self._transport._loop

    @property
    def producer(self):
        '''The producer of this :class:`Protocol`.
        '''
        return self._producer

    @property
    def closed(self):
        '''``True`` if the :attr:`transport` is closed.'''
        return self._transport._closing if self._transport else True

    def close(self):
        '''Close by closing the :attr:`transport`.'''
        if self._transport:
            self._transport.close()

    def abort(self):
        '''Abort by aborting the :attr:`transport`.'''
        if self._transport:
            self._transport.abort()

    def connection_made(self, transport):
        '''Sets the :attr:`transport`, fire the ``connection_made`` event
        and adds a :attr:`timeout` for idle connections.
        '''
        if self._transport is not None:
            self._cancel_timeout()
        self._transport = transport
        addr = self._transport.get_extra_info('peername')
        if not addr:
            self._type = 'client'
            addr = self._transport.get_extra_info('sockname')
        self._address = addr
        # let everyone know we have a connection with endpoint
        self.fire_event('connection_made')
        self._add_idle_timeout()

    def connection_lost(self, exc=None):
        '''Fires the ``connection_lost`` event.
        '''
        self.fire_event('connection_lost', exc=exc)

    def eof_received(self):
        '''The socket was closed from the remote end'''

    def set_timeout(self, timeout):
        '''Set a new :attr:`timeout` for this connection.'''
        self._cancel_timeout()
        self._timeout = timeout
        self._add_idle_timeout()

    def info(self):
        connection = {'session': self._session,
                      'timeout': self._timeout}
        info = {'connection': connection}
        if self._producer:
            info.update(self._producer.info())
        return info

    ########################################################################
    #    INTERNALS
    def _timed_out(self):
        self.close()
        self.logger.debug('Closed idle %s.', self)

    def _add_idle_timeout(self):
        if not self.closed and not self._idle_timeout and self._timeout:
            self._idle_timeout = self._loop.call_later(self._timeout,
                                                       self._timed_out)

    def _cancel_timeout(self, *args, **kw):
        if self._idle_timeout:
            self._idle_timeout.cancel()
            self._idle_timeout = None


class Protocol(PulsarProtocol, asyncio.Protocol):
    '''An :class:`asyncio.Protocol` with :ref:`events <event-handling>`
    '''


class DatagramProtocol(PulsarProtocol, asyncio.DatagramProtocol):
    '''An ``asyncio.DatagramProtocol`` with events`'''


class Connection(Protocol):
    '''A :class:`Protocol` to handle multiple request/response.

    It is a class which acts as bridge between a
    :ref:`transport <asyncio-transport>` and a :class:`.ProtocolConsumer`.
    It routes data arriving from the transport to the
    :meth:`current_consumer`.

    .. attribute:: _consumer_factory

        A factory of :class:`.ProtocolConsumer`.

    .. attribute:: _processed

        number of separate requests processed.
    '''
    _current_consumer = None

    def __init__(self, consumer_factory=None, **kw):
        super(Connection, self).__init__(**kw)
        self._processed = 0
        self._consumer_factory = consumer_factory
        self.bind_event('connection_lost', self._connection_lost)

    def current_consumer(self):
        '''The :class:`ProtocolConsumer` currently handling incoming data.

        This instance will receive data when this connection get data
        from the :attr:`~Protocol.transport` via the :meth:`data_received`
        method.
        '''
        if self._current_consumer is None:
            self._build_consumer(None)
        return self._current_consumer

    def set_consumer(self, consumer):
        assert self._current_consumer is None, 'Consumer is not None'
        self._current_consumer = consumer
        consumer._connection = self
        consumer.connection_made(self)

    def data_received(self, data):
        '''Delegates handling of data to the :meth:`current_consumer`.

        Once done set a timeout for idle connections when a
        :attr:`~Protocol.timeout` is a positive number (of seconds).
        '''
        self._cancel_timeout()
        while data:
            consumer = self.current_consumer()
            data = consumer._data_received(data)
        self._add_idle_timeout()

    def upgrade(self, consumer_factory):
        '''Upgrade the :func:`_consumer_factory` callable.

        This method can be used when the protocol specification changes
        during a response (an example is a WebSocket request/response,
        or HTTP tunneling).

        This method adds a ``post_request`` callback to the
        :meth:`current_consumer` to build a new consumer with the new
        :func:`_consumer_factory`.

        :param consumer_factory: the new consumer factory (a callable
            accepting no parameters)
        :return: ``None``.
        '''
        self._consumer_factory = consumer_factory
        consumer = self._current_consumer
        if consumer:
            consumer.bind_event('post_request', self._build_consumer)
        else:
            self._build_consumer(None)

    def info(self):
        info = super(Connection, self).info()
        connection = info['connection']
        connection.update({'request_processed': self._processed})
        return info

    def _build_consumer(self, _, exc=None):
        if not exc:
            consumer = self._producer.build_consumer(self._consumer_factory)
            self.set_consumer(consumer)

    def _connection_lost(self, conn, exc=None):
        '''It performs these actions in the following order:

        * Fires the ``connection_lost`` :ref:`one time event <one-time-event>`
          if not fired before, with ``exc`` as event data.
        * Cancel the idle timeout if set.
        * Invokes the :meth:`ProtocolConsumer.connection_lost` method in the
          :meth:`current_consumer`.
          '''
        if conn._current_consumer:
            conn._current_consumer.connection_lost(exc)


class Producer(EventHandler):
    '''An Abstract :class:`.EventHandler` class for all producers of
    connections.
    '''
    _requests_processed = 0
    _sessions = 0

    protocol_factory = None
    '''A callable producing protocols.

    The signature of the protocol factory callable must be::

        protocol_factory(session, producer, **params)
    '''

    def __init__(self, loop):
        self._loop = loop or get_event_loop() or new_event_loop()
        super(Producer, self).__init__(self._loop)

    @property
    def sessions(self):
        '''Total number of protocols created by the :class:`Producer`.
        '''
        return self._sessions

    @property
    def requests_processed(self):
        '''Total number of requests processed.
        '''
        return self._requests_processed

    def create_protocol(self):
        '''Create a new protocol via the :meth:`protocol_factory`

        This method increase the count of :attr:`sessions` and build
        the protocol passing ``self`` as the producer.
        '''
        self._sessions = session = self._sessions + 1
        return self.protocol_factory(session=session, producer=self)

    def build_consumer(self, consumer_factory):
        '''Build a consumer for a protocol.

        This method can be used by protocols which handle several requests,
        for example the :class:`Connection` class.

        :param consumer_factory: consumer factory to use.
        '''
        consumer = consumer_factory()
        consumer.copy_many_times_events(self)
        return consumer


class TcpServer(Producer):
    '''A :class:`Producer` of server :class:`Connection` for TCP servers.

    .. attribute:: _server

        A :class:`.Server` managed by this Tcp wrapper.

        Available once the :meth:`start_serving` method has returned.
    '''
    ONE_TIME_EVENTS = ('start', 'stop')
    MANY_TIMES_EVENTS = ('connection_made', 'pre_request', 'post_request',
                         'connection_lost')
    _server = None
    _started = None

    def __init__(self, protocol_factory, loop, address=None,
                 name=None, sockets=None, max_connections=None,
                 keep_alive=None):
        super(TcpServer, self).__init__(loop)
        self.protocol_factory = protocol_factory
        self._name = name or self.__class__.__name__
        self._params = {'address': address, 'sockets': sockets}
        self._max_connections = max_connections
        self._keep_alive = keep_alive
        self._concurrent_connections = set()

    def __repr__(self):
        address = self.address
        if address:
            return '%s %s' % (self.__class__.__name__, address)
        else:
            return self.__class__.__name__
    __str_ = __repr__

    @property
    def address(self):
        '''Socket address of this server.

        It is obtained from the first socket ``getsockname`` method.
        '''
        if self._server is not None:
            return self._server.sockets[0].getsockname()

    @task
    def start_serving(self, backlog=100, sslcontext=None):
        '''Start serving.

        :param backlog: Number of maximum connections
        :param sslcontext: optional SSLContext object.
        :return: a :class:`.Future` called back when the server is
            serving the socket.'''
        if hasattr(self, '_params'):
            address = self._params['address']
            sockets = self._params['sockets']
            del self._params
            create_server = self._loop.create_server
            try:
                if sockets:
                    server = None
                    for sock in sockets:
                        srv = yield create_server(self.create_protocol,
                                                  sock=sock,
                                                  backlog=backlog,
                                                  ssl=sslcontext)
                        if server:
                            server.sockets.extend(srv.sockets)
                        else:
                            server = srv
                else:
                    if isinstance(address, tuple):
                        server = yield create_server(self.create_protocol,
                                                     host=address[0],
                                                     port=address[1],
                                                     backlog=backlog,
                                                     ssl=sslcontext)
                    else:
                        raise NotImplementedError
                self._server = server
                self._started = self._loop.time()
                for sock in server.sockets:
                    address = sock.getsockname()
                    self.logger.info('%s serving on %s', self._name,
                                     format_address(address))
                self.fire_event('start')
            except Exception as exc:
                self.fire_event('start', exc=exc)

    def stop_serving(self):
        '''Stop serving the :attr:`.Server.sockets`.
        '''
        if self._server:
            server, self._server = self._server, None
            server.close()

    @task
    def close(self):
        '''Stop serving the :attr:`.Server.sockets` and close all
        concurrent connections.
        '''
        if self._server:
            server, self._server = self._server, None
            server.close()
            yield None
            yield self._close_connections()
            self.fire_event('stop')
        coroutine_return(self)

    def info(self):
        sockets = []
        up = int(self._loop.time() - self._started) if self._started else 0
        server = {'pulsar_version': pulsar.__version__,
                  'python_version': sys.version,
                  'uptime_in_seconds': up,
                  'sockets': sockets,
                  'max_connections': self._max_connections,
                  'keep_alive': self._keep_alive}
        clients = {'processed_clients': self._sessions,
                   'connected_clients': len(self._concurrent_connections),
                   'requests_processed': self._requests_processed}
        if self._server:
            for sock in self._server.sockets:
                sockets.append({
                    'address': format_address(sock.getsockname())})
        return {'server': server,
                'clients': clients}

    def create_protocol(self):
        '''Override :meth:`Producer.create_protocol`.
        '''
        self._sessions = session = self._sessions + 1
        protocol = self.protocol_factory(session=session,
                                         producer=self,
                                         timeout=self._keep_alive)
        protocol.bind_event('connection_made', self._connection_made)
        protocol.bind_event('connection_lost', self._connection_lost)
        if (self._server and self._max_connections and
                session >= self._max_connections):
            self.logger.info('Reached maximum number of connections %s. '
                             'Stop serving.' % self._max_connections)
            self.close()
        return protocol

    #    INTERNALS
    def _connection_made(self, connection, exc=None):
        if not exc:
            self._concurrent_connections.add(connection)

    def _connection_lost(self, connection, exc=None):
        self._concurrent_connections.discard(connection)

    def _close_connections(self, connection=None):
        '''Close ``connection`` if specified, otherwise close all connections.

        Return a list of :class:`.Future` called back once the connection/s
        are closed.
        '''
        all = []
        if connection:
            all.append(connection.event('connection_lost'))
            connection.transport.close()
        else:
            connections = list(self._concurrent_connections)
            self._concurrent_connections = set()
            for connection in connections:
                all.append(connection.event('connection_lost'))
                connection.transport.close()
        if all:
            self.logger.info('%s closing %d connections', self, len(all))
            return multi_async(all)


class DatagramServer(EventHandler):
    '''An :class:`.EventHandler` for serving UDP sockets.

    .. attribute:: _transports

        A list of :class:`.DatagramTransport`.

        Available once the :meth:`create_endpoint` method has returned.
    '''
    _transports = None
    _started = None

    ONE_TIME_EVENTS = ('start', 'stop')
    MANY_TIMES_EVENTS = ('pre_request', 'post_request')

    def __init__(self, protocol_factory, loop=None, address=None,
                 name=None, sockets=None, max_requests=None):
        self._loop = loop or get_event_loop()
        super(DatagramServer, self).__init__(self._loop)
        self.protocol_factory = protocol_factory
        self._max_requests = max_requests
        self._requests_processed = 0
        self._name = name or self.__class__.__name__
        self._params = {'address': address, 'sockets': sockets}

    @task
    def create_endpoint(self, **kw):
        '''create the server endpoint.

        :return: a :class:`~asyncio.Future` called back when the server is
            serving the socket.
        '''
        if hasattr(self, '_params'):
            address = self._params['address']
            sockets = self._params['sockets']
            del self._params
            try:
                transports = []
                if sockets:
                    for transport in sockets:
                        proto = self.create_protocol()
                        transports.append(transport(self._loop, proto))
                else:
                    transport, _ = yield self._loop.create_datagram_endpoint(
                        self.protocol_factory, local_addr=adress)
                    transports.append(transport)
                self._transports = transports
                self._started = self._loop.time()
                for transport in self._transports:
                    address = transport.get_extra_info('sockname')
                    self.logger.info('%s serving on %s', self._name,
                                     format_address(address))
                self.fire_event('start')
            except Exception as exc:
                self.logger.exception('Error while starting UDP server')
                self.fire_event('start', exc=exc)
                self.fire_event('stop')

    @in_loop
    def close(self):
        '''Stop serving the :attr:`.Server.sockets` and close all
        concurrent connections.
        '''
        if self._transports:
            transports, self._transports = self._transports, None
            for transport in transports:
                transport.close()
            self.fire_event('stop')
        coroutine_return(self)

    def info(self):
        sockets = []
        up = int(self._loop.time() - self._started) if self._started else 0
        server = {'pulsar_version': pulsar.__version__,
                  'python_version': sys.version,
                  'uptime_in_seconds': up,
                  'sockets': sockets,
                  'max_requests': self._max_requests}
        clients = {'requests_processed': self._requests_processed}
        if self._transports:
            for transport in self._transports:
                sockets.append({
                    'address': format_address(transport._sock.getsockname())})
        return {'server': server,
                'clients': clients}

    def create_protocol(self):
        '''Override :meth:`Producer.create_protocol`.
        '''
        return self.protocol_factory(producer=self)
