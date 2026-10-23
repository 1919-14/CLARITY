"""Safe, AST-based mathematical expression validation for researcher-proposed hypotheses.

Design Principles:
------------------
1. Absolutely NO `eval`, `exec`, or dynamic code execution.
2. Expressions are parsed into an Abstract Syntax Tree (AST) using Python's `ast.parse(mode='eval')`.
3. Only a strict whitelist of AST node types, binary/unary operators, and numeric constants is permitted.
4. Function calls, attribute lookups, indexing, comprehensions, and imports are strictly forbidden.
5. Expressions are validated against registered model families (e.g. bounded polynomials).
6. Friendly, actionable structured validation errors are returned for malformed or unsafe inputs.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import math
import re
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Set, Tuple

from .contracts import StructuredError
from .hypothesis_models import (
    HARD_MAX_POLYNOMIAL_DEGREE,
    POLYNOMIAL_FAMILY_ID,
)

# Structured error codes for hypothesis expression validation
ERROR_INVALID_EXPRESSION = "INVALID_EXPRESSION"
ERROR_UNSAFE_EXPRESSION = "UNSAFE_EXPRESSION"
ERROR_MALFORMED_EXPRESSION = "MALFORMED_EXPRESSION"
ERROR_UNSUPPORTED_OPERATOR = "UNSUPPORTED_OPERATOR"
ERROR_UNSUPPORTED_FUNCTION = "UNSUPPORTED_FUNCTION"
ERROR_INVALID_VARIABLE = "INVALID_VARIABLE"
ERROR_DISALLOWED_CONSTRUCT = "DISALLOWED_CONSTRUCT"
ERROR_UNSUPPORTED_MODEL_FAMILY = "UNSUPPORTED_MODEL_FAMILY"
ERROR_INVALID_DEGREE = "INVALID_DEGREE"
ERROR_INVALID_PARAMETERS = "INVALID_PARAMETERS"

# Dangerous keywords that indicate attempted code execution or system access
DANGEROUS_PATTERNS = re.compile(
    r"(?i)\b(__import__|import|exec|eval|open|os|sys|subprocess|shutil|builtins|"
    r"globals|locals|getattr|setattr|delattr|hasattr|compile|breakpoint|"
    r"system|popen|spawn|lambda)\b|__"
)

# Supported mathematical binary operators
ALLOWED_BIN_OPS: Dict[type, str] = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.Pow: "**",
}

# Supported mathematical unary operators
ALLOWED_UNARY_OPS: Dict[type, str] = {
    ast.UAdd: "+",
    ast.USub: "-",
}

# Supported model families
SUPPORTED_MODEL_FAMILIES: FrozenSet[str] = frozenset({
    POLYNOMIAL_FAMILY_ID,
    "linear",
    "quadratic",
    "cubic",
    "constant",
    "1d_linear",
    "1d_polynomial",
})


@dataclass(frozen=True)
class ValidatedExpression:
    """Structured result of a successful expression validation."""

    raw_formula: str
    normalized_formula: str
    model_family: str
    input_variable: str
    output_variable: str
    degree: int
    parameters: Dict[str, float]
    complexity: int
    referenced_variables: Tuple[str, ...]


class _SafeExpressionValidator(ast.NodeVisitor):
    """AST visitor that ensures the expression contains only allowed mathematical constructs."""

    def __init__(self, allowed_variables: Set[str]):
        self.allowed_variables = allowed_variables
        self.referenced_variables: Set[str] = set()
        self.highest_degree = 0
        self.has_division = False

    def visit(self, node: ast.AST) -> None:
        # Check against allowed AST node types
        if isinstance(node, ast.Expression):
            self.visit(node.body)
            return

        if isinstance(node, ast.BinOp):
            if type(node.op) not in ALLOWED_BIN_OPS:
                op_name = type(node.op).__name__
                raise ValueError(
                    f"Unsupported operator '{op_name}'. Only +, -, *, /, and ** (or ^) are supported."
                )
            if isinstance(node.op, ast.Div):
                self.has_division = True
                # Check for explicit division by literal zero
                if isinstance(node.right, ast.Constant) and node.right.value == 0:
                    raise ValueError("Division by zero in expression.")

            if isinstance(node.op, ast.Pow):
                # Check exponent is a non-negative integer within bounds
                exp_node = node.right
                if isinstance(exp_node, ast.UnaryOp) and isinstance(exp_node.op, ast.USub):
                    raise ValueError("Negative exponents are not supported in polynomial expressions.")
                if isinstance(exp_node, ast.Constant):
                    val = exp_node.value
                    if isinstance(val, bool) or not isinstance(val, (int, float)):
                        raise ValueError("Exponent must be an integer.")
                    if isinstance(val, float) and not val.is_integer():
                        raise ValueError(f"Non-integer exponent '{val}' is not supported in polynomials.")
                    int_val = int(val)
                    if int_val < 0:
                        raise ValueError("Negative exponents are not supported in polynomial expressions.")
                    if int_val > HARD_MAX_POLYNOMIAL_DEGREE:
                        raise ValueError(
                            f"Polynomial degree {int_val} exceeds maximum allowed ({HARD_MAX_POLYNOMIAL_DEGREE})."
                        )
                    if int_val > self.highest_degree:
                        self.highest_degree = int_val
                else:
                    raise ValueError("Exponents must be constant non-negative integers.")

            self.visit(node.left)
            self.visit(node.right)
            return

        if isinstance(node, ast.UnaryOp):
            if type(node.op) not in ALLOWED_UNARY_OPS:
                op_name = type(node.op).__name__
                raise ValueError(f"Unsupported unary operator '{op_name}'. Only + and - are supported.")
            self.visit(node.operand)
            return

        if isinstance(node, ast.Constant):
            val = node.value
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"Constant '{val}' of type {type(val).__name__} is not allowed; only real numbers are permitted.")
            if not math.isfinite(val):
                raise ValueError(f"Non-finite numeric constant '{val}' is not allowed.")
            return

        if isinstance(node, ast.Name):
            var_name = node.id
            self.referenced_variables.add(var_name)
            if var_name not in self.allowed_variables:
                raise ValueError(
                    f"Unknown variable or identifier '{var_name}'. "
                    f"Only the task input variable '{', '.join(sorted(self.allowed_variables))}' and parameter coefficients are allowed."
                )
            if var_name in ("x", "input") and self.highest_degree < 1:
                self.highest_degree = 1
            return

        # Explicit rejection for forbidden constructs
        if isinstance(node, ast.Call):
            func_name = getattr(node.func, "id", None) or getattr(node.func, "attr", "function")
            raise ValueError(f"Function call '{func_name}()' is not permitted in hypothesis expressions.")

        if isinstance(node, ast.Attribute):
            raise ValueError(f"Attribute access '.{node.attr}' is not permitted.")

        if isinstance(node, (ast.Subscript, ast.Slice)):
            raise ValueError("Indexing and slicing are not permitted.")

        if isinstance(node, (ast.List, ast.Dict, ast.Set, ast.Tuple)):
            raise ValueError("Data structure literals (lists, dicts, tuples) are not permitted.")

        if isinstance(node, (ast.Lambda, ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)):
            raise ValueError("Comprehensions and lambdas are not permitted.")

        if isinstance(node, (ast.Compare, ast.BoolOp)):
            raise ValueError("Comparison and boolean logic operators are not permitted.")

        raise ValueError(f"Disallowed expression construct '{type(node).__name__}'.")


def validate_hypothesis_expression(
    formula: str,
    model_family: str = POLYNOMIAL_FAMILY_ID,
    parameters: Optional[Mapping[str, Any]] = None,
    complexity: Optional[int] = None,
    input_variable: str = "x",
    output_variable: str = "y",
) -> Tuple[Optional[ValidatedExpression], Optional[StructuredError]]:
    """Validate a researcher-proposed hypothesis expression safely without code execution.

    Returns:
      (ValidatedExpression, None) on success.
      (None, StructuredError) on validation failure.
    """
    if not isinstance(formula, str) or not formula.strip():
        return None, StructuredError(
            code=ERROR_INVALID_EXPRESSION,
            message="Hypothesis formula is required and must be a non-empty string.",
        )

    clean_formula = formula.strip()
    if len(clean_formula) > 1000:
        return None, StructuredError(
            code=ERROR_INVALID_EXPRESSION,
            message="Hypothesis formula exceeds maximum permitted length (1000 characters).",
        )

    # 1. Proactive security check: detect dangerous functions/imports/keywords
    if DANGEROUS_PATTERNS.search(clean_formula):
        return None, StructuredError(
            code=ERROR_UNSAFE_EXPRESSION,
            message="Unsafe expression rejected: dynamic execution, imports, and system calls are strictly forbidden.",
        )

    # 2. Check model family
    norm_family = model_family.strip().lower() if isinstance(model_family, str) else ""
    if norm_family in ("linear", "quadratic", "cubic", "constant", "1d_linear", "1d_polynomial"):
        norm_family = POLYNOMIAL_FAMILY_ID

    if norm_family not in SUPPORTED_MODEL_FAMILIES:
        return None, StructuredError(
            code=ERROR_UNSUPPORTED_MODEL_FAMILY,
            message=f"Model family '{model_family}' is not supported. Supported families: {sorted(SUPPORTED_MODEL_FAMILIES)}.",
            details={"supported_families": sorted(SUPPORTED_MODEL_FAMILIES)},
        )

    # 3. Separate LHS (output variable) from RHS (expression)
    expr_body = clean_formula
    if "=" in clean_formula:
        parts = clean_formula.split("=", 1)
        lhs = parts[0].strip()
        rhs = parts[1].strip()
        if not lhs.isidentifier():
            return None, StructuredError(
                code=ERROR_MALFORMED_EXPRESSION,
                message=f"Left-hand side of equation must be a valid variable name, got '{lhs}'.",
            )
        output_var = lhs
        expr_body = rhs
    else:
        output_var = output_variable

    if not expr_body:
        return None, StructuredError(
            code=ERROR_MALFORMED_EXPRESSION,
            message="Formula expression body is empty.",
        )

    # 4. Normalize mathematical caret (^) to Python power operator (**) and implicit multiplication
    normalized_expr = re.sub(r"\^", "**", expr_body)
    normalized_expr = re.sub(r"(\d+(?:\.\d+)?)(?![eE][+-]?\d)\s*([a-zA-Z_])", r"\1*\2", normalized_expr)
    normalized_expr = re.sub(r"\)\s*\(", ")*(", normalized_expr)
    normalized_expr = re.sub(r"(\d+)\s*\(", r"\1*(", normalized_expr)
    normalized_expr = re.sub(r"\)\s*([a-zA-Z_])", r")*\1", normalized_expr)

    # 5. Parse into AST in 'eval' mode
    try:
        tree = ast.parse(normalized_expr, mode="eval")
    except SyntaxError as exc:
        return None, StructuredError(
            code=ERROR_MALFORMED_EXPRESSION,
            message=f"Malformed mathematical expression syntax: {exc.msg} at position {exc.offset}.",
            details={"offset": exc.offset, "text": exc.text},
        )
    except Exception as exc:  # noqa: BLE001
        return None, StructuredError(
            code=ERROR_MALFORMED_EXPRESSION,
            message=f"Failed to parse mathematical expression ({type(exc).__name__}).",
        )

    # 6. AST Walk and validation
    allowed_identifiers: Set[str] = {input_variable, "x", "X"}
    param_dict: Dict[str, float] = {}
    if parameters and isinstance(parameters, Mapping):
        for k, v in parameters.items():
            if not isinstance(k, str) or not k.strip():
                return None, StructuredError(
                    code=ERROR_INVALID_PARAMETERS,
                    message="Parameter keys must be non-empty strings.",
                )
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return None, StructuredError(
                    code=ERROR_INVALID_PARAMETERS,
                    message=f"Parameter '{k}' must be a numeric value, got {type(v).__name__}.",
                )
            if not math.isfinite(float(v)):
                return None, StructuredError(
                    code=ERROR_INVALID_PARAMETERS,
                    message=f"Parameter '{k}' must be finite.",
                )
            allowed_identifiers.add(k.strip())
            param_dict[k.strip()] = float(v)

    validator = _SafeExpressionValidator(allowed_identifiers)
    try:
        validator.visit(tree)
    except ValueError as exc:
        return None, StructuredError(
            code=ERROR_UNSAFE_EXPRESSION if "not permitted" in str(exc) else ERROR_INVALID_EXPRESSION,
            message=str(exc),
        )

    detected_degree = validator.highest_degree
    if detected_degree > HARD_MAX_POLYNOMIAL_DEGREE:
        return None, StructuredError(
            code=ERROR_INVALID_DEGREE,
            message=f"Polynomial degree {detected_degree} exceeds hard ceiling of {HARD_MAX_POLYNOMIAL_DEGREE}.",
        )

    # 7. Normalize parameters to canonical polynomial structure
    canonical_parameters: Dict[str, Any] = {}
    if norm_family == POLYNOMIAL_FAMILY_ID:
        if "coefficients" in param_dict or any(k.startswith("c") for k in param_dict):
            # Already using c0, c1, ...
            coeffs = {f"c{i}": param_dict.get(f"c{i}", 0.0) for i in range(detected_degree + 1)}
        elif "a" in param_dict or "b" in param_dict:
            # Linear shorthand y = a*x + b -> c0 = b, c1 = a
            coeffs = {
                "c0": param_dict.get("b", 0.0),
                "c1": param_dict.get("a", 1.0),
            }
            if detected_degree > 1:
                for i in range(2, detected_degree + 1):
                    coeffs[f"c{i}"] = param_dict.get(f"c{i}", 0.0)
        else:
            # Default ascending coefficients
            coeffs = {f"c{i}": param_dict.get(f"c{i}", 1.0 if i == detected_degree else 0.0) for i in range(detected_degree + 1)}

        canonical_parameters = {
            "family": POLYNOMIAL_FAMILY_ID,
            "degree": detected_degree,
            "coefficient_order": "ascending",
            "coefficients": coeffs,
            "input_variable": input_variable,
            "output_variable": output_var,
        }
    else:
        canonical_parameters = dict(param_dict)

    # 8. Compute complexity
    computed_complexity = complexity if (complexity and isinstance(complexity, int) and complexity >= 1) else (detected_degree + 1)

    normalized_formula = f"{output_var} = {clean_formula if '=' not in clean_formula else clean_formula.split('=', 1)[1].strip()}"

    return ValidatedExpression(
        raw_formula=clean_formula,
        normalized_formula=normalized_formula,
        model_family=norm_family,
        input_variable=input_variable,
        output_variable=output_var,
        degree=detected_degree,
        parameters=canonical_parameters,
        complexity=computed_complexity,
        referenced_variables=tuple(sorted(validator.referenced_variables)),
    ), None
