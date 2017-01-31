import re
import sys
import zlib
from enum import Enum
from urllib.parse import urlparse
from collections import namedtuple

from ..exceptions import ProtocolError


METHOD_RE = re.compile(b"[A-Z0-9$-_.]{3,20}")
VERSION_RE = re.compile(b"HTTP/(\d+).(\d+)")
STATUS_RE = re.compile(b"(\d{3})\s*(\w*)")
HEADER_RE = re.compile(b"[\x00-\x1F\x7F()<>@,;:\[\]={} \t\\\\\"]")

# errors
BAD_FIRST_LINE = 0
INVALID_HEADER = 1
INVALID_CHUNK = 2

CHARSET = 'ISO-8859-1'


ParsedUrl = namedtuple('ParsedUrl',
                       'schema host port path query fragment userinfo')


class HttpParserError(ProtocolError):
    pass


class HttpParserCallbackError(HttpParserError):
    pass


class HttpParserInvalidStatusError(HttpParserError):
    pass


class HttpParserInvalidMethodError(HttpParserError):
    pass


class HttpParserInvalidURLError(HttpParserError):
    pass


class HttpParserUpgrade(Exception):
    pass


class ParserType(Enum):
    HTTP_REQUEST = 0
    HTTP_RESPONSE = 1
    HTTP_BOTH = 2


class F(Enum):
    CHUNKED = 1 << 0
    CONNECTION_KEEP_ALIVE = 1 << 1
    CONNECTION_CLOSE = 1 << 2
    CONNECTION_UPGRADE = 1 << 3
    TRAILING = 1 << 4
    UPGRADE = 1 << 5
    SKIPBODY = 1 << 6
    CONTENTLENGTH = 1 << 7


def passthrough(*args):
    pass


def parse_url(url):
    p = urlparse(url)
    host_port = p.netloc.split(b':', 1)
    host = host_port[0]
    port = int(host_port[1]) if len(host_port) == 2 else None
    return ParsedUrl(p.scheme, host, port, p.path,
                     p.query, p.fragment, p.params)


class HttpParser:
    """A python HTTP parser.
    """
    status = None
    status_code = None
    reason = None
    method = None
    version_minor = None
    version_major = None
    version = None
    content_length = sys.maxsize

    def __init__(self, protocol, type=ParserType.HTTP_BOTH):
        # errors vars
        self.errno = None
        self.errstr = ""
        self.type = type
        self.buf = bytearray()
        self.flags = 0
        #
        # private variables
        self._position = 0
        self._clen_rest = self.content_length
        # protocol callbacks
        self._on_header = getattr(protocol, 'on_header', passthrough)
        self._on_headers_complete = getattr(
            protocol, 'on_headers_complete', passthrough
        )
        self._on_message_begin = getattr(
            protocol, 'on_message_begin', passthrough
        )
        self._on_message_complete = getattr(
            protocol, 'on_message_complete', passthrough
        )
        self._on_body = getattr(
            protocol, 'on_body', passthrough
        )

    def get_http_version(self):
        return self.version

    def should_keep_alive(self):
        if self.version_major > 0 and self.version_minor > 0:
            if self.flags & F.CONNECTION_CLOSE.value:
                return False
        elif self.flags & F.CONNECTION_KEEP_ALIVE.value:
            return False
        return not self.http_message_needs_eof()

    def http_message_needs_eof(self):
        if self.type == ParserType.HTTP_REQUEST:
            return False

        if (self.status_code // 100 == 1 or
                self.status_code == 204 or
                self.status_code == 304 or
                self.flags & F.SKIPBODY.value):
            return False

        if (self.flags & F.CHUNKED.value or
                self.content_length != sys.maxsize):
            return False

        return True

    def is_headers_complete(self):
        """ return True if all headers have been parsed. """
        return self._position > 1

    def is_partial_body(self):
        """ return True if a chunk of body have been parsed """
        return self._partial_body

    def is_message_complete(self):
        """ return True if the parsing is done (we get EOF) """
        return self._position > 2

    def is_chunked(self):
        """ return True if Transfer-Encoding header value is chunked"""
        return self._chunked

    def feed_data(self, data):
        # end of body can be passed manually by putting a length of 0
        if not data:
            if not self.is_message_complete():
                self._position = 3
                self._on_message_complete()
            return
        #
        # start to parse
        self.buf.extend(data)
        while True:
            if not self._position:
                idx = self.buf.find(b'\r\n')
                if idx < 0:
                    break
                else:
                    self._position = 1
                    self.parse_first_line(bytes(self.buf[:idx]))
                    self.buf = self.buf[idx+2:]
            elif not self.is_headers_complete():
                if not self._parse_headers():
                    break
            elif not self.is_message_complete():
                ret = self._parse_body()
                if ret is None:
                    return
                elif ret < 0:
                    return ret
                elif ret == 0:
                    self.__on_message_complete = True
                    self._on_message_complete()
                    break

    def _parse_headers(self):
        while True:
            idx = self.buf.find(b'\r\n')
            if idx < 0:
                return False
            chunk = bytes(self.buf[:idx])
            self.buf = self.buf[idx+2:]
            if not idx:
                break

            if chunk.find(b':') < 0:
                continue
            name, value = chunk.split(b':', 1)
            value = value.lstrip()
            name = name.rstrip(b" \t").strip()
            self._on_header(name, value)
            name = name.lower()
            if name == b'connection':
                self._connection(value)
            elif name == b'content-length':
                try:
                    self.content_length = int(value)
                except ValueError:
                    continue
                if self.content_length < 0:  # ignore negative lengths
                    self.content_length = sys.maxsize
            elif name == b'transfer-encoding':
                if value.lower() == b'chunked':
                    self.flags |= F.CHUNKED.value
        #
        self._clen_rest = self.content_length
        self._position = 2
        self._on_headers_complete()

    def _connection(self, value):
        value = value.lower()
        if value == 'keep-alive':
            self.flags |= F.CONNECTION_KEEP_ALIVE.value
        elif value == 'close':
            self.flags |= F.CONNECTION_CLOSE.value
        elif value == 'upgrade':
            self.flags |= F.CONNECTION_UPGRADE.value

    def _parse_body(self):
        data = b''.join(self._buf)
        #
        if not self._chunked:
            #
            if not data and self._clen is None:
                if not self._status:    # message complete only for servers
                    self.__on_message_complete = True
                    self._on_message_complete()
            else:
                if self._clen_rest is not None:
                    self._clen_rest -= len(data)
                if data:
                    self._on_body(data)
                self._buf = []
                if self._clen_rest <= 0:
                    self.__on_message_complete = True
                    self._on_message_complete()
            return
        else:
            size, rest = self._parse_chunk_size(data)
            if size == 0:
                return size
            if size is None or len(rest) < size + 2:
                return None
            body_part, rest = rest[:size], rest[size:]

            # maybe decompress
            body_part = self._decompress(body_part)
            self._partial_body = True
            self._body.append(body_part)
            rest = rest[2:]
            self._buf = [rest] if rest else []
            return len(rest) + 2

    def _parse_chunk_size(self, data):
        idx = data.find(b'\r\n')
        if idx < 0:
            return None, None
        line, rest_chunk = data[:idx], data[idx+2:]
        chunk_size = line.split(b';', 1)[0].strip()
        try:
            chunk_size = int(chunk_size, 16)
        except ValueError:
            raise HttpParserError(
                'Invalid chunk size %s' % chunk_size) from None
        if chunk_size == 0:
            self._parse_trailers(rest_chunk)
            return 0, None
        return chunk_size, rest_chunk

    def _parse_trailers(self, data):
        idx = data.find(b'\r\n\r\n')
        if data[:2] == b'\r\n':
            self._trailers = self._parse_headers(data[:idx])

    def _decompress(self, data):
        deco = self.__decompress_obj
        if deco is not None:
            if not self.__decompress_first_try:
                data = deco.decompress(data)
            else:
                try:
                    data = deco.decompress(data)
                except zlib.error:
                    self.__decompress_obj = zlib.decompressobj(-zlib.MAX_WBITS)
                    deco = self.__decompress_obj
                    data = deco.decompress(data)
                self.__decompress_first_try = False
        return data


class HttpRequestParser(HttpParser):

    def __init__(self, protocol):
        super().__init__(protocol, ParserType.HTTP_REQUEST)
        self._on_url = getattr(protocol, 'on_url', passthrough)

    def get_method(self):
        return self.method

    def parse_first_line(self, line):
        bits = line.split(None, 2)
        if len(bits) != 3:
            raise HttpParserError(line)
        # Method
        if not METHOD_RE.match(bits[0]):
            raise HttpParserInvalidMethodError("invalid Method: %s" % bits[0])
        self.method = bits[0].upper()
        # URI
        self._on_url(bits[1])
        # Version
        matchv = VERSION_RE.match(bits[2])
        if matchv is None:
            raise HttpParserError(
                "Invalid HTTP version: %s" % bits[2]
            )
        self.version_major = int(matchv.group(1))
        self.version_minor = int(matchv.group(2))
        self.version = '%s.%s' % (self.version_major, self.version_minor)


class HttpResponseParser(HttpParser):

    def __init__(self, protocol):
        super().__init__(protocol, ParserType.HTTP_RESPONSE)
        self._on_status = getattr(protocol, 'on_status', passthrough)

    def get_status_code(self):
        return self.status_code

    def parse_first_line(self, line):
        bits = line.split(None, 1)
        if len(bits) != 2:
            raise HttpParserError(line)

        # version
        matchv = VERSION_RE.match(bits[0])
        if matchv is None:
            raise HttpParserError("Invalid HTTP version: %s" % bits[0])
        # status
        matchs = STATUS_RE.match(bits[1])
        if matchs is None:
            raise HttpParserInvalidStatusError("Invalid status %" % bits[1])

        self._on_status(bits[1])
        self.version_major = int(matchv.group(1))
        self.version_minor = int(matchv.group(2))
        self.version = '%s.%s' % (self.version_major, self.version_minor)
        self.status = bits[1]
        self.status_code = int(matchs.group(1))
        self.reason = matchs.group(2)
