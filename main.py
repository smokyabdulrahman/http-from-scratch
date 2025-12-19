import asyncio
import json
from asyncio import StreamReader, StreamWriter
from dataclasses import dataclass
from enum import StrEnum, Enum
from typing import Any, Optional
import argparse

parser = argparse.ArgumentParser("Smoky HTTP/1.1 Server")
parser.add_argument(
    "-p", "--port", help="the port to run the server on", type=int, default=9999
)
parser.add_argument(
    "--host", help="the host to run the server on", type=str, default="127.0.0.1"
)
args = parser.parse_args()


ENCODING = "utf8"


class HTTPStatusCode(Enum):
    # 2xx
    HTTP_200_OK = 200, "OK"
    # 4xx
    HTTP_400_BAD_REQUEST = 400, "Bad Request"
    HTTP_413_ENTITY_TOO_LARGE = 413, "Entity Too Large"
    # 5xx
    HTTP_500_INTERNAL_SERVER_ERROR = 500, "Internal Server Error"

    def __new__(cls, *args, **_: dict[int, Any]) -> "HTTPStatusCode":
        obj = object.__new__(cls)
        obj._value_ = args[0]
        return obj

    # ignore the first param since it's already set by __new__
    def __init__(self, _value: int, message: str) -> None:
        self._message = message

    def __str__(self) -> str:
        return f"{self.value} {self._message}"

    @property
    def message(self) -> str:
        return self._message


class HTTPError(Exception):
    def __init__(
        self,
        http_status_code: HTTPStatusCode = HTTPStatusCode.HTTP_500_INTERNAL_SERVER_ERROR,
        message: str | None = None,
    ) -> None:
        super().__init__(http_status_code.message)
        self.http_status_code = http_status_code
        self.message = message

    def __str__(self) -> str:
        return str(self.http_status_code)


class HTTPMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    OPTIONS = "OPTIONS"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


@dataclass
class HTTPRequestLine:
    method: HTTPMethod
    version: float
    path: str


async def read_body(reader: StreamReader, body_length: int) -> bytes | None:
    if body_length <= 0:
        return None

    return await reader.readexactly(body_length)


async def read_headers(reader: StreamReader) -> dict[str, str]:
    has_host_header = False
    headers: dict[str, str] = {}
    field_line = await reader.readuntil(b"\r\n")
    while field_line != b"\r\n":
        field_line_parts = field_line.split(b":", 1)
        if len(field_line_parts) != 2:
            raise HTTPError(
                http_status_code=HTTPStatusCode.HTTP_400_BAD_REQUEST,
                message="header doesn't have the format "
                '(field-name ":" OWS field-value OWS) '
                f"- {field_line}",
            )

        try:
            header_name = field_line_parts[0].decode(ENCODING).strip().lower()
            if len(header_name) == 0:
                raise HTTPError(http_status_code=HTTPStatusCode.HTTP_400_BAD_REQUEST)
        except Exception:
            raise HTTPError(http_status_code=HTTPStatusCode.HTTP_400_BAD_REQUEST)

        try:
            header_value = field_line_parts[1].decode(ENCODING).strip().lower()
            if len(header_value) == 0:
                raise HTTPError(http_status_code=HTTPStatusCode.HTTP_400_BAD_REQUEST)
        except Exception:
            raise HTTPError(http_status_code=HTTPStatusCode.HTTP_400_BAD_REQUEST)

        if header_name == "host":
            # only one host header is allowed
            if has_host_header:
                raise HTTPError(
                    http_status_code=HTTPStatusCode.HTTP_400_BAD_REQUEST,
                    message="Request can have one 'host' header. "
                    "Multiple 'host' headers sent.",
                )

            has_host_header = True

        headers[header_name] = header_value

        # loop
        field_line = await reader.readuntil(b"\r\n")

    if not has_host_header:
        raise HTTPError(
            http_status_code=HTTPStatusCode.HTTP_413_ENTITY_TOO_LARGE,
            message="Request doesn't have 'host' header.",
        )
    return headers


async def read_request_line(reader: StreamReader) -> HTTPRequestLine:
    request_line = await reader.readuntil(b"\r\n")
    request_line_parts = request_line.split(b" ")
    if len(request_line_parts) != 3:
        raise HTTPError(http_status_code=HTTPStatusCode.HTTP_400_BAD_REQUEST)

    try:
        method = HTTPMethod(request_line_parts[0].decode(ENCODING))
    except ValueError:
        raise HTTPError(http_status_code=HTTPStatusCode.HTTP_400_BAD_REQUEST)

    if request_line_parts[2] != b"HTTP/1.1\r\n":
        raise HTTPError(http_status_code=HTTPStatusCode.HTTP_400_BAD_REQUEST)

    return HTTPRequestLine(
        method=method,
        version=1.1,
        path=request_line_parts[1].decode(ENCODING),
    )


async def send_response(
    writer: StreamWriter, http_status_code: HTTPStatusCode, body: Optional[bytes] = None
):
    """Helper to send a well-formed HTTP response."""
    response = (
        f"HTTP/1.1 {http_status_code}\r\n"
        f"Content-Type: text/plain\r\n"
        f"Content-Length: {len(body) if body else 0}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode()

    if body:
        response = response + body

    writer.write(response)
    await writer.drain()


async def handler(reader: StreamReader, writer: StreamWriter) -> None:
    try:
        request_line = await read_request_line(reader)
        headers = await read_headers(reader)
        body_length = headers.get("content-length", "0")

        if not body_length.isdigit:
            raise HTTPError(
                http_status_code=HTTPStatusCode.HTTP_400_BAD_REQUEST,
                message=f"Content-Length header must have 'int' as value. Value of '{body_length}' is invalid.",
            )

        body = await read_body(reader, int(body_length))

        response_body = {
            "method": request_line.method,
            "version": request_line.version,
            "path": request_line.path,
            "body": body.decode(ENCODING) if body else "$NONE",
        }

        response_body_json_str = json.dumps(response_body)
        response_body_json_bytes = response_body_json_str.encode(ENCODING)

        response = (
            f"HTTP/1.1 {HTTPStatusCode.HTTP_200_OK}\r\n"
            "content-type: text/plain\r\n"
            f"content-length: {len(response_body_json_bytes)}\r\n"
            "\r\n"
        )
        await send_response(
            writer,
            HTTPStatusCode.HTTP_200_OK,
            response.encode(ENCODING) + response_body_json_bytes,
        )
    except asyncio.IncompleteReadError:
        raise HTTPError(http_status_code=HTTPStatusCode.HTTP_413_ENTITY_TOO_LARGE)
    except HTTPError as e:
        print(f"{e} - {e.message}")
        await send_response(writer, e.http_status_code)
    finally:
        writer.close()
        await writer.wait_closed()


async def main() -> None:
    server = await asyncio.start_server(handler, args.host, args.port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
