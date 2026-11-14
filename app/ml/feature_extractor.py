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

MIN_MEANINGFUL_LINES = 10
BOILERPLATE_CHAR_RATIO = 0.6
_BOILERPLATE_CHARS = set("{}();,")

_CPP_PREPROCESSOR_RE = re.compile(r"^\s*#\s*(include|define|pragma|ifdef|endif|ifndef)\b")
_JAVA_BOILERPLATE_RE = re.compile(r"^\s*(public\s+class\b|public\s+static\s+void\s+main\b|import\s+java\.)")
_PY_MAIN_GUARD_RE = re.compile(r'^\s*if\s+__name__\s*==\s*[\'"]__main__[\'"]\s*:')


class InsufficientCodeError(Exception):
    """Not enough meaningful (non-blank, non-comment, non-boilerplate) code
    to run AI detection reliably on."""
    def __init__(self, message="Code too short or boilerplate-only for reliable analysis. Need 10+ meaningful lines."):
        super().__init__(message)
        self.message = message


def _strip_cpp_preprocessor(code: str) -> str:
    return "\n".join(
        line for line in code.split("\n") if not _CPP_PREPROCESSOR_RE.match(line)
    )


def _strip_python_main_block(lines):
    """Drop the `if __name__ == "__main__":` line and its indented body —
    boilerplate that shouldn't count toward "meaningful" code."""
    out = []
    skip_indent = None
    for line in lines:
        if skip_indent is not None:
            if line.strip() == "":
                continue
            indent = len(line) - len(line.lstrip(" \t"))
            if indent > skip_indent:
                continue
            skip_indent = None
        if _PY_MAIN_GUARD_RE.match(line):
            skip_indent = len(line) - len(line.lstrip(" \t"))
            continue
        out.append(line)
    return out


def _meaningful_lines(code_for_analysis: str, language: str) -> list:
    """Non-empty, non-comment, non-boilerplate lines — used only to gate
    whether there's enough code to analyze reliably. Doesn't change the
    trained feature vector itself."""
    lang = language.lower()
    lines = code_for_analysis.split("\n")

    if lang == "python":
        lines = _strip_python_main_block(lines)
    elif lang == "java":
        lines = [l for l in lines if not _JAVA_BOILERPLATE_RE.match(l)]

    prefixes = _COMMENT_PREFIXES.get(lang, ("//", "#"))
    return [l for l in lines if l.strip() != "" and not l.strip().startswith(prefixes)]


def _is_boilerplate_only(code_for_analysis: str) -> bool:
    non_ws = [c for c in code_for_analysis if not c.isspace()]
    if not non_ws:
        return True
    n_bracket = sum(1 for c in non_ws if c in _BOILERPLATE_CHARS)
    return (n_bracket / len(non_ws)) > BOILERPLATE_CHAR_RATIO


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
    """Returns the 23 features as a dict keyed by FEATURE_COLUMNS.

    Raises InsufficientCodeError if there isn't enough meaningful code
    (fewer than MIN_MEANINGFUL_LINES non-blank/non-comment/non-boilerplate
    lines, or the code is mostly brackets/semicolons) to analyze reliably.
    `code` is kept as-is for display elsewhere; only `code_for_analysis`
    (preprocessor-stripped, C/C++ only) feeds the feature vector.
    """
    lang = language.lower()
    code_for_analysis = code
    if lang in ("c", "cpp", "c++"):
        code_for_analysis = _strip_cpp_preprocessor(code)

    if len(_meaningful_lines(code_for_analysis, lang)) < MIN_MEANINGFUL_LINES:
        raise InsufficientCodeError()
    if _is_boilerplate_only(code_for_analysis):
        raise InsufficientCodeError(
            "Code is boilerplate-only (brackets/semicolons dominate) for reliable analysis."
        )

    lines = code_for_analysis.split("\n")
    total_lines = len(lines)
    total_chars = len(code_for_analysis)

    avg_line_len, std_line_len = _line_stats(lines)
    indent_std = _indent_stats(lines)
    comment_ratio = _comment_ratio(lines, language)
    blank_ratio = _blank_ratio(lines)
    unique_token_ratio, avg_identifier_len, n_operators = _token_features(code_for_analysis)

    struct_features = _struct_features(code_for_analysis, language)

    if lang == "python":
        ast_features = _ast_features_python(code_for_analysis)
    else:
        ast_features = _ast_features_fallback(code_for_analysis, struct_features)

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
