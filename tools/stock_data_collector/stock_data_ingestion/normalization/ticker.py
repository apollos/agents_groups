from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class TickerErrorCode(StrEnum):
    INVALID_TICKER = "INVALID_TICKER"


class TickerNormalizationError(ValueError):
    def __init__(self, message: str, code: TickerErrorCode = TickerErrorCode.INVALID_TICKER) -> None:
        self.code = code
        super().__init__(f"{code.value}: {message}")


@dataclass(frozen=True)
class TickerParts:
    code: str
    exchange: str


_EXCHANGE_ALIASES = {
    "SH": "SH",
    "SSE": "SH",
    "XSHG": "SH",
    "SZ": "SZ",
    "SZSE": "SZ",
    "XSHE": "SZ",
    "BJ": "BJ",
    "BSE": "BJ",
    "XBSE": "BJ",
    "XBEI": "BJ",
    "HK": "HK",
    "HKG": "HK",
    "HKEX": "HK",
    "XHKG": "HK",
}
_PREFIX_TO_EXCHANGE = {
    "sh": "SH",
    "sz": "SZ",
    "bj": "BJ",
    "hk": "HK",
}


A_SHARE_EXCHANGES = {"SH", "SZ", "BJ"}
HK_EXCHANGES = {"HK"}


def infer_exchange(code: str) -> str:
    digits = re.sub(r"\D", "", str(code))
    if not re.fullmatch(r"\d{6}", digits):
        raise TickerNormalizationError(f"unable to infer A-share exchange from {code!r}")
    if digits.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return "SH"
    if digits.startswith(("000", "001", "002", "003", "200", "300", "301")):
        return "SZ"
    if digits.startswith(("43", "82", "83", "87", "88", "89")):
        return "BJ"
    raise TickerNormalizationError(f"unsupported A-share ticker prefix: {digits}")


def _validate_code_for_exchange(code: str, exchange: str) -> None:
    if exchange in A_SHARE_EXCHANGES:
        if not re.fullmatch(r"\d{6}", code):
            raise TickerNormalizationError(f"A-share ticker must be 6 digits: {code!r}")
        inferred = infer_exchange(code)
        # Some providers may suffix an index-like code explicitly. For normal stock
        # symbols we keep the suffix authoritative only when it is plausible.
        if exchange != inferred:
            raise TickerNormalizationError(f"ticker {code!r} belongs to {inferred}, not {exchange}")
    elif exchange == "HK":
        if not re.fullmatch(r"\d{5}", code):
            raise TickerNormalizationError(f"HK ticker must be 5 digits: {code!r}")
    else:
        raise TickerNormalizationError(f"unsupported exchange: {exchange}")


def _parse_ticker(raw: str) -> TickerParts:
    value = str(raw).strip()
    if not value:
        raise TickerNormalizationError("empty ticker")

    compact = value.replace("_", ".").replace("-", ".")
    lower = compact.lower()

    a_prefixed = re.fullmatch(r"(sh|sz|bj)\.?(\d{6})", lower)
    if a_prefixed:
        prefix, code = a_prefixed.groups()
        exchange = _PREFIX_TO_EXCHANGE[prefix]
        _validate_code_for_exchange(code, exchange)
        return TickerParts(code=code, exchange=exchange)

    hk_prefixed = re.fullmatch(r"hk\.?(\d{5})", lower)
    if hk_prefixed:
        code = hk_prefixed.group(1)
        return TickerParts(code=code, exchange="HK")

    suffixed = re.fullmatch(r"(\d{5,6})\.([A-Za-z]+)", compact)
    if suffixed:
        code, exch = suffixed.groups()
        exch = exch.upper()
        if exch not in _EXCHANGE_ALIASES:
            raise TickerNormalizationError(f"unknown exchange suffix: {exch}")
        exchange = _EXCHANGE_ALIASES[exch]
        _validate_code_for_exchange(code, exchange)
        return TickerParts(code=code, exchange=exchange)

    pure = re.fullmatch(r"\d{6}", compact)
    if pure:
        return TickerParts(code=compact, exchange=infer_exchange(compact))

    raise TickerNormalizationError(f"cannot parse ticker {raw!r}")


def normalize_ticker(raw: str) -> str:
    parts = _parse_ticker(raw)
    return f"{parts.code}.{parts.exchange}"


def validate_a_share_ticker(raw: str) -> bool:
    try:
        normalized = normalize_ticker(raw)
    except TickerNormalizationError:
        return False
    return normalized.endswith((".SH", ".SZ", ".BJ"))


def validate_hk_ticker(raw: str) -> bool:
    try:
        return normalize_ticker(raw).endswith(".HK")
    except TickerNormalizationError:
        return False


def is_hk_ticker(raw: str) -> bool:
    return validate_hk_ticker(raw)


def is_a_share_ticker(raw: str) -> bool:
    return validate_a_share_ticker(raw)


def to_tushare_symbol(raw: str) -> str:
    return normalize_ticker(raw)


def to_baostock_symbol(raw: str) -> str:
    normalized = normalize_ticker(raw)
    code, exchange = normalized.split(".")
    if exchange == "HK":
        raise TickerNormalizationError("BaoStock documented Python API does not support HK tickers")
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}[exchange]
    if exchange == "BJ":
        raise TickerNormalizationError("BaoStock documented Python API only accepts sh/sz stock codes")
    return f"{prefix}.{code}"


def to_akshare_symbol(raw: str) -> str:
    normalized = normalize_ticker(raw)
    code, exchange = normalized.split(".")
    if exchange == "HK":
        return f"hk{code}"
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}[exchange]
    return f"{prefix}{code}"


def to_joinquant_symbol(raw: str) -> str:
    normalized = normalize_ticker(raw)
    code, exchange = normalized.split(".")
    suffix = {"SH": "XSHG", "SZ": "XSHE", "BJ": "XBSE"}.get(exchange)
    if suffix is None:
        raise TickerNormalizationError("JoinQuant adapter does not support HK tickers in this project")
    return f"{code}.{suffix}"
