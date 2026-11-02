"""
火山引擎 OpenAPI V4 签名。

优先使用官方 volcengine SDK 的 SignerV4（github.com/volcengine/volc-sdk-python），
找不到 SDK 时 fallback 到本地手写实现（已知存在边角问题，仅作占位）。

用法：
    headers = sign_request(
        method='POST',
        url='https://rtc.volcengineapi.com/?Action=StartVoiceChat&Version=2024-12-01',
        body=b'{...}',
        ak=Settings.VOLC_AK,
        sk=Settings.VOLC_SK,
        service='rtc',
        region='cn-north-1',
    )
    requests.post(url, data=body, headers=headers)
"""
import datetime
import hashlib
import hmac
from urllib.parse import urlparse, parse_qsl, quote


def _sign_with_official_sdk(method, url, body, ak, sk, service, region,
                            extra_headers):
    from volcengine.auth.SignerV4 import SignerV4
    from volcengine.base.Request import Request
    from volcengine.Credentials import Credentials

    parsed = urlparse(url)
    req = Request()
    req.set_schema(parsed.scheme)
    req.set_method(method.upper())
    req.set_host(parsed.netloc)
    req.set_path(parsed.path or '/')
    if parsed.query:
        from collections import OrderedDict
        q = OrderedDict()
        for k, v in parse_qsl(parsed.query, keep_blank_values=True):
            q[k] = v
        req.set_query(q)
    if body:
        req.set_body(body if isinstance(body, (bytes, bytearray)) else
                     body.encode('utf-8'))
    if extra_headers:
        for k, v in extra_headers.items():
            req.headers[k] = v
    req.headers.setdefault('Content-Type', 'application/json')

    creds = Credentials(ak, sk, service, region)
    SignerV4.sign(req, creds)
    return dict(req.headers)


# ---- 以下为本地手写实现（已知有边角问题，仅作 SDK 不可用时的占位） ----

def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()


def _canonical_query(query: str) -> str:
    if not query:
        return ''
    pairs = parse_qsl(query, keep_blank_values=True)
    pairs.sort(key=lambda kv: kv[0])
    return '&'.join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
        for k, v in pairs
    )


def _select_signed_headers(headers: dict) -> list[str]:
    out = []
    for k in headers.keys():
        kl = k.lower()
        if kl in ('content-type', 'content-md5', 'host') or kl.startswith('x-'):
            out.append(kl)
    out.sort()
    return out


def _sign_local_fallback(method, url, body, ak, sk, service, region,
                         extra_headers):
    parsed = urlparse(url)
    host = parsed.netloc
    if host.endswith(':80') or host.endswith(':443'):
        host = host.rsplit(':', 1)[0]
    path = parsed.path or '/'
    query = parsed.query or ''
    body = body or b''

    now = datetime.datetime.utcnow()
    x_date = now.strftime('%Y%m%dT%H%M%SZ')
    short_date = now.strftime('%Y%m%d')
    body_hash = _sha256_hex(body)

    headers = {
        'Host': host,
        'X-Date': x_date,
        'X-Content-Sha256': body_hash,
        'Content-Type': 'application/json',
    }
    if extra_headers:
        headers.update(extra_headers)

    headers_l = {k.lower(): v.strip() for k, v in headers.items()}
    signed_list = _select_signed_headers(headers)
    canonical_headers = ''.join(f"{n}:{headers_l[n]}\n" for n in signed_list)
    signed_headers = ';'.join(signed_list)
    canonical = '\n'.join([
        method.upper(),
        path,
        _canonical_query(query),
        canonical_headers,
        signed_headers,
        body_hash,
    ])
    canonical_hash = _sha256_hex(canonical.encode('utf-8'))
    credential_scope = f"{short_date}/{region}/{service}/request"
    string_to_sign = '\n'.join([
        'HMAC-SHA256', x_date, credential_scope, canonical_hash,
    ])
    k_date = _hmac_sha256(sk.encode('utf-8'), short_date)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, 'request')
    signature = hmac.new(k_signing, string_to_sign.encode('utf-8'),
                         hashlib.sha256).hexdigest()
    headers['Authorization'] = (
        f"HMAC-SHA256 Credential={ak}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


def sign_request(method: str, url: str, body: bytes,
                 ak: str, sk: str,
                 service: str, region: str = 'cn-north-1',
                 extra_headers: dict | None = None) -> dict:
    try:
        return _sign_with_official_sdk(
            method, url, body, ak, sk, service, region, extra_headers,
        )
    except ImportError:
        return _sign_local_fallback(
            method, url, body, ak, sk, service, region, extra_headers,
        )
