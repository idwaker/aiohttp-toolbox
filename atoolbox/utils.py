import json
import re
from typing import Any, Tuple, Type, TypeVar

from aiohttp.web import Response
from aiohttp.web_exceptions import HTTPClientError
from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ValidationError, validate_model

from .json_tools import pretty_lenient_json

JSON_CONTENT_TYPE = 'application/json'
IP_HEADER = 'X-Forwarded-For'
PROTO_HEADER = 'X-Forwarded-Proto'
URI_NOT_ALLOWED = re.compile(r'[^a-zA-Z0-9_\-/.]')
REMOVE_PORT = re.compile(r':\d{2,}$')
PydanticModel = TypeVar('PydanticModel', bound=BaseModel)

__all__ = (
    'raw_json_response',
    'json_response',
    'parse_request',
    'parse_request_ignore_missing',
    'get_ip',
    'request_root',
    'JsonErrors',
    'encrypt_json',
    'decrypt_json',
    'get_offset',
    'slugify',
    'remove_port',
    'RequestError',
)


def raw_json_response(json_str, status_=200):
    return Response(body=json_str.encode() + b'\n', status=status_, content_type=JSON_CONTENT_TYPE)


def json_response(*, status_=200, list_=None, headers_=None, **data):
    return Response(
        body=json.dumps(data if list_ is None else list_).encode(),
        status=status_,
        content_type=JSON_CONTENT_TYPE,
        headers=headers_,
    )


async def parse_request(request, model: Type[PydanticModel], *, headers=None) -> PydanticModel:
    error_details = None
    try:
        data = await request.json()
    except ValueError:
        error_msg = 'Invalid JSON'
    else:
        try:
            return model.parse_obj(data)
        except ValidationError as e:
            error_msg = 'Invalid Data'
            error_details = e.errors()

    raise JsonErrors.HTTPBadRequest(message=error_msg, details=error_details, headers=headers)


async def parse_request_ignore_missing(
    request, model: Type[PydanticModel], *, headers=None
) -> Tuple[PydanticModel, dict]:
    try:
        raw_data = await request.json()
    except ValueError:
        raise JsonErrors.HTTPBadRequest(message='Invalid JSON', headers=headers)
    if not isinstance(raw_data, dict):
        raise JsonErrors.HTTPBadRequest(message='data not a dictionary', headers=headers)

    data, e = validate_model(model, raw_data, raise_exc=False)
    if e:
        errors = [e for e in e.errors() if not (e['type'] == 'value_error.missing' and len(e['loc']) == 1)]
        if errors:
            raise JsonErrors.HTTPBadRequest(message='Invalid Data', details=errors, headers=headers)

    return model.construct(**data), raw_data


def get_ip(request):
    ips = request.headers.get(IP_HEADER)
    if ips:
        return ips.split(',', 1)[0].strip(' ')
    else:
        return request.remote


def request_root(request):
    # request.url.scheme doesn't work as https is already terminated
    scheme = request.headers.get(PROTO_HEADER) or 'http'
    return f'{scheme}://{request.host}'


class JsonErrors:
    class _HTTPClientErrorJson(HTTPClientError):
        def __init__(self, message, *, details=None, headers=None):
            data = {'message': message}
            if details:
                data['details'] = details
            super().__init__(text=pretty_lenient_json(data), content_type=JSON_CONTENT_TYPE, headers=headers)

    class HTTPBadRequest(_HTTPClientErrorJson):
        status_code = 400

    class HTTPUnauthorized(_HTTPClientErrorJson):
        status_code = 401

    class HTTPPaymentRequired(_HTTPClientErrorJson):
        status_code = 402

    class HTTPForbidden(_HTTPClientErrorJson):
        status_code = 403

    class HTTPNotFound(_HTTPClientErrorJson):
        status_code = 404

    class HTTPConflict(_HTTPClientErrorJson):
        status_code = 409

    class HTTP470(_HTTPClientErrorJson):
        status_code = 470


def encrypt_json(app, data: Any) -> str:
    return app['auth_fernet'].encrypt(json.dumps(data).encode()).decode()


def decrypt_json(app, token: bytes, *, ttl: int = None, headers=None) -> Any:
    try:
        return json.loads(app['auth_fernet'].decrypt(token, ttl=ttl).decode())
    except InvalidToken:
        raise JsonErrors.HTTPBadRequest(message='invalid token', headers=headers)


def get_offset(request, paginate_by=100):
    page = request.query.get('page')
    if not page:
        return 0

    try:
        p = int(page)
        if p < 1:
            raise ValueError()
    except ValueError:
        raise JsonErrors.HTTPBadRequest(f"invalid page '{page}'")
    else:
        return (p - 1) * paginate_by


def slugify(title):
    name = title.replace(' ', '-').lower()
    name = URI_NOT_ALLOWED.sub('', name)
    name = re.sub('-{2,}', '-', name)
    return name.strip('_-')


def remove_port(host):
    return REMOVE_PORT.sub('', host)


class RequestError(RuntimeError):
    def __init__(self, status, url, *, text: str = None):
        self.status = status
        self.url = url
        self.text = text

    def __str__(self):
        return f'response {self.status} from "{self.url}"' + (f':\n{self.text[:400]}' if self.text else '')

    def json(self):
        return json.loads(self.text)

    def extra(self):
        return self.text
