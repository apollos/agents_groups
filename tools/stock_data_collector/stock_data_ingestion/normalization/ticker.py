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
}
_PREFIX_TO_EXCHANGE = {
    "sh": "SH",
    "sz": "SZ",
    "bj": "BJ",
}


def infer_exchange(code: str) -> str:
    digits = re.sub(r"\D", "", str(code))
    if not re.fullmatch(r"\d{6}", digits):
        raise TickerNormalizationError(f"unable to infer exchange from {code!r}")
    if digits.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return "SH"
    if digits.startswith(("000", "001", "002", "003", "200", "300", "301")):
        return "SZ"
    if digits.startswith(("43", "82", "83", "87", "88", "89")):
        return "BJ"
    raise TickerNormalizationError(f"unsupported A-share ticker prefix: {digits}")


def _parse_ticker(raw: str) -> TickerParts:
    value = str(raw).strip()
    if not value:
        raise TickerNormalizationError("empty ticker")

    compact = value.replace("_", ".").replace("-", ".")
    lower = compact.lower()

    prefixed = re.fullmatch(r"(sh|sz|bj)(\d{6})", lower)
    if prefixed:
        prefix, code = prefixed.groups()
        return TickerParts(code=code, exchange=_PREFIX_TO_EXCHANGE[prefix])

    suffixed = re.fullmatch(r"(\d{6})\.([A-Za-z]+)", compact)
    if suffixed:
        code, exch = suffixed.groups()
        exch = exch.upper()
        if exch not in _EXCHANGE_ALIASES:
            raise TickerNormalizationError(f"unknown exchange suffix: {exch}")
        return TickerParts(code=code, exchange=_EXCHANGE_ALIASES[exch])

    pure = re.fullmatch(r"\d{6}", compact)
    if pure:
        return TickerParts(code=compact, exchange=infer_exchange(compact))

    raise TickerNormalizationError(f"cannot parse ticker {raw!r}")


def normalize_ticker(raw: str) -> str:
    parts = _parse_ticker(raw)
    return f"{parts.code}.{parts.exchange}"


def validate_a_share_ticker(raw: str) -> bool:
    try:
        normalize_ticker(raw)
    except TickerNormalizationError:
        return False
    return True


def to_tushare_symbol(raw: str) -> str:
    return normalize_ticker(raw)


def to_akshare_symbol(raw: str) -> str:
    normalized = normalize_ticker(raw)
    code, exchange = normalized.split(".")
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}[exchange]
    return f"{prefix}{code}"


def to_joinquant_symbol(raw: str) -> str:
    normalized = normalize_ticker(raw)
    code, exchange = normalized.split(".")
    suffix = {"SH": "XSHG", "SZ": "XSHE", "BJ": "XBSE"}[exchange]
    return f"{code}.{suffix}"
