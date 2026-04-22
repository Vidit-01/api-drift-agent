from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from drift_agent.code_analyzer.resolver import ModelResolver, ModuleInfo
from drift_agent.types import EndpointContract, FieldSchema, ParameterSchema, RequestBodySchema, ResponseSchema

LOGGER = logging.getLogger(__name__)
PATH_PARAM_RE = re.compile(r"{([^}]+)}")


@dataclass
class RouterDefinition:
    key: str
    variable_name: str
    module: str
    file_path: Path
    defined_at_line: int
    instance_prefix: str = ""
    is_app: bool = False
    tags: list[str] = field(default_factory=list)


@dataclass
class IncludeEdge:
    parent_key: str
    child_key: str
    include_prefix: str


def extract_endpoints(
    project_root: Path,
    modules: dict[str, ModuleInfo],
    routers: dict[str, RouterDefinition],
    includes: list[IncludeEdge],
    model_resolver: ModelResolver,
) -> dict[str, EndpointContract]:
    prefix_map = _resolve_router_prefixes(routers, includes)
    endpoints: dict[str, EndpointContract] = {}
    for module_info in modules.values():
        for node in ast.walk(module_info.tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for decorator in node.decorator_list:
                route = _extract_route_decorator(decorator)
                if route is None:
                    continue
                owner_key = _resolve_router_key(module_info, route["owner"])
                prefixes = prefix_map.get(owner_key, [""])
                for prefix in prefixes:
                    method = route["method"].upper()
                    resolved_path = _normalize_path(f"{prefix}{route['path']}")
                    parameters, request_body = _extract_parameters(module_info, node, resolved_path, model_resolver)
                    responses = _extract_responses(module_info, node, route, model_resolver)
                    tags = route["tags"] or routers.get(owner_key, RouterDefinition("", "", module_info.module, module_info.filepath, 0)).tags
                    endpoint = EndpointContract(
                        method=method,
                        path=resolved_path,
                        parameters=parameters,
                        request_body=request_body,
                        responses=responses,
                        tags=tags,
                        source_file=str(module_info.filepath.relative_to(project_root)),
                        source_line=decorator.lineno,
                    )
                    endpoints[f"{method} {resolved_path}"] = endpoint
    return endpoints


def _resolve_router_prefixes(
    routers: dict[str, RouterDefinition],
    includes: list[IncludeEdge],
) -> dict[str, list[str]]:
    children_by_parent: dict[str, list[IncludeEdge]] = {}
    for edge in includes:
        children_by_parent.setdefault(edge.parent_key, []).append(edge)
    prefix_map: dict[str, list[str]] = {}
    roots = [key for key, router in routers.items() if router.is_app]
    for root_key in roots:
        prefix_map.setdefault(root_key, []).append(_normalize_path_fragment(routers[root_key].instance_prefix))
        stack = [(root_key, _normalize_path_fragment(routers[root_key].instance_prefix))]
        while stack:
            parent_key, parent_prefix = stack.pop()
            for edge in children_by_parent.get(parent_key, []):
                child = routers.get(edge.child_key)
                if child is None:
                    continue
                combined = _normalize_path_fragment(f"{parent_prefix}{edge.include_prefix}{child.instance_prefix}")
                prefix_map.setdefault(edge.child_key, [])
                if combined not in prefix_map[edge.child_key]:
                    prefix_map[edge.child_key].append(combined)
                    stack.append((edge.child_key, combined))
    for key, router in routers.items():
        prefix_map.setdefault(key, [_normalize_path_fragment(router.instance_prefix)])
    return prefix_map


def _extract_route_decorator(decorator: ast.AST) -> dict[str, Any] | None:
    if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
        return None
    method = decorator.func.attr.lower()
    if method not in {"get", "post", "put", "patch", "delete", "head", "options"}:
        return None
    if not decorator.args or not isinstance(decorator.args[0], ast.Constant) or not isinstance(decorator.args[0].value, str):
        LOGGER.warning("Skipping dynamic route decorator at line %s", getattr(decorator, "lineno", "?"))
        return None
    route: dict[str, Any] = {
        "owner": decorator.func.value,
        "method": method,
        "path": decorator.args[0].value,
        "response_model": None,
        "status_code": _default_status_code(method),
        "tags": [],
    }
    for keyword in decorator.keywords:
        if keyword.arg == "response_model":
            route["response_model"] = keyword.value
        elif keyword.arg == "status_code" and isinstance(keyword.value, ast.Constant):
            route["status_code"] = int(keyword.value.value)
        elif keyword.arg == "tags" and isinstance(keyword.value, ast.List):
            route["tags"] = [
                elt.value
                for elt in keyword.value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
    return route


def _extract_parameters(
    module_info: ModuleInfo,
    function_node: ast.FunctionDef,
    resolved_path: str,
    model_resolver: ModelResolver,
) -> tuple[list[ParameterSchema], RequestBodySchema | None]:
    path_params = set(PATH_PARAM_RE.findall(resolved_path))
    parameters: list[ParameterSchema] = []
    request_body = None
    args = [*function_node.args.posonlyargs, *function_node.args.args, *function_node.args.kwonlyargs]
    positional_defaults = [None] * (len(function_node.args.posonlyargs) + len(function_node.args.args) - len(function_node.args.defaults))
    positional_defaults.extend(function_node.args.defaults)
    kw_defaults = function_node.args.kw_defaults
    defaults = positional_defaults + kw_defaults
    for arg, default in zip(args, defaults):
        if arg.arg == "self":
            continue
        marker = _default_marker(default)
        if marker == "Depends":
            continue
        if arg.arg in path_params:
            field, _ = model_resolver.resolve_annotation(module_info.module, arg.annotation, arg.arg)
            field.required = True
            parameters.append(ParameterSchema(name=arg.arg, location="path", required=True, schema=field))
            continue
        if marker == "Header":
            field, _ = model_resolver.resolve_annotation(module_info.module, arg.annotation, arg.arg)
            parameters.append(
                ParameterSchema(
                    name=arg.arg.replace("_", "-").title(),
                    location="header",
                    required=_is_required_parameter(default),
                    schema=field,
                )
            )
            continue
        if marker == "Cookie":
            field, _ = model_resolver.resolve_annotation(module_info.module, arg.annotation, arg.arg)
            parameters.append(
                ParameterSchema(
                    name=arg.arg,
                    location="cookie",
                    required=_is_required_parameter(default),
                    schema=field,
                )
            )
            continue
        if marker == "Query":
            field, _ = model_resolver.resolve_annotation(module_info.module, arg.annotation, arg.arg)
            parameters.append(
                ParameterSchema(
                    name=arg.arg,
                    location="query",
                    required=_is_required_parameter(default, fallback=default is None),
                    schema=field,
                )
            )
            continue
        if marker == "Body" or _looks_like_body_model(module_info.module, arg.annotation, model_resolver):
            if request_body is not None:
                LOGGER.warning("Multiple body parameters found in %s:%s; using first", module_info.filepath, function_node.lineno)
                continue
            schema, _ = model_resolver.resolve_annotation(module_info.module, arg.annotation, arg.arg)
            request_body = RequestBodySchema(
                required=_is_required_parameter(default, fallback=default is None),
                content_type="application/json",
                schema=schema,
            )
            continue
        field, _ = model_resolver.resolve_annotation(module_info.module, arg.annotation, arg.arg)
        parameters.append(
            ParameterSchema(
                name=arg.arg,
                location="query",
                required=_is_required_parameter(default, fallback=default is None),
                schema=field,
            )
        )
    return parameters, request_body


def _extract_responses(
    module_info: ModuleInfo,
    function_node: ast.FunctionDef,
    route: dict[str, Any],
    model_resolver: ModelResolver,
) -> dict[str, ResponseSchema]:
    status_code = str(route["status_code"])
    response_field = None
    if route["response_model"] is not None:
        response_field, _ = model_resolver.resolve_annotation(module_info.module, route["response_model"], "response")
    elif function_node.returns is not None:
        response_field, _ = model_resolver.resolve_annotation(module_info.module, function_node.returns, "response")
        if response_field.type == "unknown":
            response_field = _infer_dict_response(function_node)
    else:
        response_field = _infer_dict_response(function_node)
    responses = {
        status_code: ResponseSchema(status_code=status_code, content_type="application/json", schema=response_field),
        "422": ResponseSchema(
            status_code="422",
            content_type="application/json",
            schema=FieldSchema(
                name="validation_error",
                type="object",
                description="[FastAPI validation error response]",
                properties={},
            ),
        ),
    }
    return responses


def _infer_dict_response(function_node: ast.FunctionDef) -> FieldSchema | None:
    keys: dict[str, FieldSchema] = {}
    for node in ast.walk(function_node):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        for key in node.value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys[key.value] = FieldSchema(name=key.value, type="unknown")
    if not keys:
        return None
    return FieldSchema(name="response", type="object", properties=keys)


def _resolve_router_key(module_info: ModuleInfo, owner: ast.AST) -> str:
    if isinstance(owner, ast.Name):
        imported = module_info.imports.get(owner.id)
        if imported:
            return f"{imported[0]}::{imported[1]}"
        return f"{module_info.module}::{owner.id}"
    if isinstance(owner, ast.Attribute):
        if isinstance(owner.value, ast.Name):
            imported = module_info.imports.get(owner.value.id)
            if imported:
                return f"{imported[0]}::{owner.attr}"
        return f"{module_info.module}::{owner.attr}"
    return f"{module_info.module}::app"


def _default_marker(default: ast.AST | None) -> str | None:
    if not isinstance(default, ast.Call):
        return None
    if isinstance(default.func, ast.Name):
        return default.func.id
    if isinstance(default.func, ast.Attribute):
        return default.func.attr
    return None


def _looks_like_body_model(module_name: str, annotation: ast.AST | None, model_resolver: ModelResolver) -> bool:
    if annotation is None:
        return False
    field, _ = model_resolver.resolve_annotation(module_name, annotation, "body")
    return field.type == "object" and field.description is None


def _is_required_parameter(default: ast.AST | None, fallback: bool = False) -> bool:
    if default is None:
        return fallback
    if isinstance(default, ast.Call):
        if not default.args:
            return fallback
        first_arg = default.args[0]
        return isinstance(first_arg, ast.Constant) and first_arg.value is Ellipsis
    return False


def _default_status_code(method: str) -> int:
    return 201 if method == "post" else 200


def _normalize_path(path: str) -> str:
    combined = path if path.startswith("/") else f"/{path}"
    while "//" in combined:
        combined = combined.replace("//", "/")
    if combined != "/" and combined.endswith("/"):
        combined = combined[:-1]
    return combined


def _normalize_path_fragment(path: str) -> str:
    if not path:
        return ""
    path = _normalize_path(path)
    return "" if path == "/" else path

