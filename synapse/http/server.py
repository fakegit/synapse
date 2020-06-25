# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2018 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import abc
import collections
import html
import logging
import types
import urllib
from http import HTTPStatus
from io import BytesIO
from typing import Any, Awaitable, Tuple, TypeVar, Union

import jinja2
from canonicaljson import encode_canonical_json, encode_pretty_printed_json, json

from twisted.internet import defer
from twisted.python import failure
from twisted.web import resource
from twisted.web.server import NOT_DONE_YET, Request
from twisted.web.static import NoRangeStaticProducer
from twisted.web.util import redirectTo

import synapse.events
import synapse.metrics
from synapse.api.errors import (
    CodeMessageException,
    Codes,
    RedirectException,
    SynapseError,
    UnrecognizedRequestError,
)
from synapse.http.site import SynapseRequest
from synapse.logging.context import preserve_fn
from synapse.logging.opentracing import trace_servlet
from synapse.util.caches import intern_dict

logger = logging.getLogger(__name__)

HTML_ERROR_TEMPLATE = """<!DOCTYPE html>
<html lang=en>
  <head>
    <meta charset="utf-8">
    <title>Error {code}</title>
  </head>
  <body>
     <p>{msg}</p>
  </body>
</html>
"""

T = TypeVar("T")

# A tuple of HTTP response code and response body. For JSON requests the body is
# anything JSON serializable, while for HTML requests they are bytes.
ResponseTuple = Tuple[int, Any]

# Used for functions that may or may not return an awaitable object.
MaybeAwaitable = Union[Awaitable[T], T]


def return_json_error(f: failure.Failure, request: Request) -> None:
    """Sends a JSON error response to clients.
    """

    if f.check(SynapseError):
        error_code = f.value.code
        error_dict = f.value.error_dict()

        logger.info("%s SynapseError: %s - %s", request, error_code, f.value.msg)
    else:
        error_code = 500
        error_dict = {"error": "Internal server error", "errcode": Codes.UNKNOWN}

        logger.error(
            "Failed handle request via %r: %r",
            request.request_metrics.name,
            request,
            exc_info=(f.type, f.value, f.getTracebackObject()),
        )

    if request.startedWriting:
        if request.transport:
            try:
                request.transport.abortConnection()
            except Exception:
                # abortConnection throws if the connection is already closed
                pass
    else:
        respond_with_json(
            request,
            error_code,
            error_dict,
            send_cors=True,
            pretty_print=_request_user_agent_is_curl(request),
        )


TV = TypeVar("TV")


def return_html_error(
    f: failure.Failure, request: Request, error_template: Union[str, jinja2.Template],
) -> None:
    """Sends an HTML error page corresponding to the given failure.

    Handles RedirectException and other CodeMessageExceptions (such as SynapseError)

    Args:
        f: the error to report
        request: the failing request
        error_template: the HTML template. Can be either a string (with `{code}`,
            `{msg}` placeholders), or a jinja2 template
    """
    if f.check(CodeMessageException):
        cme = f.value
        code = cme.code
        msg = cme.msg

        if isinstance(cme, RedirectException):
            logger.info("%s redirect to %s", request, cme.location)
            request.setHeader(b"location", cme.location)
            request.cookies.extend(cme.cookies)
        elif isinstance(cme, SynapseError):
            logger.info("%s SynapseError: %s - %s", request, code, msg)
        else:
            logger.error(
                "Failed handle request %r",
                request,
                exc_info=(f.type, f.value, f.getTracebackObject()),
            )
    else:
        code = HTTPStatus.INTERNAL_SERVER_ERROR
        msg = "Internal server error"

        logger.error(
            "Failed handle request %r",
            request,
            exc_info=(f.type, f.value, f.getTracebackObject()),
        )

    if isinstance(error_template, str):
        body = error_template.format(code=code, msg=html.escape(msg))
    else:
        body = error_template.render(code=code, msg=msg)

    body_bytes = body.encode("utf-8")
    request.setResponseCode(code)
    request.setHeader(b"Content-Type", b"text/html; charset=utf-8")
    request.setHeader(b"Content-Length", b"%i" % (len(body_bytes),))
    request.write(body_bytes)
    finish_request(request)


def wrap_async_request_handler(h):
    """Wraps an async request handler so that it calls request.processing.

    This helps ensure that work done by the request handler after the request is completed
    is correctly recorded against the request metrics/logs.

    The handler method must have a signature of "handle_foo(self, request)",
    where "request" must be a SynapseRequest.

    The handler may return a deferred, in which case the completion of the request isn't
    logged until the deferred completes.
    """

    async def wrapped_async_request_handler(self, request):
        with request.processing():
            await h(self, request)

    # we need to preserve_fn here, because the synchronous render method won't yield for
    # us (obviously)
    return preserve_fn(wrapped_async_request_handler)


class HttpServer(object):
    """ Interface for registering callbacks on a HTTP server
    """

    def register_paths(self, method, path_patterns, callback):
        """ Register a callback that gets fired if we receive a http request
        with the given method for a path that matches the given regex.

        If the regex contains groups these gets passed to the calback via
        an unpacked tuple.

        Args:
            method (str): The method to listen to.
            path_patterns (list<SRE_Pattern>): The regex used to match requests.
            callback (function): The function to fire if we receive a matched
                request. The first argument will be the request object and
                subsequent arguments will be any matched groups from the regex.
                This should return a tuple of (code, response).
        """
        pass


class _AsyncResource(resource.Resource, metaclass=abc.ABCMeta):
    """Base class for resources that have async handlers.

    Args:
        extract_context: Whether to attempt to extract the opentracing
            context from the request the servlet is handling.
    """

    def __init__(self, extract_context=False):
        super().__init__()

        self._extract_context = extract_context

    def render(self, request):
        """ This gets called by twisted every time someone sends us a request.
        """
        defer.ensureDeferred(self._async_render_wrapper(request))
        return NOT_DONE_YET

    @wrap_async_request_handler
    async def _async_render_wrapper(self, request):
        """This is a wrapper that delegates to `_async_render`,
        """
        try:
            request.request_metrics.name = self.__class__.__name__

            with trace_servlet(request, self._extract_context):
                callback_return = await self._async_render(request)

                if callback_return is not None:
                    code, response = callback_return
                    self._send_response(request, code, response)
        except Exception:
            f = failure.Failure()
            self._send_error_response(f, request)

    async def _async_render(self, request):
        method_handler = getattr(
            self, "_async_render_%s" % (request.method.decode("ascii"),), None
        )
        if method_handler:
            raw_callback_return = method_handler(request)

            # Is it synchronous? We'll allow this for now.
            if isinstance(raw_callback_return, (defer.Deferred, types.CoroutineType)):
                callback_return = await raw_callback_return
            else:
                callback_return = raw_callback_return

            return callback_return

        _unrecognised_request_handler(request)

    @abc.abstractmethod
    def _send_response(
        self, request: SynapseRequest, code: int, response_object: Any,
    ) -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    def _send_error_response(
        self, f: failure.Failure, request: SynapseRequest,
    ) -> None:
        raise NotImplementedError()


class DirectServeJsonResource(_AsyncResource):
    """A resource that will call `self._async_on_<METHOD>` on new requests,
    formatting responses and errors as JSON.
    """

    def _send_response(
        self, request, code, response_object,
    ):
        """Implements _AsyncResource._send_response
        """
        # TODO: Only enable CORS for the requests that need it.
        respond_with_json(
            request,
            code,
            response_object,
            send_cors=True,
            pretty_print=_request_user_agent_is_curl(request),
            canonical_json=self.canonical_json,
        )

    def _send_error_response(
        self, f: failure.Failure, request: SynapseRequest,
    ) -> None:
        """Implements _AsyncResource._send_error_response
        """
        return_json_error(f, request)


class JsonResource(DirectServeJsonResource):
    """ This implements the HttpServer interface and provides JSON support for
    Resources.

    Register callbacks via register_paths()

    Callbacks can return a tuple of status code and a dict in which case the
    the dict will automatically be sent to the client as a JSON object.

    The JsonResource is primarily intended for returning JSON, but callbacks
    may send something other than JSON, they may do so by using the methods
    on the request object and instead returning None.
    """

    isLeaf = True

    _PathEntry = collections.namedtuple(
        "_PathEntry", ["pattern", "callback", "servlet_classname"]
    )

    def __init__(self, hs, canonical_json=True, extract_context=False):
        super().__init__(extract_context)

        self.canonical_json = canonical_json
        self.clock = hs.get_clock()
        self.path_regexs = {}
        self.hs = hs

    def register_paths(self, method, path_patterns, callback, servlet_classname):
        """
        Registers a request handler against a regular expression. Later request URLs are
        checked against these regular expressions in order to identify an appropriate
        handler for that request.

        Args:
            method (str): GET, POST etc

            path_patterns (Iterable[str]): A list of regular expressions to which
                the request URLs are compared.

            callback (function): The handler for the request. Usually a Servlet

            servlet_classname (str): The name of the handler to be used in prometheus
                and opentracing logs.
        """
        method = method.encode("utf-8")  # method is bytes on py3

        for path_pattern in path_patterns:
            logger.debug("Registering for %s %s", method, path_pattern.pattern)
            self.path_regexs.setdefault(method, []).append(
                self._PathEntry(path_pattern, callback, servlet_classname)
            )

    def _get_handler_for_request(self, request: SynapseRequest):
        """Implements _AsyncResource._get_handler_for_request
        """
        request_path = request.path.decode("ascii")

        # Loop through all the registered callbacks to check if the method
        # and path regex match
        for path_entry in self.path_regexs.get(request.method, []):
            m = path_entry.pattern.match(request_path)
            if m:
                # We found a match!
                return path_entry.callback, path_entry.servlet_classname, m.groupdict()

        # Huh. No one wanted to handle that? Fiiiiiine. Send 400.
        return _unrecognised_request_handler, "unrecognised_request_handler", {}

    async def _async_render(self, request):
        callback, servlet_classname, group_dict = self._get_handler_for_request(request)

        request.request_metrics.name = servlet_classname

        # Now trigger the callback. If it returns a response, we send it
        # here. If it throws an exception, that is handled by the wrapper
        # installed by @request_handler.
        kwargs = intern_dict(
            {
                name: urllib.parse.unquote(value) if value else value
                for name, value in group_dict.items()
            }
        )

        raw_callback_return = callback(request, **kwargs)

        # Is it synchronous? We'll allow this for now.
        if isinstance(raw_callback_return, (defer.Deferred, types.CoroutineType)):
            callback_return = await raw_callback_return
        else:
            callback_return = raw_callback_return

        return callback_return


class DirectServeHtmlResource(_AsyncResource):
    """A resource that will call `self._async_on_<METHOD>` on new requests,
    formatting responses and errors as HTML.
    """

    # The error template to use for this resource
    ERROR_TEMPLATE = HTML_ERROR_TEMPLATE

    def _send_response(
        self, request: SynapseRequest, code: int, response_object: Any,
    ):
        """Implements _AsyncResource._send_response
        """
        # We expect to get bytes for us to write
        assert isinstance(response_object, bytes)
        html_bytes = response_object

        request.setResponseCode(200)
        request.setHeader(b"Content-Type", b"text/html; charset=utf-8")
        request.setHeader(b"Content-Length", b"%d" % (len(html_bytes),))
        request.write(html_bytes)
        finish_request(request)

    def _send_error_response(
        self, f: failure.Failure, request: SynapseRequest,
    ) -> None:
        """Implements _AsyncResource._send_error_response
        """
        return_html_error(f, request, self.ERROR_TEMPLATE)


def _options_handler(request):
    """Request handler for OPTIONS requests

    This is a request handler suitable for return from
    _get_handler_for_request. It returns a 200 and an empty body.

    Args:
        request (twisted.web.http.Request):

    Returns:
        Tuple[int, dict]: http code, response body.
    """
    return 200, {}


def _unrecognised_request_handler(request):
    """Request handler for unrecognised requests

    This is a request handler suitable for return from
    _get_handler_for_request. It actually just raises an
    UnrecognizedRequestError.

    Args:
        request (twisted.web.http.Request):
    """
    raise UnrecognizedRequestError()


class RootRedirect(resource.Resource):
    """Redirects the root '/' path to another path."""

    def __init__(self, path):
        resource.Resource.__init__(self)
        self.url = path

    def render_GET(self, request):
        return redirectTo(self.url.encode("ascii"), request)

    def getChild(self, name, request):
        if len(name) == 0:
            return self  # select ourselves as the child to render
        return resource.Resource.getChild(self, name, request)


class OptionsResource(resource.Resource):
    """Responds to OPTION requests for itself and all children."""

    def render_OPTIONS(self, request):
        code, response_json_object = _options_handler(request)

        return respond_with_json(
            request, code, response_json_object, send_cors=True, canonical_json=False,
        )

    def getChildWithDefault(self, path, request):
        if request.method == b"OPTIONS":
            return self  # select ourselves as the child to render
        return resource.Resource.getChildWithDefault(self, path, request)


class RootOptionsRedirectResource(OptionsResource, RootRedirect):
    pass


def respond_with_json(
    request,
    code,
    json_object,
    send_cors=False,
    response_code_message=None,
    pretty_print=False,
    canonical_json=True,
):
    # could alternatively use request.notifyFinish() and flip a flag when
    # the Deferred fires, but since the flag is RIGHT THERE it seems like
    # a waste.
    if request._disconnected:
        logger.warning(
            "Not sending response to request %s, already disconnected.", request
        )
        return

    if pretty_print:
        json_bytes = encode_pretty_printed_json(json_object) + b"\n"
    else:
        if canonical_json or synapse.events.USE_FROZEN_DICTS:
            # canonicaljson already encodes to bytes
            json_bytes = encode_canonical_json(json_object)
        else:
            json_bytes = json.dumps(json_object).encode("utf-8")

    return respond_with_json_bytes(
        request,
        code,
        json_bytes,
        send_cors=send_cors,
        response_code_message=response_code_message,
    )


def respond_with_json_bytes(
    request, code, json_bytes, send_cors=False, response_code_message=None
):
    """Sends encoded JSON in response to the given request.

    Args:
        request (twisted.web.http.Request): The http request to respond to.
        code (int): The HTTP response code.
        json_bytes (bytes): The json bytes to use as the response body.
        send_cors (bool): Whether to send Cross-Origin Resource Sharing headers
            http://www.w3.org/TR/cors/
    Returns:
        twisted.web.server.NOT_DONE_YET"""

    request.setResponseCode(code, message=response_code_message)
    request.setHeader(b"Content-Type", b"application/json")
    request.setHeader(b"Content-Length", b"%d" % (len(json_bytes),))
    request.setHeader(b"Cache-Control", b"no-cache, no-store, must-revalidate")

    if send_cors:
        set_cors_headers(request)

    # todo: we can almost certainly avoid this copy and encode the json straight into
    # the bytesIO, but it would involve faffing around with string->bytes wrappers.
    bytes_io = BytesIO(json_bytes)

    producer = NoRangeStaticProducer(request, bytes_io)
    producer.start()
    return NOT_DONE_YET


def set_cors_headers(request):
    """Set the CORs headers so that javascript running in a web browsers can
    use this API

    Args:
        request (twisted.web.http.Request): The http request to add CORs to.
    """
    request.setHeader(b"Access-Control-Allow-Origin", b"*")
    request.setHeader(
        b"Access-Control-Allow-Methods", b"GET, POST, PUT, DELETE, OPTIONS"
    )
    request.setHeader(
        b"Access-Control-Allow-Headers",
        b"Origin, X-Requested-With, Content-Type, Accept, Authorization",
    )


def finish_request(request):
    """ Finish writing the response to the request.

    Twisted throws a RuntimeException if the connection closed before the
    response was written but doesn't provide a convenient or reliable way to
    determine if the connection was closed. So we catch and log the RuntimeException

    You might think that ``request.notifyFinish`` could be used to tell if the
    request was finished. However the deferred it returns won't fire if the
    connection was already closed, meaning we'd have to have called the method
    right at the start of the request. By the time we want to write the response
    it will already be too late.
    """
    try:
        request.finish()
    except RuntimeError as e:
        logger.info("Connection disconnected before response was written: %r", e)


def _request_user_agent_is_curl(request):
    user_agents = request.requestHeaders.getRawHeaders(b"User-Agent", default=[])
    for user_agent in user_agents:
        if b"curl" in user_agent:
            return True
    return False
