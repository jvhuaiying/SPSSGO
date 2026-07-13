# -*- coding: utf-8 -*-
"""数据浏览服务，只管预览、变量查看和导出，别把上传落库塞进来。"""
import os
import re
import tempfile
from io import BytesIO

import pandas as pd
from fastapi import HTTPException

from backend.app_runtime import download_response
from backend.file_parser import parse_data_file
from backend.services.file_service import load_dataframe
from backend.services.session_data_service import materialized_session_data, resolve_session_data_source
from backend.services.variable_metadata_service import infer_variable_type, recommended_auto_group_count, sort_label_values

EXPORT_FORMATS = {
    "xlsx": {"ext": ".xlsx", "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    "csv": {"ext": ".csv", "media_type": "text/csv; charset=utf-8"},
    "sav": {"ext": ".sav", "media_type": "application/octet-stream"},
    "dta": {"ext": ".dta", "media_type": "application/octet-stream"},
    "xpt": {"ext": ".xpt", "media_type": "application/octet-stream"},
    "tsv": {"ext": ".tsv", "media_type": "text/tab-separated-values; charset=utf-8"},
    "txt": {"ext": ".txt", "media_type": "text/plain; charset=utf-8"},
    "json": {"ext": ".json", "media_type": "application/json; charset=utf-8"},
    "parquet": {"ext": ".parquet", "media_type": "application/octet-stream"},
}

DEFAULT_PREVIEW_LIMIT = 100
MAX_PREVIEW_LIMIT = 1000

_ALIAS_NUMBER_RE = re.compile(r'^[Qq]?(\d{1,3})\s*[、.。)）\s:_\-]')
_ALIAS_EXISTING_RE = re.compile(r'^(q\d{1,3}(?:[_-]\d{1,3})?)$', re.IGNORECASE)


async def build_data_preview(session_id: str, limit: int = 100, *, allow_legacy_fallback: bool = False):
    data_source = await resolve_session_data_source(
        session_id,
        allow_legacy_fallback=allow_legacy_fallback,
    )
    with materialized_session_data(data_source) as data_file:
        df, _ = parse_data_file(data_file)
        sample = df.head(_normalize_preview_limit(limit))
        headers = [str(c) for c in sample.columns]
        rows = []
        for _, row in sample.iterrows():
            rows.append([
                "" if pd.isna(v) else (str(int(v)) if isinstance(v, float) and v == int(v) else str(v))
                for v in row
            ])
        return {
            "filename": data_source["filename"],
            "total_rows": len(df),
            "total_cols": len(df.columns),
            "headers": headers,
            "rows": rows,
            "source": data_source["source"],
            "dataset_version_id": data_source["dataset_version_id"],
            "dataset_version_no": data_source["dataset_version_no"],
        }


def _generate_aliases(columns) -> tuple[dict[str, str], dict[str, str]]:
    """根据列名生成短别名，返回 (name->alias, alias->name) 两个映射。"""
    alias_to_name: dict[str, str] = {}
    name_to_alias: dict[str, str] = {}
    counter: dict[str, int] = {}

    for idx, col in enumerate(columns):
        col_str = str(col)
        # 已经是 q 开头的短别名格式
        em = _ALIAS_EXISTING_RE.match(col_str)
        if em:
            alias = em.group(1).lower()
            name_to_alias[col_str] = alias
            alias_to_name[alias] = col_str
            continue

        # 尝试从列名提取题号，如 "7、xxx" → q7
        nm = _ALIAS_NUMBER_RE.match(col_str)
        if nm:
            base = f"q{nm.group(1)}"
        else:
            base = f"v{idx + 1}"

        # 处理重复别名
        if base in alias_to_name:
            count = counter.get(base, 0) + 1
            counter[base] = count
            alias = f"{base}_{count + 1}"
        else:
            alias = base
            counter[base] = 0

        name_to_alias[col_str] = alias
        alias_to_name[alias] = col_str

    return name_to_alias, alias_to_name


async def get_variable_values(session_id: str, column_name: str, metadata_map: dict[str, dict], limit: int = 200, *, allow_legacy_fallback: bool = False):
    data_source = await resolve_session_data_source(
        session_id,
        allow_legacy_fallback=allow_legacy_fallback,
    )
    with materialized_session_data(data_source) as data_file:
        df = load_dataframe(data_file)
        # 支持 alias 查找
        if column_name not in df.columns:
            name_to_alias, _ = _generate_aliases(df.columns)
            resolved = None
            for name, alias in name_to_alias.items():
                if alias == column_name:
                    resolved = name
                    break
            if resolved:
                column_name = resolved
            else:
                raise HTTPException(404, "变量不存在")
        series = df[column_name]
        inferred_type = infer_variable_type(series, column_name)
        metadata = metadata_map.get(column_name, {})
        values = sort_label_values(series.dropna().unique().tolist())
        final_type = metadata.get("var_type") or inferred_type
        return {
            "column": column_name,
            "type": final_type,
            "values": values[:limit],
            "total_unique": len(values),
            "truncated": len(values) > limit,
            "sample_size": int(series.dropna().shape[0]),
            "recommended_groups": recommended_auto_group_count(int(series.dropna().shape[0])),
            "supports_labels": final_type == "categorical",
            "value_labels": metadata.get("value_labels", {}),
            "code_rules": metadata.get("code_rules", {}),
            "source": data_source["source"],
            "dataset_version_id": data_source["dataset_version_id"],
            "dataset_version_no": data_source["dataset_version_no"],
        }


async def export_data_file(session_id: str, export_format: str, *, allow_legacy_fallback: bool = False):
    data_source = await resolve_session_data_source(
        session_id,
        allow_legacy_fallback=allow_legacy_fallback,
    )
    filename = data_source["filename"]
    with materialized_session_data(data_source) as data_file:
        df, _ = parse_data_file(data_file)
        content, media_type = _export_dataframe_content(df, export_format)
        export_name = _build_export_filename(filename, export_format)
        return download_response(content, export_name, media_type)


async def get_variables(session_id: str, metadata_map: dict[str, dict], *, allow_legacy_fallback: bool = False):
    data_source = await resolve_session_data_source(
        session_id,
        allow_legacy_fallback=allow_legacy_fallback,
    )
    with materialized_session_data(data_source) as data_file:
        df = load_dataframe(data_file)
        name_to_alias, alias_to_name = _generate_aliases(df.columns)
        variables = []
        for col in df.columns:
            col_str = str(col)
            metadata = metadata_map.get(col_str, {})
            alias = name_to_alias.get(col_str, col_str)
            label = col_str
            user_display_name = metadata.get("display_name")
            display_name = user_display_name if (user_display_name and user_display_name != col_str) else col_str
            variables.append({
                "name": col_str,
                "alias": alias,
                "label": label,
                "display_name": display_name,
                "dtype": str(df[col].dtype),
                "type": metadata.get("var_type") or infer_variable_type(df[col], col_str),
                "nunique": int(df[col].nunique(dropna=True)),
                "missing": int(df[col].isna().sum()),
                "value_labels": metadata.get("value_labels", {}),
                "code_rules": metadata.get("code_rules", {}),
            })
        return {
            "variables": variables,
            "column_aliases": alias_to_name,
            "total_rows": len(df),
            "source": data_source["source"],
            "dataset_version_id": data_source["dataset_version_id"],
            "dataset_version_no": data_source["dataset_version_no"],
        }


def _build_export_filename(filename: str, export_format: str) -> str:
    base_name = os.path.splitext(filename)[0]
    return f"{base_name}{EXPORT_FORMATS[export_format]['ext']}"


def _export_dataframe_content(df: pd.DataFrame, export_format: str):
    if export_format not in EXPORT_FORMATS:
        raise HTTPException(400, "不支持的导出格式")

    if export_format == "xlsx":
        buffer = BytesIO()
        df.to_excel(buffer, index=False)
        return buffer.getvalue(), EXPORT_FORMATS[export_format]["media_type"]
    if export_format == "csv":
        return df.to_csv(index=False).encode("utf-8-sig"), EXPORT_FORMATS[export_format]["media_type"]
    if export_format in {"tsv", "txt"}:
        return df.to_csv(index=False, sep="\t").encode("utf-8-sig"), EXPORT_FORMATS[export_format]["media_type"]
    if export_format == "json":
        return df.to_json(orient="records", force_ascii=False, indent=2).encode("utf-8"), EXPORT_FORMATS[export_format]["media_type"]
    if export_format == "parquet":
        buffer = BytesIO()
        df.to_parquet(buffer, index=False)
        return buffer.getvalue(), EXPORT_FORMATS[export_format]["media_type"]

    try:
        import pyreadstat
    except Exception as exc:
        raise HTTPException(500, f"缺少导出依赖: {str(exc)}")

    suffix = EXPORT_FORMATS[export_format]["ext"]
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp:
            temp_path = temp.name
        if export_format == "sav":
            pyreadstat.write_sav(df, temp_path)
        elif export_format == "dta":
            pyreadstat.write_dta(df, temp_path)
        elif export_format == "xpt":
            pyreadstat.write_xport(df, temp_path)
        else:
            raise HTTPException(400, "不支持的导出格式")
        with open(temp_path, "rb") as handle:
            return handle.read(), EXPORT_FORMATS[export_format]["media_type"]
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _normalize_preview_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_PREVIEW_LIMIT
    if value <= 0:
        return DEFAULT_PREVIEW_LIMIT
    return min(value, MAX_PREVIEW_LIMIT)
