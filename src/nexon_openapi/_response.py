from __future__ import annotations

import inspect
import logging
import datetime
from typing import TYPE_CHECKING, Union, Generic, TypeVar, cast
from typing_extensions import ParamSpec, override, get_origin

import httpx
import pydantic

from ._types import NoneType, UnknownResponse, BinaryResponseContent, ResponseT, ModelBuilderProtocol
from .utils import is_given
from ._models import BaseModel, is_basemodel, validate_type, construct_type
from ._exceptions import APIResponseValidationError

if TYPE_CHECKING:
    from ._models import FinalRequestOptions


P = ParamSpec("P")
R = TypeVar("R")

log: logging.Logger = logging.getLogger(__name__)


class APIResponse(Generic[R]):
    _cast_to: type[R]
    _parsed: Union[R, None]
    _strict_response_validation: bool
    _options: FinalRequestOptions

    http_response: httpx.Response

    def __init__(
        self,
        raw: httpx.Response,
        cast_to: type[R],
        strict_response_validation: bool,
        options: FinalRequestOptions,
    ) -> None:
        self._cast_to = cast_to
        self._parsed = None
        self._strict_response_validation = strict_response_validation
        self._options = options
        self.http_response = raw

    def parse(self) -> R:
        if self._parsed is not None:
            return self._parsed

        parsed = self._parse()
        if is_given(self._options.post_parser):
            parsed = self._options.post_parser(parsed)

        self._parsed = parsed
        return parsed

    @property
    def headers(self) -> httpx.Headers:
        return self.http_response.headers

    @property
    def http_request(self) -> httpx.Request:
        return self.http_response.request

    @property
    def status_code(self) -> int:
        return self.http_response.status_code

    @property
    def url(self) -> httpx.URL:
        return self.http_response.url

    @property
    def method(self) -> str:
        return self.http_request.method

    @property
    def content(self) -> bytes:
        return self.http_response.content

    @property
    def text(self) -> str:
        return self.http_response.text

    @property
    def http_version(self) -> str:
        return self.http_response.http_version

    @property
    def elapsed(self) -> datetime.timedelta:
        """The time taken for the complete request/response cycle to complete."""
        return self.http_response.elapsed

    def _parse(self) -> R:
        cast_to = self._cast_to
        if cast_to is NoneType:
            return cast(R, None)

        response = self.http_response
        if cast_to == str:
            return cast(R, response.text)

        origin = get_origin(cast_to) or cast_to

        if inspect.isclass(origin) and issubclass(origin, BinaryResponseContent):
            return cast(R, cast_to(response))  # type: ignore

        if origin == APIResponse:
            raise RuntimeError("Unexpected state - cast_to is `APIResponse`")

        if inspect.isclass(origin) and issubclass(origin, httpx.Response):
            # Because of the invariance of our ResponseT TypeVar, users can subclass httpx.Response
            # and pass that class to our request functions. We cannot change the variance to be either
            # covariant or contravariant as that makes our usage of ResponseT illegal. We could construct
            # the response class ourselves but that is something that should be supported directly in httpx
            # as it would be easy to incorrectly construct the Response object due to the multitude of arguments.
            if cast_to != httpx.Response:
                raise ValueError(f"Subclasses of httpx.Response cannot be passed to `cast_to`")
            return cast(R, response)

        # The check here is necessary as we are subverting the the type system
        # with casts as the relationship between TypeVars and Types are very strict
        # which means we must return *exactly* what was input or transform it in a
        # way that retains the TypeVar state. As we cannot do that in this function
        # then we have to resort to using `cast`. At the time of writing, we know this
        # to be safe as we have handled all the types that could be bound to the
        # `ResponseT` TypeVar, however if that TypeVar is ever updated in the future, then
        # this function would become unsafe but a type checker would not report an error.
        if (
            cast_to is not UnknownResponse
            and not origin is list
            and not origin is dict
            and not origin is Union
            and not issubclass(origin, BaseModel)
        ):
            raise RuntimeError(
                f"Invalid state, expected {cast_to} to be a subclass type of {BaseModel}, {dict}, {list} or {Union}."
            )

        # split is required to handle cases where additional information is included
        # in the response, e.g. application/json; charset=utf-8
        content_type, *_ = response.headers.get("content-type").split(";")
        if content_type != "application/json":
            if is_basemodel(cast_to):
                try:
                    data = response.json()
                except Exception as exc:
                    log.debug(
                        "Could not read JSON from response data due to %s - %s",
                        type(exc),
                        exc,
                    )
                else:
                    return self._process_response_data(
                        data=data,
                        cast_to=cast_to,  # type: ignore
                        response=response,
                    )

            if self._strict_response_validation:
                raise APIResponseValidationError(
                    response=response,
                    message=f"Expected Content-Type response header to be `application/json` but received `{content_type}` instead.",
                    body=response.text,
                )

            # If the API responds with content that isn't JSON then we just return
            # the (decoded) text without performing any parsing so that you can still
            # handle the response however you need to.
            return response.text  # type: ignore

        data = response.json()

        return self._process_response_data(
            data=data,
            cast_to=cast_to,  # type: ignore
            response=response,
        )

    def _process_response_data(
        self,
        *,
        data: object,
        cast_to: type[ResponseT],
        response: httpx.Response,
    ) -> ResponseT:
        if data is None:
            return cast(ResponseT, None)

        if cast_to is UnknownResponse:
            return cast(ResponseT, data)

        try:
            if inspect.isclass(cast_to) and issubclass(cast_to, ModelBuilderProtocol):
                return cast(ResponseT, cast_to.build(response=response, data=data))

            if self._strict_response_validation:
                return cast(ResponseT, validate_type(type_=cast_to, value=data))

            return cast(ResponseT, construct_type(type_=cast_to, value=data))
        except pydantic.ValidationError as err:
            raise APIResponseValidationError(response=response, body=data) from err

    @override
    def __repr__(self) -> str:
        return f"<APIResponse [{self.status_code} {self.http_response.reason_phrase}] type={self._cast_to}>"
