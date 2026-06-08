from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd


class ParquetStore:
    """Cleaned structured-data Parquet store.

    Parquet is never used for raw external responses. Raw payloads must remain in RawObjectStore.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _require_pyarrow(self) -> None:
        if importlib.util.find_spec("pyarrow") is None:
            raise ImportError("pyarrow is required for ParquetStore. Install stock-data-collector dependencies.")

    @staticmethod
    def _deduplicate(df: pd.DataFrame, business_key: Sequence[str] | None = None) -> pd.DataFrame:
        if df.empty:
            return df
        keys = [key for key in (business_key or []) if key in df.columns]
        if keys:
            if "ingested_at" in df.columns:
                df = df.sort_values("ingested_at", kind="stable")
            return df.drop_duplicates(subset=keys, keep="last").reset_index(drop=True)
        return df.drop_duplicates().reset_index(drop=True)

    @staticmethod
    def _json_default(value: Any) -> Any:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:  # noqa: BLE001
                pass
        return str(value)

    @classmethod
    def _serialize_nested_cell(cls, value: Any) -> Any:
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=cls._json_default)
        return value

    @classmethod
    def _normalize_for_parquet(cls, df: pd.DataFrame) -> pd.DataFrame:
        """Convert nested JSON-like cells to strings before pyarrow inference.

        PyArrow cannot infer a Parquet struct type from an empty dict such as
        ``supplement_flags={}``. Serializing JSON-like columns keeps the exported
        table stable across empty and non-empty metadata payloads.
        """

        if df.empty:
            return df
        normalized = df.copy()
        for column in normalized.columns:
            series = normalized[column]
            if series.map(lambda value: isinstance(value, (dict, list, tuple))).any():
                normalized[column] = series.map(cls._serialize_nested_cell)
        return normalized

    def write_records(
        self,
        data_type: str,
        records: Iterable[dict[str, Any]],
        partition_cols: Sequence[str] | None = None,
        filename: str = "part-000.parquet",
        business_key: Sequence[str] | None = None,
        dedupe_keys: Sequence[str] | None = None,
    ) -> list[str]:
        self._require_pyarrow()
        df = pd.DataFrame(list(records))
        if df.empty:
            return []
        partition_cols = list(partition_cols or [])
        if business_key is None and dedupe_keys is not None:
            business_key = dedupe_keys
        written: list[str] = []
        if not partition_cols:
            path = self.root / data_type / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                old = pd.read_parquet(path)
                df = pd.concat([old, df], ignore_index=True)
            df = self._normalize_for_parquet(df)
            df = self._deduplicate(df, business_key)
            df.to_parquet(path, index=False)
            return [str(path)]
        for keys, group in df.groupby(partition_cols, dropna=False):
            if not isinstance(keys, tuple):
                keys = (keys,)
            path = self.root / data_type
            for col, val in zip(partition_cols, keys):
                path = path / f"{col}={val}"
            path.mkdir(parents=True, exist_ok=True)
            file_path = path / filename
            if file_path.exists():
                old = pd.read_parquet(file_path)
                group = pd.concat([old, group], ignore_index=True)
            group = self._normalize_for_parquet(group)
            group = self._deduplicate(group, business_key)
            group.to_parquet(file_path, index=False)
            written.append(str(file_path))
        return written

    def read_date_range(
        self,
        data_type: str,
        date_col: str,
        start_date: str,
        end_date: str,
        tickers: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        self._require_pyarrow()
        files = list((self.root / data_type).rglob("*.parquet"))
        frames = [pd.read_parquet(path) for path in files]
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        df[date_col] = pd.to_datetime(df[date_col])
        mask = (df[date_col] >= pd.to_datetime(start_date)) & (df[date_col] <= pd.to_datetime(end_date))
        if tickers and "normalized_ticker" in df.columns:
            mask &= df["normalized_ticker"].isin(tickers)
        return df.loc[mask].reset_index(drop=True)

    def read_tickers(self, data_type: str, tickers: Sequence[str]) -> pd.DataFrame:
        self._require_pyarrow()
        files = list((self.root / data_type).rglob("*.parquet"))
        frames = [pd.read_parquet(path) for path in files]
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        if "normalized_ticker" not in df.columns:
            return df
        return df[df["normalized_ticker"].isin(tickers)].reset_index(drop=True)

    def validate_row_count(self, data_type: str, sqlite_row_count: int) -> bool:
        self._require_pyarrow()
        files = list((self.root / data_type).rglob("*.parquet"))
        parquet_count = sum(len(pd.read_parquet(path)) for path in files)
        return parquet_count == sqlite_row_count
