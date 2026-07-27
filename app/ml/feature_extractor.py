"""
Extracts the 23 stylometric / structural / AST features that the classical
models (XGBoost, RandomForest, LogisticRegression) and the fusion scaler
were trained on. Order MUST match app/ml/models/feature_columns.pkl exactly.
"""
import ast
import re

FEATURE_COLUMNS = [
    "comment_ratio", "avg_line_len", "std_line_len", "indent_std", "blank_ratio",
    "unique_token_ratio", "avg_identifier_len", "total_lines", "total_chars",
    "struct_n_loops", "struct_n_conditionals", "struct_n_returns", "struct_n_func_defs",
    "struct_n_calls", "struct_max_brace_depth", "struct_n_operators", "struct_n_semicolons",
    "struct_n_braces", "struct_cyclomatic", "ast_max_depth", "ast_total_nodes",
    "ast_n_functions", "ast_parse_failed",
]

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[^\sA-Za-z0-9_]")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OPERATOR_RE = re.compile(r"[+\-*/%=<>!&|^~]+")

_COMMENT_PREFIXES = {
    "python": ("#",),
    "javascript": ("//",),
    "typescript": ("//",),
    "java": ("//",),
    "c": ("//",),
    "cpp": ("//",),
    "c++": ("//",),
    "csharp": ("//",),
    "go": ("//",),
}

_LOOP_KEYWORDS = ("for", "while", "do")
_CONDITIONAL_KEYWORDS = ("if", "elif", "else if", "else", "switch", "case")
_RETURN_KEYWORDS = ("return",)
_FUNC_DEF_PATTERNS = (
    re.compile(r"\bdef\s+\w+\s*\("),                       # python
    re.compile(r"\bfunction\s+\w*\s*\("),                  # js/ts
    re.compile(r"\b(?:public|private|protected|static)*\s*[\w<>\[\]]+\s+\w+\s*\([^;{]*\)\s*\{"),  # java/c/c++/c#
    re.compile(r"\bfunc\s+\w+\s*\("),                      # go
)


def _line_stats(lines):
    lengths = [len(l) for l in lines]
    total_lines = len(lines)
    avg_line_len = sum(lengths) / total_lines if total_lines else 0.0
    if total_lines:
        mean = avg_line_len
        variance = sum((l - mean) ** 2 for l in lengths) / total_lines
        std_line_len = variance ** 0.5
    else:
        std_line_len = 0.0
    return avg_line_len, std_line_len


def _indent_stats(lines):
    indents = []
    for l in lines:
        if l.strip() == "":
            continue
        stripped = l.lstrip(" \t")
        indents.append(len(l) - len(stripped))
    if not indents:
        return 0.0
    mean = sum(indents) / len(indents)
    variance = sum((i - mean) ** 2 for i in indents) / len(indents)
    return variance ** 0.5


def _comment_ratio(lines, language):
    prefixes = _COMMENT_PREFIXES.get(language.lower(), ("//", "#"))
    comment_lines = sum(1 for l in lines if l.strip().startswith(prefixes))
    return comment_lines / len(lines) if lines else 0.0


def _blank_ratio(lines):
    if not lines:
        return 0.0
    blanks = sum(1 for l in lines if l.strip() == "")
    return blanks / len(lines)


def _token_features(code):
    tokens = _TOKEN_RE.findall(code)
    unique_ratio = len(set(tokens)) / len(tokens) if tokens else 0.0

    identifiers = _IDENTIFIER_RE.findall(code)
    avg_identifier_len = sum(len(i) for i in identifiers) / len(identifiers) if identifiers else 0.0

    n_operators = len(_OPERATOR_RE.findall(code))
    return unique_ratio, avg_identifier_len, n_operators


def _brace_depth(code):
    depth = 0
    max_depth = 0
    n_braces = 0
    for ch in code:
        if ch == "{":
            depth += 1
            n_braces += 1
            max_depth = max(max_depth, depth)
        elif ch == "}":
            depth = max(depth - 1, 0)
            n_braces += 1
    return max_depth, n_braces


def _keyword_count(code, keywords):
    total = 0
    for kw in keywords:
        total += len(re.findall(rf"\b{re.escape(kw)}\b", code))
    return total


def _struct_features(code, language):
    n_loops = _keyword_count(code, _LOOP_KEYWORDS)
    n_conditionals = _keyword_count(code, _CONDITIONAL_KEYWORDS)
    n_returns = _keyword_count(code, _RETURN_KEYWORDS)

    n_func_defs = 0
    for pattern in _FUNC_DEF_PATTERNS:
        n_func_defs += len(pattern.findall(code))

    n_calls = len(re.findall(r"\b\w+\s*\(", code)) - n_func_defs
    n_calls = max(n_calls, 0)

    max_brace_depth, n_braces = _brace_depth(code)
    n_semicolons = code.count(";")

    cyclomatic = 1 + n_conditionals + n_loops + _keyword_count(code, ("and", "&&", "or", "||", "case"))

    return {
        "struct_n_loops": n_loops,
        "struct_n_conditionals": n_conditionals,
        "struct_n_returns": n_returns,
        "struct_n_func_defs": n_func_defs,
        "struct_n_calls": n_calls,
        "struct_max_brace_depth": max_brace_depth,
        "struct_n_semicolons": n_semicolons,
        "struct_n_braces": n_braces,
        "struct_cyclomatic": cyclomatic,
    }


def _ast_features_python(code):
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {"ast_max_depth": 0, "ast_total_nodes": 0, "ast_n_functions": 0, "ast_parse_failed": 1}

    total_nodes = 0
    n_functions = 0
    max_depth = 0

    def walk(node, depth):
        nonlocal total_nodes, n_functions, max_depth
        total_nodes += 1
        max_depth = max(max_depth, depth)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n_functions += 1
        for child in ast.iter_child_nodes(node):
            walk(child, depth + 1)

    walk(tree, 0)
    return {
        "ast_max_depth": max_depth,
        "ast_total_nodes": total_nodes,
        "ast_n_functions": n_functions,
        "ast_parse_failed": 0,
    }


def _ast_features_fallback(code, struct_features):
    return {
        "ast_max_depth": struct_features["struct_max_brace_depth"],
        "ast_total_nodes": struct_features["struct_n_braces"] + struct_features["struct_n_calls"],
        "ast_n_functions": struct_features["struct_n_func_defs"],
        "ast_parse_failed": 1,
    }


def extract_features(code: str, language: str) -> dict:
    """Returns the 23 features as a dict keyed by FEATURE_COLUMNS."""
    lines = code.split("\n")
    total_lines = len(lines)
    total_chars = len(code)

    avg_line_len, std_line_len = _line_stats(lines)
    indent_std = _indent_stats(lines)
    comment_ratio = _comment_ratio(lines, language)
    blank_ratio = _blank_ratio(lines)
    unique_token_ratio, avg_identifier_len, n_operators = _token_features(code)

    struct_features = _struct_features(code, language)

    if language.lower() == "python":
        ast_features = _ast_features_python(code)
    else:
        ast_features = _ast_features_fallback(code, struct_features)

    features = {
        "comment_ratio": comment_ratio,
        "avg_line_len": avg_line_len,
        "std_line_len": std_line_len,
        "indent_std": indent_std,
        "blank_ratio": blank_ratio,
        "unique_token_ratio": unique_token_ratio,
        "avg_identifier_len": avg_identifier_len,
        "total_lines": total_lines,
        "total_chars": total_chars,
        "struct_n_operators": n_operators,
        **struct_features,
        **ast_features,
    }
    return {col: features[col] for col in FEATURE_COLUMNS}
