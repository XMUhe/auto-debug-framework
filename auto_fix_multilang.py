"""
多语言自动修复框架 - 支持 Python / Java / C / C++
优化：减少 token 消耗，批量修复，本地规则优先
"""
import subprocess
import tempfile
import os
import re
import json
# ============================================================
# LLM 查询函数（内置，无外部依赖）
# ============================================================
def get_llm_query_func(provider: str = "qwen"):
    """获取 LLM 查询函数
    
    Args:
        provider: 可选 'qwen', 'openai', 'kimi', 'claude'
    Returns:
        query_fn: 接受 prompt 返回响应的函数
    """
    # 清除代理设置
    for proxy_var in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy']:
        os.environ.pop(proxy_var, None)
    
    if provider in ["qwen", "openai", "kimi"]:
        api_key = os.environ.get("QWEN_API_KEY") or "sk-880819fa540242f8b45c6df18acca522"
        base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        model = "qwen-max"
        
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("请安装 openai: pip install openai")
        
        client = OpenAI(api_key=api_key, base_url=base_url)
        
        def query_fn(prompt):
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4096,
                temperature=0.7
            )
            return response.choices[0].message.content
        return query_fn
    
    elif provider == "claude":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("请设置环境变量 ANTHROPIC_API_KEY")
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("请安装 anthropic: pip install anthropic")
        
        client = anthropic.Anthropic(api_key=api_key)
        
        def query_fn(prompt):
            response = client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.content[0].text
        return query_fn
    
    else:
        raise ValueError(f"不支持的 provider: {provider}，可选: qwen, openai, kimi, claude")

# ============================================================
# 语言配置
# ============================================================
LANG_CONFIG = {
    "python": {
        "image": "python:3.10-slim",
        "suffix": ".py",
        "compile_cmd": None,
        "run_cmd": "python {file}",
        "linter": "ruff check {file} --select=E,F,W",
        "linter_fix": "ruff check {file} --fix --unsafe-fixes",
        "install_linter": "pip install ruff -q",
        "error_pattern": r"line (\d+)",
        "main_pattern": r'if __name__ == "__main__":',
        # 额外的修复工具
        "extra_fixers": [
            "ruff format {file}",  # 代码格式化
        ],
    },
    "java": {
        "image": "amazoncorretto:17",
        "suffix": ".java",
        "compile_cmd": "javac {file}",
        "run_cmd": "java -cp {dir} {classname}",
        "linter": "javac -Xlint:all {file} 2>&1",
        "linter_fix": None,  # google-java-format 需要下载，改为可选
        "install_linter": None,
        "error_pattern": r"(?::(\d+):|java:(\d+)\))",
        "main_pattern": r"public static void main",
        # 额外的修复工具和选项
        "extra_fixers": [],
        # 使用 ECJ 的 fixit 建议
        "fixit_cmd": None,
    },
    "c": {
        "image": "gcc:12",
        "suffix": ".c",
        "compile_cmd": "gcc -Wall -Wextra -o /tmp/out {file}",
        "run_cmd": "/tmp/out",
        "linter": "gcc -fsyntax-only -Wall -Wextra -fdiagnostics-parseable-fixits {file} 2>&1",
        "linter_fix": None,  # clang-tidy 安装太慢
        "install_linter": None,
        "error_pattern": r":(\d+):\d+:",
        "main_pattern": r"int main\s*\(",
        # clang 的 fixit 功能
        "fixit_cmd": "clang -fsyntax-only -fdiagnostics-parseable-fixits {file} 2>&1",
        "extra_fixers": [],
    },
    "cpp": {
        "image": "gcc:12",
        "suffix": ".cpp",
        "compile_cmd": "g++ -Wall -Wextra -o /tmp/out {file}",
        "run_cmd": "/tmp/out",
        "linter": "g++ -fsyntax-only -Wall -Wextra -fdiagnostics-parseable-fixits {file} 2>&1",
        "linter_fix": None,
        "install_linter": None,
        "error_pattern": r":(\d+):\d+:",
        "main_pattern": r"int main\s*\(",
        # clang++ 的 fixit 功能
        "fixit_cmd": "clang++ -fsyntax-only -fdiagnostics-parseable-fixits {file} 2>&1",
        "extra_fixers": [],
    },
}

# ============================================================
# 本地修复规则（免 LLM 调用）
# ============================================================
LOCAL_FIX_RULES = {
    "python": {
        # === 导入相关 ===
        r"F401.*'(\w+)' imported but unused": lambda m, code: remove_line_containing(code, f"import {m.group(1)}"),
        r"F811.*redefinition of unused": lambda m, code: code,  # 保留，可能是有意的
        r"F821.*undefined name": None,  # 需要 LLM
        r"F841.*local variable.*never used": lambda m, code: code,  # 可保留
        # === 【新增】依赖导入错误 ===
        r"ModuleNotFoundError: No module named '([\w.]+)'": lambda m, code: fix_python_module_not_found(code, m.group(1), m.string),
        r"ImportError: cannot import name '(\w+)' from '([\w.]+)'": lambda m, code: fix_python_import_error(code, m.group(1), m.group(2), m.string),
        r"ImportError: attempted relative import with no known parent package": lambda m, code: fix_python_relative_import(code, m.string),
        r"ImportError: attempted relative import beyond top-level package": lambda m, code: fix_python_relative_import(code, m.string),
        r"No module named '([\w_]+)'": lambda m, code: fix_python_module_not_found(code, m.group(1), m.string),
        # === 空白相关 ===
        r"W293.*whitespace": lambda m, code: code,  # ruff --fix 处理
        r"W291.*trailing whitespace": lambda m, code: code,  # ruff --fix 处理
        r"W292.*no newline": lambda m, code: code,  # ruff --fix 处理
        r"W391.*blank line at end": lambda m, code: code,  # ruff --fix 处理
        # === 语法错误（本地规则优先） ===
        # 【冒号缺失】
        r"SyntaxError: expected ':'": lambda m, code: fix_python_missing_colon(code, extract_line_num(m.string)),
        r"expected ':'": lambda m, code: fix_python_missing_colon(code, extract_line_num(m.string)),
        r"SyntaxError: invalid syntax.*\n.*\^\s*$": lambda m, code: fix_python_missing_colon(code, extract_line_num(m.string)),  # py_compile 格式
        r"expected '\('": lambda m, code: fix_missing_paren(code, extract_line_num(m.string), '('),
        # 【括号未闭合】
        r"SyntaxError.*was never closed": lambda m, code: fix_python_unclosed_bracket(code, m.string),
        r"'\(' was never closed": lambda m, code: fix_python_unclosed_bracket(code, m.string),
        r"'\[' was never closed": lambda m, code: fix_python_unclosed_bracket(code, m.string),
        r"'\{' was never closed": lambda m, code: fix_python_unclosed_bracket(code, m.string),
        r"unmatched '\)'": lambda m, code: fix_python_extra_bracket(code, extract_line_num(m.string), ')'),
        r"unmatched '\]'": lambda m, code: fix_python_extra_bracket(code, extract_line_num(m.string), ']'),
        r"unmatched '\}'": lambda m, code: fix_python_extra_bracket(code, extract_line_num(m.string), '}'),
        r"IndentationError": lambda m, code: fix_python_indentation(code, extract_line_num(m.string)),
        r"unexpected indent": lambda m, code: fix_python_unexpected_indent(code, extract_line_num(m.string)),
        r"expected an indented block": lambda m, code: fix_python_expected_indent(code, extract_line_num(m.string)),
        r"unindent does not match": lambda m, code: fix_python_unindent_mismatch(code, extract_line_num(m.string)),
        r"E999.*SyntaxError": lambda m, code: fix_python_syntax_error(code, m.string),  # 通用语法错误
        # === 新增：引号/字符串错误 ===
        r"EOL while scanning string literal": lambda m, code: fix_python_unclosed_string(code, extract_line_num(m.string)),
        r"unterminated string literal": lambda m, code: fix_python_unclosed_string(code, extract_line_num(m.string)),
        r"invalid syntax.*Perhaps you forgot a comma": lambda m, code: fix_python_missing_comma(code, extract_line_num(m.string)),
        # === 新增：返回语句错误 ===
        r"'return' outside function": lambda m, code: fix_python_return_outside_func(code, extract_line_num(m.string)),
        r"'break' outside loop": lambda m, code: fix_python_break_outside_loop(code, extract_line_num(m.string)),
        r"'continue' not properly in loop": lambda m, code: fix_python_continue_outside_loop(code, extract_line_num(m.string)),
        # === 新增：赋值/比较错误 ===
        r"cannot assign to": lambda m, code: fix_python_invalid_assignment(code, extract_line_num(m.string)),
        r"invalid syntax.*==": lambda m, code: fix_python_eq_to_assign(code, extract_line_num(m.string)),  # 条件中用了 =
        # === 新增：参数错误 ===
        r"non-default argument follows default argument": lambda m, code: fix_python_arg_order(code, extract_line_num(m.string)),
        r"duplicate argument": lambda m, code: code,  # 需要手动修复
        # === 原有规则 ===
        r"E501.*line too long": lambda m, code: code,  # 可忽略
        r"E711.*comparison to None": lambda m, code: fix_none_comparison(code, extract_line_num(m.string)),
        r"E712.*comparison to True": lambda m, code: fix_bool_comparison(code, extract_line_num(m.string)),
        r"E713.*not in test": lambda m, code: fix_not_in_test(code, extract_line_num(m.string)),
        r"E721.*type comparison": lambda m, code: code,  # 可忽略
        # === 缩进相关 ===
        r"E101.*indentation contains mixed": lambda m, code: fix_mixed_indent(code),
        r"E111.*indentation is not a multiple": lambda m, code: code,  # ruff --fix
        r"E117.*over-indented": lambda m, code: code,  # ruff --fix
        # === 冒号/括号 ===
        r"E701.*multiple statements on one line.*colon": lambda m, code: code,
        r"E702.*multiple statements on one line.*semicolon": lambda m, code: code,
        r"E703.*statement ends with semicolon": lambda m, code: fix_trailing_semicolon(code, extract_line_num(m.string)),
    },
    "java": {
        # ============================================================
        # [语法错误] - 代码不符合语法规则
        # ============================================================
        # === 分号相关 ===
        r"';' expected": lambda m, code: add_semicolon_at_line(code, extract_line_num(m.string)),
        r"illegal start of expression": lambda m, code: add_semicolon_at_line(code, extract_line_num(m.string) - 1),
        r"illegal start of type": lambda m, code: add_semicolon_at_line(code, extract_line_num(m.string) - 1),
        # === 括号相关 ===
        r"\) expected": lambda m, code: fix_missing_paren(code, extract_line_num(m.string), ')'),
        r"\( expected": lambda m, code: fix_missing_paren(code, extract_line_num(m.string), '('),
        r"'}' expected": lambda m, code: fix_missing_brace(code, extract_line_num(m.string)),
        r"'\{' expected": lambda m, code: fix_missing_open_brace(code, extract_line_num(m.string)),
        r"reached end of file while parsing": lambda m, code: fix_eof_brace(code),
        r"'\]' expected": lambda m, code: fix_missing_bracket(code, extract_line_num(m.string), ']'),
        r"'\[' expected": lambda m, code: fix_missing_bracket(code, extract_line_num(m.string), '['),
        # === 字符串相关 ===
        r"unclosed string literal": lambda m, code: fix_unclosed_string(code, extract_line_num(m.string)),
        r"unclosed character literal": lambda m, code: fix_unclosed_string(code, extract_line_num(m.string)),
        r"empty character literal": lambda m, code: fix_empty_char_literal(code, extract_line_num(m.string)),
        # === 类名相关 ===
        r"should be declared in a file named": lambda m, code: fix_java_classname(code),
        r"class .* is public, should be declared": lambda m, code: fix_java_classname(code),
        # === 控制流语法 ===
        r"'else' without 'if'": lambda m, code: fix_else_without_if(code, extract_line_num(m.string)),
        # === 标识符语法 ===
        r"<identifier> expected": lambda m, code: add_semicolon_at_line(code, extract_line_num(m.string) - 1),
        r"not a statement": lambda m, code: add_semicolon_at_line(code, extract_line_num(m.string) - 1),
        # === 方法语法 ===
        r"missing method body": lambda m, code: fix_java_missing_method_body(code, extract_line_num(m.string)),
        r"return type required": lambda m, code: fix_java_return_type(code, extract_line_num(m.string)),
        # ============================================================
        # [语义错误] - 语法正确但含义有问题
        # ============================================================
        # === 变量语义 ===
        r"duplicate class": lambda m, code: code,  # [语义] 重复定义
        r"variable .* is already defined": lambda m, code: code,  # [语义] 重复定义
        r"variable .* might not have been initialized": lambda m, code: fix_java_init_variable(code, m.string),  # [语义] 可自动修复
        r"variable .* might already have been assigned": lambda m, code: code,  # [语义] final相关
        # === 类型语义 ===
        r"incompatible types": None,  # [语义] 需要 LLM
        r"cannot convert": None,  # [语义] 需要 LLM
        r"bad operand type": None,  # [语义] 需要 LLM
        r"inconvertible types": None,  # [语义] 需要 LLM
        # === 方法语义 ===
        r"missing return statement": lambda m, code: fix_java_missing_return(code, extract_line_num(m.string)),  # [语义] 可自动修复
        r"method .* in class .* cannot be applied": None,  # [语义] 需要 LLM
        # === 控制流语义 ===
        r"unreachable statement": lambda m, code: fix_unreachable_statement(code, extract_line_num(m.string)),  # [语义] 可自动修复
        r"'break' outside switch or loop": lambda m, code: fix_unreachable_statement(code, extract_line_num(m.string)),  # [语义]
        r"'continue' outside of loop": lambda m, code: fix_unreachable_statement(code, extract_line_num(m.string)),  # [语义]
        # === 访问控制语义 ===
        r"has private access": None,  # [语义] 需要 LLM
        r"is not public": None,  # [语义] 需要 LLM
        r"cannot be accessed from outside": None,  # [语义] 需要 LLM
        # === 静态上下文语义 ===
        r"non-static .* cannot be referenced from a static context": lambda m, code: fix_java_static_context(code, m.string),  # [语义]
        r"non-static method .* cannot be referenced": lambda m, code: fix_java_static_context(code, m.string),  # [语义]
        # === 抽象/接口语义 ===
        r"is abstract; cannot be instantiated": None,  # [语义] 需要 LLM
        r"abstract methods cannot have a body": lambda m, code: fix_java_abstract_body(code, extract_line_num(m.string)),  # [语义] 可自动修复
        r"does not override or implement": None,  # [语义] 需要 LLM
        # === 异常语义 ===
        r"unreported exception": None,  # [语义] 需要 LLM
        r"exception .* is never thrown": lambda m, code: fix_java_remove_throws(code, m.string),  # [语义] 可自动修复
        # === 符号解析语义 ===
        r"cannot find symbol": lambda m, code: fix_java_cannot_find_symbol(code, m.string),  # [语义] 尝试修复
        r"package .* does not exist": lambda m, code: fix_java_missing_import(code, m.string),  # [语义] 尝试添加 import
        r"cannot access .*": None,  # [语义] 需要 LLM
        # === 新增：数组相关 ===
        r"array required, but .* found": lambda m, code: fix_java_array_access(code, extract_line_num(m.string)),
        r"cannot convert .* to .*\[\]": None,  # [语义] 需要 LLM
        # === 新增：常量表达式 ===
        r"constant expression required": None,  # [语义] 需要 LLM
        r"case label must be constant": None,  # [语义] 需要 LLM
        # === 新增：构造函数 ===
        r"constructor .* cannot be applied": lambda m, code: fix_java_constructor_args(code, m.string),
        r"no suitable constructor found": None,  # [语义] 需要 LLM
        r"call to super must be first statement": lambda m, code: fix_java_super_first(code, extract_line_num(m.string)),
        # === 新增：泛型 ===
        r"generic array creation": None,  # [语义] 需要 LLM
        r"cannot infer type arguments": None,  # [语义] 需要 LLM
        # === 注解语义 ===
        r"annotation .* is missing": None,  # [语义] 需要 LLM
        r"@Override .* does not override": lambda m, code: fix_java_remove_override(code, extract_line_num(m.string)),  # [语义] 可自动修复
    },
    "c": {
        # ============================================================
        # [语法错误] - 代码不符合语法规则
        # ============================================================
        # === 分号语法 ===
        r"expected ';'": lambda m, code: add_semicolon_at_line(code, extract_line_num(m.string)),
        r"expected ';' before": lambda m, code: add_semicolon_at_line(code, extract_line_num(m.string) - 1),
        r"expected ';' at end of": lambda m, code: add_semicolon_at_line(code, extract_line_num(m.string)),
        r"multiple types in one declaration": lambda m, code: fix_brace_semicolon(code, extract_line_num(m.string)),
        # === 括号语法 ===
        r"expected '\)'": lambda m, code: fix_missing_paren(code, extract_line_num(m.string), ')'),
        r"expected '\('": lambda m, code: fix_missing_paren(code, extract_line_num(m.string), '('),
        r"expected '}'": lambda m, code: fix_missing_brace(code, extract_line_num(m.string)),
        r"expected '\{'": lambda m, code: fix_missing_open_brace(code, extract_line_num(m.string)),
        r"expected '\}' before 'else'": lambda m, code: fix_brace_before_else(code, extract_line_num(m.string)),
        r"expected '\}' at end of input": lambda m, code: fix_eof_brace(code),
        r"expected '\]'": lambda m, code: fix_missing_bracket(code, extract_line_num(m.string), ']'),
        # === 字符串语法 ===
        r"unterminated": lambda m, code: fix_unclosed_string(code, extract_line_num(m.string)),
        r"missing terminating": lambda m, code: fix_unclosed_string(code, extract_line_num(m.string)),
        r"empty character constant": lambda m, code: fix_empty_char_literal(code, extract_line_num(m.string)),
        # === 控制流语法 ===
        r"'else' without a previous 'if'": lambda m, code: fix_else_without_if(code, extract_line_num(m.string)),
        # === 标识符语法 ===
        r"expected identifier": lambda m, code: add_semicolon_at_line(code, extract_line_num(m.string) - 1),
        r"expected ',' or '\.\.\.'": lambda m, code: fix_missing_comma(code, extract_line_num(m.string)),
        r"expected '=' before": lambda m, code: fix_missing_equals(code, extract_line_num(m.string)),
        r"expected declaration before '\}'": lambda m, code: fix_stray_brace(code, extract_line_num(m.string)),
        # === 杂项语法 ===
        r"stray '\\\\' in program": lambda m, code: fix_stray_backslash(code, extract_line_num(m.string)),
        r"stray '#' in program": lambda m, code: fix_stray_hash(code, extract_line_num(m.string)),
        # ============================================================
        # [语义错误] - 语法正确但含义有问题
        # ============================================================
        # === 符号解析语义 ===
        r"implicit declaration": lambda m, code: fix_c_implicit_declaration(code, m.string),  # [语义] 尝试添加声明
        r"undeclared": lambda m, code: fix_c_undeclared(code, m.string),  # [语义] 尝试添加声明
        r"unknown type name": lambda m, code: fix_c_unknown_type(code, m.string),  # [语义] 尝试添加 include
        # === 类型语义 ===
        r"conflicting types": None,  # [语义] 需要 LLM
        r"redefinition of": None,  # [语义] 需要 LLM
        # === 新增：指针相关 ===
        r"assignment makes pointer from integer": lambda m, code: fix_c_pointer_cast(code, extract_line_num(m.string)),
        r"assignment makes integer from pointer": lambda m, code: fix_c_pointer_cast(code, extract_line_num(m.string)),
        r"incompatible pointer type": None,  # [语义] 需要 LLM
        r"dereferencing pointer to incomplete type": None,  # [语义] 需要 LLM
        # === 新增：函数相关 ===
        r"too few arguments to function": lambda m, code: fix_c_function_args(code, m.string, 'few'),
        r"too many arguments to function": lambda m, code: fix_c_function_args(code, m.string, 'many'),
        r"control reaches end of non-void function": lambda m, code: fix_c_missing_return(code, extract_line_num(m.string)),
        # === 新增：警告转错误 ===
        r"unused variable": lambda m, code: fix_c_unused_variable(code, m.string),
        r"unused parameter": lambda m, code: code,  # 可忽略
        r"comparison between signed and unsigned": lambda m, code: code,  # 可忽略
    },
    "cpp": {
        # ============================================================
        # [语法错误] - 代码不符合语法规则
        # ============================================================
        # === 分号语法 ===
        r"expected ';' before '}'": lambda m, code: fix_semicolon_before_brace(code, extract_line_num(m.string)),
        r"expected ';'": lambda m, code: add_semicolon_at_line(code, extract_line_num(m.string)),
        r"expected ';' at end of": lambda m, code: add_semicolon_at_line(code, extract_line_num(m.string)),
        r"multiple types in one declaration": lambda m, code: fix_brace_semicolon(code, extract_line_num(m.string)),
        r"',' or ';' before": lambda m, code: add_semicolon_at_line(code, extract_line_num(m.string) - 1),
        # === 括号语法 ===
        r"expected '\)' before '\{'": lambda m, code: fix_paren_before_brace(code, extract_line_num(m.string)),
        r"expected '\)' before '\['": lambda m, code: fix_paren_before_bracket(code, extract_line_num(m.string)),
        r"expected '\)'": lambda m, code: fix_missing_paren(code, extract_line_num(m.string), ')'),
        r"expected '\('": lambda m, code: fix_missing_paren(code, extract_line_num(m.string), '('),
        r"expected '}'": lambda m, code: fix_missing_brace(code, extract_line_num(m.string)),
        r"expected '\{'": lambda m, code: fix_missing_open_brace(code, extract_line_num(m.string)),
        r"expected '\}' before 'else'": lambda m, code: fix_brace_before_else(code, extract_line_num(m.string)),
        r"expected '\}' at end of input": lambda m, code: fix_eof_brace(code),
        r"expected '\]'": lambda m, code: fix_missing_bracket(code, extract_line_num(m.string), ']'),
        r"expected unqualified-id before '\)'": lambda m, code: fix_stray_paren(code, extract_line_num(m.string)),
        r"expected unqualified-id before '\('": lambda m, code: fix_stray_paren(code, extract_line_num(m.string)),
        r"expected unqualified-id before '\{'": lambda m, code: fix_stray_brace(code, extract_line_num(m.string)),
        r"expected declaration before '\}'": lambda m, code: fix_stray_brace(code, extract_line_num(m.string)),
        r"expected primary-expression before '\}'": lambda m, code: fix_empty_statement(code, extract_line_num(m.string)),
        r"expected primary-expression before '\)'": lambda m, code: fix_empty_expression(code, extract_line_num(m.string)),
        r"expected primary-expression before ';'": lambda m, code: fix_empty_expression(code, extract_line_num(m.string)),
        # === 字符串语法 ===
        r"unterminated": lambda m, code: fix_unclosed_string(code, extract_line_num(m.string)),
        r"missing terminating": lambda m, code: fix_unclosed_string(code, extract_line_num(m.string)),
        r"empty character constant": lambda m, code: fix_empty_char_literal(code, extract_line_num(m.string)),
        # === 作用域语法 ===
        r"found ':' in nested-name-specifier, expected '::'" : lambda m, code: fix_single_colon(code, extract_line_num(m.string)),
        r"extra qualification": lambda m, code: fix_extra_qualification(code, m.string),
        r"qualified-id in declaration before": lambda m, code: fix_qualified_declaration(code, extract_line_num(m.string)),
        # === 控制流语法 ===
        r"'else' without a previous 'if'": lambda m, code: fix_else_without_if(code, extract_line_num(m.string)),
        # === 标识符语法 ===
        r"expected identifier before numeric constant": lambda m, code: fix_identifier_before_number(code, extract_line_num(m.string)),
        r"expected ',' or '\.\.\.'": lambda m, code: fix_missing_comma(code, extract_line_num(m.string)),
        r"expected '=' before": lambda m, code: fix_missing_equals(code, extract_line_num(m.string)),
        r"expected initializer before": lambda m, code: fix_missing_initializer(code, extract_line_num(m.string)),
        # === 杂项语法 ===
        r"stray '\\\\' in program": lambda m, code: fix_stray_backslash(code, extract_line_num(m.string)),
        r"stray '#' in program": lambda m, code: fix_stray_hash(code, extract_line_num(m.string)),
        r"extraneous closing brace": lambda m, code: fix_stray_brace(code, extract_line_num(m.string)),
        r"invalid preprocessing directive": lambda m, code: fix_invalid_directive(code, extract_line_num(m.string)),
        r"unterminated #": lambda m, code: fix_unterminated_directive(code, extract_line_num(m.string)),
        # ============================================================
        # [语义错误] - 语法正确但含义有问题
        # ============================================================
        # === 符号解析语义 ===
        r"was not declared in this scope": lambda m, code: fix_undeclared_symbol(code, m.string),  # [语义] 尝试std::
        r"has not been declared": lambda m, code: fix_undeclared_class(code, m.string),  # [语义] 尝试std::
        r"has no member named": lambda m, code: fix_no_member(code, m.string),  # [语义]
        r"does not name a type": lambda m, code: fix_type_not_found(code, m.string),  # [语义] 尝试std::
        # === 类型语义 ===
        r"conflicting types": None,  # [语义] 需要 LLM
        r"redefinition of": None,  # [语义] 需要 LLM
        r"unknown type name": None,  # [语义] 需要 LLM
        r"no matching function": None,  # [语义] 需要 LLM
        r"cannot convert": None,  # [语义] 需要 LLM
        r"invalid operands": None,  # [语义] 需要 LLM
        # === 模板语义 ===
        r"need 'typename' before": lambda m, code: fix_add_typename(code, m.string),  # [语义] 可自动修复
        r"expected 'template' keyword": lambda m, code: fix_add_template_keyword(code, extract_line_num(m.string)),  # [语义]
        r"dependent-name .* parsed as a non-type": lambda m, code: fix_add_typename(code, m.string),  # [语义]
        r"'typename' cannot be used outside of a template": lambda m, code: fix_remove_typename(code, extract_line_num(m.string)),  # [语义]
        r"expected qualified name after 'typename'": lambda m, code: fix_typename_to_class(code, extract_line_num(m.string)),  # [语义]
        r"template argument.*involves template parameter": None,  # [语义] 需要 LLM
    },
}

# ============================================================
# 工具函数
# ============================================================
CONTAINER = "autodebug_sandbox"
WORKSPACE = "/workspace"
MAX_ITERATIONS = 50  # 增加迭代次数以修复更多错误
CONTEXT_LINES = 10  # 减少上下文行数（省 token）


def parse_gcc_fixits(error_output):
    """
    解析 GCC/Clang 的 fix-it hints 输出
    格式: fix-it:"file":{{line:col-line:col}}:"replacement"
    返回: [(line, col_start, col_end, replacement), ...]
    """
    fixits = []
    # 匹配 fix-it 提示
    pattern = r'fix-it:"[^"]+":\{(\d+):(\d+)-(\d+):(\d+)\}:"([^"]*)"'
    for match in re.finditer(pattern, error_output):
        line_start = int(match.group(1))
        col_start = int(match.group(2))
        line_end = int(match.group(3))
        col_end = int(match.group(4))
        replacement = match.group(5)
        # 解码转义字符
        replacement = replacement.replace('\\n', '\n').replace('\\t', '\t')
        fixits.append((line_start, col_start, line_end, col_end, replacement))
    return fixits


def apply_fixits(code, fixits):
    """
    应用 fix-it hints 到代码
    按行号和列号进行替换
    """
    if not fixits:
        return code, False
    
    lines = code.split('\n')
    applied = False
    
    # 按行号降序排列，从后往前应用避免位置偏移
    fixits_sorted = sorted(fixits, key=lambda x: (x[0], x[1]), reverse=True)
    
    for line_start, col_start, line_end, col_end, replacement in fixits_sorted:
        if line_start == line_end and 0 < line_start <= len(lines):
            line = lines[line_start - 1]
            # 应用单行替换
            if col_start <= len(line) + 1 and col_end <= len(line) + 1:
                new_line = line[:col_start-1] + replacement + line[col_end-1:]
                if new_line != line:
                    lines[line_start - 1] = new_line
                    print(f"  [FIXIT] 行 {line_start}: 应用修复建议")
                    applied = True
    
    return '\n'.join(lines), applied


def try_fixit_suggestions(code, error_output, lang):
    """
    尝试从编译器输出中提取并应用 fix-it 建议
    """
    if lang not in ['c', 'cpp']:
        return code, False
    
    fixits = parse_gcc_fixits(error_output)
    if fixits:
        print(f"  [FIXIT] 发现 {len(fixits)} 个编译器修复建议")
        return apply_fixits(code, fixits)
    return code, False


def extract_compiler_suggestions(error_output):
    """
    提取编译器的 "did you mean" 建议
    """
    suggestions = []
    # GCC/Clang: note: suggested alternative: 'xxx'
    pattern1 = r"note:.*suggested alternative.*?[\'\"]?(\w+)[\'\"]?"
    # GCC: error: ... did you mean '...'
    pattern2 = r"did you mean [\'\"]?(\w+)[\'\"]?"
    # Clang: use of undeclared identifier 'xxx'; did you mean 'yyy'?
    pattern3 = r"did you mean [\'\"]?(\w+)[\'\"]?\?"
    
    for pattern in [pattern1, pattern2, pattern3]:
        for match in re.finditer(pattern, error_output, re.IGNORECASE):
            suggestions.append(match.group(1))
    return suggestions


def smart_symbol_replacement(code, error_msg, suggestions):
    """
    智能替换未声明的符号（利用编译器建议）
    """
    if not suggestions:
        return code, False
    
    line_num = extract_line_num(error_msg)
    if not line_num:
        return code, False
    
    # 提取错误的符号
    match = re.search(r"'(\w+)' was not declared|'(\w+)' undeclared|use of undeclared identifier '(\w+)'", error_msg)
    if not match:
        return code, False
    
    wrong_symbol = match.group(1) or match.group(2) or match.group(3)
    if not wrong_symbol or not suggestions:
        return code, False
    
    suggested = suggestions[0]  # 使用第一个建议
    
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        new_line = re.sub(rf'\b{re.escape(wrong_symbol)}\b', suggested, line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [SUGGESTION] 替换 '{wrong_symbol}' -> '{suggested}': 第 {line_num} 行")
            return '\n'.join(lines), True
    
    return code, False

def detect_language(filename):
    """根据文件后缀检测语言"""
    ext = os.path.splitext(filename)[1].lower()
    ext_map = {".py": "python", ".java": "java", ".c": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp"}
    return ext_map.get(ext, "python")

def remove_line_containing(code, pattern):
    """删除包含指定模式的行"""
    lines = code.split('\n')
    return '\n'.join(line for line in lines if pattern not in line)

def add_semicolon_at_line(code, line_num):
    """在指定行末尾添加分号（处理注释情况）"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1].rstrip()
        # 跳过已有分号、花括号的行
        if line.endswith(';') or line.endswith('{') or line.endswith('}'):
            return code
        
        # 处理带注释的行：在注释前添加分号
        comment_pos = -1
        for marker in ['//', '/*', '#']:
            pos = line.find(marker)
            if pos != -1 and (comment_pos == -1 or pos < comment_pos):
                comment_pos = pos
        
        if comment_pos > 0:
            before = line[:comment_pos].rstrip()
            comment = line[comment_pos:]
            if before and not before.endswith(';'):
                lines[line_num - 1] = before + ';  ' + comment
        else:
            lines[line_num - 1] = line + ';'
    return '\n'.join(lines)

def extract_line_num(error_msg):
    """从错误信息提取行号"""
    match = re.search(r':(\d+):', error_msg)
    if match:
        return int(match.group(1))
    match = re.search(r'line (\d+)', error_msg)
    if match:
        return int(match.group(1))
    return None
def fix_brace_semicolon(code, line_num):
    """修复 enum/struct/class 末尾 } 后缺少分号"""
    if not line_num:
        return code
    lines = code.split('\n')
    # 向上查找只有 } 的行
    for i in range(line_num - 1, max(0, line_num - 10), -1):
        line = lines[i].strip()
        if line == '}':
            lines[i] = lines[i].rstrip() + ';'
            return '\n'.join(lines)
    return code


def fix_unclosed_string(code, line_num):
    """修复未关闭的字符串"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        # 统计引号数量
        double_quotes = line.count('"') - line.count('\\"')
        single_quotes = line.count("'") - line.count("\\'")
        if double_quotes % 2 == 1:
            lines[line_num - 1] = line.rstrip() + '"'
        elif single_quotes % 2 == 1:
            lines[line_num - 1] = line.rstrip() + "'"
    return '\n'.join(lines)


def fix_missing_paren(code, line_num, paren):
    """修复缺少括号（智能处理 { 前的情况）"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1].rstrip()
        
        # 如果是右括号且行中有 {，使用智能修复
        if paren == ')' and '{' in line:
            # 先移除错误的 {; 
            cleaned_line = re.sub(r'\{\s*;', '{', line)
            
            # 查找行中最后一个 { 的位置
            brace_pos = cleaned_line.rfind('{')
            if brace_pos > 0:
                before_brace = cleaned_line[:brace_pos].rstrip()
                after_brace = cleaned_line[brace_pos:]
                # 检查 { 前是否缺少 )
                open_count = before_brace.count('(') - before_brace.count(')')
                if open_count > 0:
                    # 在 { 前添加缺少的 )
                    new_line = before_brace + ')' * open_count + ' ' + after_brace
                    lines[line_num - 1] = new_line
                    return '\n'.join(lines)
            
            # 如果只是清理了 {;，也返回
            if cleaned_line != line:
                lines[line_num - 1] = cleaned_line
                return '\n'.join(lines)
        
        # 普通情况：在行末添加括号
        if not line.endswith(paren) and not line.endswith(';'):
            lines[line_num - 1] = line + paren
    return '\n'.join(lines)


def fix_missing_brace(code, line_num):
    """修复缺少右花括号 }"""
    if not line_num:
        return code
    lines = code.split('\n')
    # 在文件末尾添加 }
    if lines and not lines[-1].strip() == '}':
        lines.append('}')
    return '\n'.join(lines)


def fix_missing_open_brace(code, line_num):
    """修复缺少左花括号 {"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1].rstrip()
        # 在行末添加 {
        if not line.endswith('{') and not line.endswith(';'):
            lines[line_num - 1] = line + ' {'
            print(f"  [LOCAL FIX] 在行末添加 {{: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_java_classname(code):
    """修复 Java 类名与文件名不匹配（改为 code）"""
    # 查找 public class XXX 并替换为 public class code
    new_code = re.sub(r'public\s+class\s+(\w+)', 'public class code', code, count=1)
    if new_code != code:
        print(f"  [LOCAL FIX] 修复 Java 类名 -> code")
    return new_code


def fix_single_colon(code, line_num):
    """修复单冒号为双冒号或构造函数初始化列表缺少右括号"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        
        # 情况1: 构造函数初始化列表缺少右括号
        # 检查上一行是否以 ( 开始但没有 )
        if line_num > 1:
            prev_line = lines[line_num - 2].rstrip()
            # 检查是否是构造函数参数列表未结束
            if '(' in prev_line and ')' not in prev_line and line.strip().startswith(':'):
                # 在上一行末尾添加 )
                lines[line_num - 2] = prev_line + ')'
                print(f"  [LOCAL FIX] 在构造函数参数列表末尾添加 ): 第 {line_num - 1} 行")
                return '\n'.join(lines)
        
        # 情况2: Class:Method 模式，改为 Class::Method
        new_line = re.sub(r'(\w+):(\w+)\s*\(', r'\1::\2(', line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 修复单冒号为双冒号: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_stray_paren(code, line_num):
    """修复孤立的 ) 或 } 符号（LLM 引入的错误）"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1].strip()
        # 只删除整行只有 ) 或 } 的情况
        if line == ')' or line == '}':
            print(f"  [LOCAL FIX] 删除孤立的 '{line}': 第 {line_num} 行")
            del lines[line_num - 1]
            return '\n'.join(lines)
        # 删除行首的孤立 )
        if line.startswith(')') and not re.match(r'\)\s*[{;]', line):
            new_line = lines[line_num - 1].lstrip()[1:]  # 移除第一个 )
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 删除行首孤立的 ')': 第 {line_num} 行")
            return '\n'.join(lines)
    return code


def fix_stray_brace(code, line_num):
    """修复孤立的 } 符号（expected declaration before '}')"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1].strip()
        if line == '}':
            print(f"  [LOCAL FIX] 删除孤立的 '}}': 第 {line_num} 行")
            del lines[line_num - 1]
            return '\n'.join(lines)
    return code


def fix_empty_statement(code, line_num):
    """修复 } 前缺少表达式（可能是空语句）"""
    if not line_num:
        return code
    lines = code.split('\n')
    # 检查上一行是否为空或只有空格
    if line_num > 1:
        prev_idx = line_num - 2
        if lines[prev_idx].strip() == '':
            # 删除空行
            del lines[prev_idx]
            print(f"  [LOCAL FIX] 删除空行: 第 {line_num - 1} 行")
            return '\n'.join(lines)
    return code


def fix_brace_before_else(code, line_num):
    """修复 else 前缺少 }"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        # 在 else 前加上 }
        new_line = re.sub(r'(\s*)else', r'\1} else', line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 在 else 前添加 }}: 第 {line_num} 行")
            return '\n'.join(lines)
    return code


def fix_paren_before_bracket(code, line_num):
    """修复 [ 前缺少 )"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        # 查找括号不匹配的情况
        open_count = line.count('(') - line.count(')')
        if open_count > 0 and '[' in line:
            # 在第一个 [ 前添加 )
            new_line = re.sub(r'(\s*)\[', ')\\1[', line, count=1)
            if new_line != line:
                lines[line_num - 1] = new_line
                print(f"  [LOCAL FIX] 在 [ 前添加 ): 第 {line_num} 行")
                return '\n'.join(lines)
    return code


def fix_extra_qualification(code, error_msg):
    """修复多余的类限定符 (ClassName::ClassName::method -> ClassName::method)"""
    line_num = extract_line_num(error_msg)
    match = re.search(r"'(\w+)::(\w+)::", error_msg)
    if not line_num or not match:
        return code
    class_name = match.group(1)
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        # 移除重复的类名限定
        pattern = rf'{class_name}::{class_name}::'
        new_line = line.replace(pattern, f'{class_name}::')
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 移除多余类限定: 第 {line_num} 行")
            return '\n'.join(lines)
    return code


def fix_stray_backslash(code, line_num):
    """修复游离的反斜杠"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        # 移除行末的反斜杠（不在字符串内）
        new_line = re.sub(r'\\\s*$', '', line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 移除游离反斜杠: 第 {line_num} 行")
            return '\n'.join(lines)
    return code


def fix_missing_initializer(code, line_num):
    """修复缺少初始化器（通常是缺分号或等号）"""
    if not line_num:
        return code
    # 尝试在上一行添加分号
    return add_semicolon_at_line(code, line_num - 1)


def fix_eof_brace(code):
    """修复文件末尾缺少 } (reached end of file while parsing)"""
    lines = code.split('\n')
    # 统计花括号
    open_count = code.count('{') - code.count('}')
    if open_count > 0:
        # 添加缺少的 }
        for _ in range(open_count):
            lines.append('}')
        print(f"  [LOCAL FIX] 在文件末尾添加 {open_count} 个 }}")
    return '\n'.join(lines)


def fix_missing_bracket(code, line_num, bracket):
    """修复缺少方括号 [ 或 ]"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1].rstrip()
        if not line.endswith(bracket):
            lines[line_num - 1] = line + bracket
            print(f"  [LOCAL FIX] 在行末添加 '{bracket}': 第 {line_num} 行")
    return '\n'.join(lines)


def fix_empty_char_literal(code, line_num):
    """修复空字符常量 '' -> ' '"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        # 替换空字符 '' 为空格 ' '
        new_line = re.sub(r"''", "' '", line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 修复空字符常量: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_else_without_if(code, line_num):
    """修复 else 没有对应的 if（通常是上一行缺 })"""
    if not line_num:
        return code
    lines = code.split('\n')
    if line_num > 1:
        # 在 else 上一行添加 }
        prev_line = lines[line_num - 2].rstrip()
        if not prev_line.endswith('}'):
            lines[line_num - 2] = prev_line + ' }'
            print(f"  [LOCAL FIX] 在 else 前添加 }}: 第 {line_num - 1} 行")
    return '\n'.join(lines)


def fix_unreachable_statement(code, line_num):
    """修复不可达语句（注释掉该行）"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        # 注释掉不可达语句
        lines[line_num - 1] = '// ' + line.lstrip()
        print(f"  [LOCAL FIX] 注释不可达语句: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_missing_comma(code, line_num):
    """修复缺少逗号"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1].rstrip()
        # 在行末添加逗号（如果不以 , ; { } 结尾）
        if not re.search(r'[,;{}]\s*$', line):
            lines[line_num - 1] = line + ','
            print(f"  [LOCAL FIX] 在行末添加逗号: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_missing_equals(code, line_num):
    """修复缺少等号"""
    if not line_num:
        return code
    # 大多数情况是上一行缺分号
    return add_semicolon_at_line(code, line_num - 1)


def fix_stray_hash(code, line_num):
    """修复游离的 # 符号"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        # 删除行中游离的 #
        new_line = re.sub(r'#(?!include|define|ifdef|ifndef|endif|else|elif|pragma|error|warning)', '', line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 移除游离 # 符号: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_empty_expression(code, line_num):
    """修复 ) 或 ; 前缺少表达式"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        # 处理 func(, xxx) -> func(xxx)
        new_line = re.sub(r'\(\s*,', '(', line)
        # 处理 func(xxx, ) -> func(xxx)
        new_line = re.sub(r',\s*\)', ')', new_line)
        # 处理 ;; -> ;
        new_line = re.sub(r';\s*;', ';', new_line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 修复空表达式: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_qualified_declaration(code, line_num):
    """修复声明中的限定符问题"""
    # 通常是上一行缺分号
    return add_semicolon_at_line(code, line_num - 1) if line_num else code


def fix_type_not_found(code, error_msg):
    """修复类型未找到 (xxx does not name a type)"""
    line_num = extract_line_num(error_msg)
    match = re.search(r"'(\w+)' does not name a type", error_msg)
    if not match or not line_num:
        return code
    
    type_name = match.group(1)
    # 常见的 std 类型
    std_types = ['string', 'vector', 'map', 'set', 'list', 'pair', 'tuple', 'array', 
                 'queue', 'stack', 'deque', 'unordered_map', 'unordered_set',
                 'shared_ptr', 'unique_ptr', 'function', 'optional', 'variant']
    
    if type_name in std_types:
        lines = code.split('\n')
        if 0 < line_num <= len(lines):
            line = lines[line_num - 1]
            # 替换为 std:: 前缀
            new_line = re.sub(rf'\b(?<!std::){type_name}\b', f'std::{type_name}', line)
            if new_line != line:
                lines[line_num - 1] = new_line
                print(f"  [LOCAL FIX] 添加 std:: 前缀: {type_name} -> std::{type_name}")
                return '\n'.join(lines)
    return code


def fix_identifier_before_number(code, line_num):
    """修复数字前缺少标识符 (int 123abc -> int _123abc)"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        # 给数字开头的标识符加下划线前缀
        new_line = re.sub(r'\b(\d+\w*)', r'_\1', line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 修复数字开头的标识符: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_invalid_directive(code, line_num):
    """修复无效的预处理指令"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        # 注释掉无效指令
        if line.strip().startswith('#'):
            lines[line_num - 1] = '// ' + line.lstrip()
            print(f"  [LOCAL FIX] 注释无效预处理指令: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_unterminated_directive(code, line_num):
    """修复未结束的预处理指令"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1].rstrip()
        # 移除行末的反斜杠
        if line.endswith('\\'):
            lines[line_num - 1] = line[:-1]
            print(f"  [LOCAL FIX] 移除行末反斜杠: 第 {line_num} 行")
    return '\n'.join(lines)


# ============================================================
# Python 特有修复函数
# ============================================================
def fix_python_missing_colon(code, line_num):
    """修复 Python 缺少冒号 (if/for/while/def/class/try/except/finally/with/elif/else)"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1].rstrip()
        # 检查是否是需要冒号的语句
        keywords = ['if', 'elif', 'else', 'for', 'while', 'def', 'class', 'try', 'except', 'finally', 'with']
        stripped = line.lstrip()
        for kw in keywords:
            if stripped.startswith(kw + ' ') or stripped.startswith(kw + '(') or stripped == kw or stripped == 'else' or stripped == 'try' or stripped == 'finally':
                if not line.endswith(':'):
                    # 在行末添加冒号
                    lines[line_num - 1] = line + ':'
                    print(f"  [LOCAL FIX] 添加缺少的冒号: 第 {line_num} 行")
                    return '\n'.join(lines)
        # 如果行末是右括号，可能是函数定义或 if 语句
        if line.endswith(')') and not line.endswith(':'):
            lines[line_num - 1] = line + ':'
            print(f"  [LOCAL FIX] 添加缺少的冒号: 第 {line_num} 行")
            return '\n'.join(lines)
    return code


def fix_python_unclosed_bracket(code, error_msg):
    """修复 Python 未关闭的括号 ('(' was never closed)"""
    # 提取行号
    line_num = extract_line_num(error_msg)
    if not line_num:
        return code
    
    lines = code.split('\n')
    if not (0 < line_num <= len(lines)):
        return code
    
    # 确定哪种括号未关闭
    bracket_pairs = {'(': ')', '[': ']', '{': '}'}
    for open_b, close_b in bracket_pairs.items():
        if f"'{open_b}' was never closed" in error_msg:
            # 从错误行开始往下查找应该添加闭合括号的位置
            target_line = line_num - 1
            line = lines[target_line]
            # 统计当前行的括号
            open_count = line.count(open_b) - line.count(close_b)
            if open_count > 0:
                # 在行末添加缺少的右括号
                stripped = line.rstrip()
                lines[target_line] = stripped + close_b * open_count
                print(f"  [LOCAL FIX] 添加缺少的 '{close_b}': 第 {line_num} 行")
                return '\n'.join(lines)
    
    return code


def fix_python_extra_bracket(code, line_num, bracket):
    """修复 Python 多余的括号 (unmatched ')')"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        # 移除多余的括号（从右向左第一个）
        idx = line.rfind(bracket)
        if idx >= 0:
            new_line = line[:idx] + line[idx+1:]
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 移除多余的 '{bracket}': 第 {line_num} 行")
    return '\n'.join(lines)


def fix_python_indentation(code, line_num):
    """修复 Python 缩进错误（通用）"""
    if not line_num:
        return code
    lines = code.split('\n')
    if not (0 < line_num <= len(lines)):
        return code
    
    # 获取上一行的缩进
    if line_num > 1:
        prev_line = lines[line_num - 2]
        prev_indent = len(prev_line) - len(prev_line.lstrip())
        # 如果上一行以冒号结尾，增加缩进
        if prev_line.rstrip().endswith(':'):
            prev_indent += 4
    else:
        prev_indent = 0
    
    current_line = lines[line_num - 1]
    current_content = current_line.lstrip()
    
    # 保留内容，调整缩进
    lines[line_num - 1] = ' ' * prev_indent + current_content
    print(f"  [LOCAL FIX] 调整缩进: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_python_unexpected_indent(code, line_num):
    """修复 Python unexpected indent（多余缩进）"""
    if not line_num:
        return code
    lines = code.split('\n')
    if not (0 < line_num <= len(lines)):
        return code
    
    # 获取上一行的缩进
    if line_num > 1:
        prev_line = lines[line_num - 2]
        prev_indent = len(prev_line) - len(prev_line.lstrip())
        # 如果上一行不以冒号结尾，保持同样缩进
        if not prev_line.rstrip().endswith(':'):
            current_line = lines[line_num - 1]
            current_content = current_line.lstrip()
            lines[line_num - 1] = ' ' * prev_indent + current_content
            print(f"  [LOCAL FIX] 去除多余缩进: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_python_expected_indent(code, line_num):
    """修复 Python expected an indented block（缺少缩进）"""
    if not line_num:
        return code
    lines = code.split('\n')
    if not (0 < line_num <= len(lines)):
        return code
    
    current_line = lines[line_num - 1]
    current_content = current_line.lstrip()
    
    # 获取上一行的缩进，然后增加 4 个空格
    if line_num > 1:
        prev_line = lines[line_num - 2]
        prev_indent = len(prev_line) - len(prev_line.lstrip())
        new_indent = prev_indent + 4
    else:
        new_indent = 4
    
    lines[line_num - 1] = ' ' * new_indent + current_content
    print(f"  [LOCAL FIX] 添加缩进: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_python_unindent_mismatch(code, line_num):
    """修复 Python unindent does not match（缩进不匹配）"""
    if not line_num:
        return code
    lines = code.split('\n')
    if not (0 < line_num <= len(lines)):
        return code
    
    # 往回查找有效的缩进级别
    current_line = lines[line_num - 1]
    current_content = current_line.lstrip()
    current_indent = len(current_line) - len(current_content)
    
    valid_indents = [0]
    for i in range(line_num - 2, -1, -1):
        prev_line = lines[i]
        if prev_line.strip():  # 跳过空行
            prev_indent = len(prev_line) - len(prev_line.lstrip())
            if prev_indent not in valid_indents:
                valid_indents.append(prev_indent)
            if prev_line.rstrip().endswith(':'):
                valid_indents.append(prev_indent + 4)
    
    # 找到最接近的有效缩进
    valid_indents = sorted(set(valid_indents))
    closest = min(valid_indents, key=lambda x: abs(x - current_indent))
    
    if closest != current_indent:
        lines[line_num - 1] = ' ' * closest + current_content
        print(f"  [LOCAL FIX] 修复缩进不匹配: 第 {line_num} 行 (调整为 {closest} 空格)")
    return '\n'.join(lines)


def fix_python_syntax_error(code, error_msg):
    """通用 Python 语法错误修复（根据错误信息尝试修复）"""
    line_num = extract_line_num(error_msg)
    if not line_num:
        return code
    
    lines = code.split('\n')
    if not (0 < line_num <= len(lines)):
        return code
    
    # 尝试各种常见修复
    line = lines[line_num - 1]
    
    # 1. 检查缺少冒号
    if 'expected' in error_msg.lower() and ':' in error_msg:
        code = fix_python_missing_colon(code, line_num)
        if code != '\n'.join(lines):
            return code
    
    # 2. 检查括号不匹配
    open_parens = line.count('(') - line.count(')')
    if open_parens > 0:
        lines[line_num - 1] = line.rstrip() + ')' * open_parens
        print(f"  [LOCAL FIX] 添加缺少的 ')': 第 {line_num} 行")
        return '\n'.join(lines)
    
    open_brackets = line.count('[') - line.count(']')
    if open_brackets > 0:
        lines[line_num - 1] = line.rstrip() + ']' * open_brackets
        print(f"  [LOCAL FIX] 添加缺少的 ']': 第 {line_num} 行")
        return '\n'.join(lines)
    
    return code


def fix_python_unclosed_string(code, line_num):
    """修复 Python 未关闭的字符串 (EOL while scanning string literal)"""
    if not line_num:
        return code
    lines = code.split('\n')
    if not (0 < line_num <= len(lines)):
        return code
    
    line = lines[line_num - 1]
    # 检查引号是否配对
    single_quotes = line.count("'") - line.count("\\'")
    double_quotes = line.count('"') - line.count('\\"')
    
    if single_quotes % 2 == 1:
        lines[line_num - 1] = line.rstrip() + "'"
        print(f"  [LOCAL FIX] 添加缺少的单引号: 第 {line_num} 行")
    elif double_quotes % 2 == 1:
        lines[line_num - 1] = line.rstrip() + '"'
        print(f"  [LOCAL FIX] 添加缺少的双引号: 第 {line_num} 行")
    
    return '\n'.join(lines)


def fix_python_missing_comma(code, line_num):
    """修复 Python 缺少逗号 (Perhaps you forgot a comma)"""
    if not line_num:
        return code
    lines = code.split('\n')
    if not (0 < line_num <= len(lines)):
        return code
    
    # 检查上一行是否需要逗号
    if line_num > 1:
        prev_line = lines[line_num - 2].rstrip()
        # 如果上一行以字符串、数字或变量结尾，可能需要逗号
        if prev_line and not prev_line.endswith((',', '(', '[', '{', ':')):
            lines[line_num - 2] = prev_line + ','
            print(f"  [LOCAL FIX] 添加缺少的逗号: 第 {line_num - 1} 行")
    
    return '\n'.join(lines)


def fix_python_return_outside_func(code, line_num):
    """修复 'return' outside function - 注释掉错误行"""
    if not line_num:
        return code
    lines = code.split('\n')
    if not (0 < line_num <= len(lines)):
        return code
    
    line = lines[line_num - 1]
    # 注释掉错误的 return 语句
    lines[line_num - 1] = '# ' + line.lstrip()  # 注释并保持原有内容
    print(f"  [LOCAL FIX] 注释掉函数外的 return: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_python_break_outside_loop(code, line_num):
    """修复 'break' outside loop - 注释掉错误行"""
    if not line_num:
        return code
    lines = code.split('\n')
    if not (0 < line_num <= len(lines)):
        return code
    
    line = lines[line_num - 1]
    lines[line_num - 1] = '# ' + line.lstrip()
    print(f"  [LOCAL FIX] 注释掉循环外的 break: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_python_continue_outside_loop(code, line_num):
    """修复 'continue' outside loop - 注释掉错误行"""
    if not line_num:
        return code
    lines = code.split('\n')
    if not (0 < line_num <= len(lines)):
        return code
    
    line = lines[line_num - 1]
    lines[line_num - 1] = '# ' + line.lstrip()
    print(f"  [LOCAL FIX] 注释掉循环外的 continue: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_python_invalid_assignment(code, line_num):
    """修复无效赋值 (cannot assign to)"""
    if not line_num:
        return code
    lines = code.split('\n')
    if not (0 < line_num <= len(lines)):
        return code
    
    line = lines[line_num - 1]
    # 检查是否是比较运算符误用为赋值
    # 例如: if x = 5:  应该是 if x == 5:
    if ' = ' in line and ('if ' in line or 'while ' in line or 'elif ' in line):
        new_line = re.sub(r'(\s+)(\w+)\s*=\s*(\w+)', r'\1\2 == \3', line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 将 = 改为 ==: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_python_eq_to_assign(code, line_num):
    """修复条件语句中使用 = 而非 =="""
    if not line_num:
        return code
    lines = code.split('\n')
    if not (0 < line_num <= len(lines)):
        return code
    
    line = lines[line_num - 1]
    # 在条件语句中将 = 替换为 ==
    if re.search(r'(if|while|elif).*[^=!<>]=[^=]', line):
        new_line = re.sub(r'([^=!<>])=([^=])', r'\1==\2', line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 将 = 改为 ==: 第 {line_num} 行")
    return '\n'.join(lines)


# ============================================================
# 【新增】依赖导入错误修复函数
# ============================================================

# 全局变量：项目文件结构缓存（用于模块路径恢复）
_PROJECT_MODULES_CACHE = {}

def scan_project_modules(project_dir):
    """
    扫描项目目录，获取所有可用的 Python 模块
    返回: {'短名': '完整模块路径'} 如 {'step1': 'code.step1_parsing'}
    """
    global _PROJECT_MODULES_CACHE
    if project_dir in _PROJECT_MODULES_CACHE:
        return _PROJECT_MODULES_CACHE[project_dir]
    
    modules = {}
    try:
        for root, dirs, files in os.walk(project_dir):
            # 跳过隐藏目录和常见非代码目录
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('__pycache__', 'venv', '.git', 'node_modules')]
            
            for f in files:
                if f.endswith('.py') and not f.startswith('_'):
                    rel_path = os.path.relpath(os.path.join(root, f), project_dir)
                    # 转换为模块路径: code/step1_parsing.py -> code.step1_parsing
                    module_path = rel_path.replace(os.sep, '.').replace('/', '.')[:-3]
                    module_name = f[:-3]  # 文件名（不含 .py）
                    
                    # 存储多种变体
                    modules[module_name] = module_path
                    # 存储下划线前缀
                    if '_' in module_name:
                        prefix = module_name.split('_')[0]
                        if prefix not in modules:
                            modules[prefix] = module_path
    except Exception as e:
        print(f"  [WARN] 扫描项目模块失败: {e}")
    
    _PROJECT_MODULES_CACHE[project_dir] = modules
    return modules


def fix_python_module_not_found(code, missing_module, error_msg):
    """
    修复 ModuleNotFoundError
    场景1: from step1 import X -> from step1_parsing import X (模块名截断)
    场景2: 相对导入路径错误
    """
    lines = code.split('\n')
    
    # 尝试从项目结构中查找完整模块名
    # 获取当前工作目录
    project_dir = os.getcwd()
    modules = scan_project_modules(project_dir)
    
    # 查找匹配的完整模块名
    full_module = None
    for short_name, full_path in modules.items():
        if short_name == missing_module or full_path.endswith('.' + missing_module):
            # 找到了截断版本对应的完整模块
            if '_' in full_path.split('.')[-1]:
                full_module = full_path
                break
    
    if not full_module:
        # 查找以 missing_module 开头的模块
        for short_name, full_path in modules.items():
            module_basename = full_path.split('.')[-1]
            if module_basename.startswith(missing_module + '_'):
                full_module = full_path
                break
    
    if full_module:
        # 替换代码中的截断模块名
        for i, line in enumerate(lines):
            if f'from {missing_module} import' in line or f'import {missing_module}' in line:
                # 提取完整模块名的最后一部分
                correct_module = full_module.split('.')[-1] if '.' not in missing_module else full_module
                new_line = line.replace(f'from {missing_module} ', f'from {correct_module} ')
                new_line = new_line.replace(f'import {missing_module}', f'import {correct_module}')
                if new_line != line:
                    lines[i] = new_line
                    print(f"  [LOCAL FIX] 模块路径恢复: {missing_module} -> {correct_module}")
                    return '\n'.join(lines)
    
    # 如果是相对导入问题，尝试转换为绝对导入
    for i, line in enumerate(lines):
        if f'from .{missing_module}' in line:
            # 将相对导入转为绝对导入
            new_line = line.replace(f'from .{missing_module}', f'from {missing_module}')
            if new_line != line:
                lines[i] = new_line
                print(f"  [LOCAL FIX] 相对导入转绝对: .{missing_module} -> {missing_module}")
                return '\n'.join(lines)
    
    print(f"  [INFO] 无法自动修复模块不存在错误: {missing_module}，需 LLM 处理")
    return code


def fix_python_import_error(code, symbol_name, module_name, error_msg):
    """
    修复 ImportError: cannot import name 'X' from 'Y'
    场景: 从截断的模块名导入，需要恢复完整路径
    """
    lines = code.split('\n')
    project_dir = os.getcwd()
    modules = scan_project_modules(project_dir)
    
    # 查找包含该符号的完整模块
    correct_module = None
    for short_name, full_path in modules.items():
        module_basename = full_path.split('.')[-1]
        # 检查是否是截断版本
        if module_basename.startswith(module_name + '_') or module_name in full_path:
            correct_module = module_basename
            break
    
    if correct_module and correct_module != module_name:
        for i, line in enumerate(lines):
            if f'from {module_name} import' in line and symbol_name in line:
                new_line = line.replace(f'from {module_name} ', f'from {correct_module} ')
                if new_line != line:
                    lines[i] = new_line
                    print(f"  [LOCAL FIX] 导入路径修复: from {module_name} -> from {correct_module}")
                    return '\n'.join(lines)
    
    print(f"  [INFO] 无法自动修复导入错误: {symbol_name} from {module_name}，需 LLM 处理")
    return code


def fix_python_relative_import(code, error_msg):
    """
    修复相对导入错误
    场景: from . import xxx 在非包环境下执行
    解决: 转换为绝对导入
    """
    lines = code.split('\n')
    modified = False
    
    for i, line in enumerate(lines):
        # from . import xxx -> from xxx import xxx
        match = re.match(r'^(\s*)from\s+\.\s+import\s+(\w+)', line)
        if match:
            indent, module = match.groups()
            new_line = f"{indent}import {module}"
            lines[i] = new_line
            print(f"  [LOCAL FIX] 相对导入转绝对: from . import {module} -> import {module}")
            modified = True
            continue
        
        # from .xxx import yyy -> from xxx import yyy
        match = re.match(r'^(\s*)from\s+\.(\w+)\s+import\s+(.+)', line)
        if match:
            indent, module, symbols = match.groups()
            new_line = f"{indent}from {module} import {symbols}"
            lines[i] = new_line
            print(f"  [LOCAL FIX] 相对导入转绝对: from .{module} -> from {module}")
            modified = True
    
    if modified:
        return '\n'.join(lines)
    
    print(f"  [INFO] 无法自动修复相对导入错误，需 LLM 处理")
    return code


def fix_python_import_truncated(code, truncated_module, full_module):
    """
    修复 LLM 截断的模块名
    例如: from step1 import X -> from step1_parsing import X
    """
    lines = code.split('\n')
    for i, line in enumerate(lines):
        if f'from {truncated_module} ' in line or f'import {truncated_module}' in line:
            new_line = line.replace(f'from {truncated_module} ', f'from {full_module} ')
            new_line = new_line.replace(f'import {truncated_module}', f'import {full_module}')
            if new_line != line:
                lines[i] = new_line
                print(f"  [LOCAL FIX] 模块路径恢复: {truncated_module} -> {full_module}")
    return '\n'.join(lines)


# ============================================================
# 其他 Python 修复函数
# ============================================================

def fix_python_arg_order(code, line_num):
    """修复函数参数顺序错误 (non-default argument follows default argument)"""
    # 这个错误需要重排参数，较复杂，返回原代码让 LLM 处理
    print(f"  [INFO] 参数顺序错误需要手动修复或 LLM 处理")
    return code


def fix_none_comparison(code, line_num):
    """修复 == None -> is None"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        new_line = re.sub(r'== None\b', 'is None', line)
        new_line = re.sub(r'!= None\b', 'is not None', new_line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 修复 None 比较: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_bool_comparison(code, line_num):
    """修复 == True -> is True"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        new_line = re.sub(r'== True\b', 'is True', line)
        new_line = re.sub(r'== False\b', 'is False', new_line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 修复 bool 比较: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_not_in_test(code, line_num):
    """修复 not x in y -> x not in y"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        new_line = re.sub(r'\bnot (\w+) in\b', r'\1 not in', line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 修复 not in 语法: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_mixed_indent(code):
    """修复混合缩进 (tab/space)"""
    lines = code.split('\n')
    fixed = False
    for i, line in enumerate(lines):
        if '\t' in line:
            # 将 tab 转换为 4 个空格
            new_line = line.replace('\t', '    ')
            if new_line != line:
                lines[i] = new_line
                fixed = True
    if fixed:
        print(f"  [LOCAL FIX] 修复混合缩进")
    return '\n'.join(lines)


def fix_trailing_semicolon(code, line_num):
    """移除 Python 行末分号"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1].rstrip()
        if line.endswith(';'):
            lines[line_num - 1] = line[:-1]
            print(f"  [LOCAL FIX] 移除行末分号: 第 {line_num} 行")
    return '\n'.join(lines)


# ============================================================
# C++ 模板特有修复函数
# ============================================================
def fix_add_typename(code, error_msg):
    """在依赖类型前添加 typename"""
    line_num = extract_line_num(error_msg)
    if not line_num:
        return code
    
    # 提取需要添加 typename 的类型
    match = re.search(r"need 'typename' before '([^']+)'", error_msg)
    if not match:
        match = re.search(r"dependent-name '([^']+)'", error_msg)
    if not match:
        return code
    
    dep_type = match.group(1)
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        # 在依赖类型前添加 typename
        pattern = rf'\b(?<!typename )({re.escape(dep_type)})\b'
        new_line = re.sub(pattern, f'typename {dep_type}', line, count=1)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 添加 typename: 第 {line_num} 行")
            return '\n'.join(lines)
    return code


def fix_add_template_keyword(code, line_num):
    """在成员模板调用前添加 template 关键字"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        # 在 ->xxx< 或 .xxx< 前添加 template
        new_line = re.sub(r'(->|\.)\s*(\w+)\s*<', r'\1template \2<', line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 添加 template 关键字: 第 {line_num} 行")
            return '\n'.join(lines)
    return code


def fix_remove_typename(code, line_num):
    """移除非模板上下文中的 typename"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        new_line = re.sub(r'\btypename\s+', '', line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 移除 typename: 第 {line_num} 行")
            return '\n'.join(lines)
    return code


def fix_typename_to_class(code, line_num):
    """将 friend typename X -> friend class X"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        new_line = re.sub(r'\bfriend\s+typename\b', 'friend class', line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] typename -> class: 第 {line_num} 行")
            return '\n'.join(lines)
    return code


# ============================================================
# Java 特有修复函数
# ============================================================
def fix_java_init_variable(code, error_msg):
    """修复未初始化的变量 (= null 或 = 0)"""
    line_num = extract_line_num(error_msg)
    match = re.search(r"variable (\w+) might not have been initialized", error_msg)
    if not line_num or not match:
        return code
    
    var_name = match.group(1)
    lines = code.split('\n')
    
    # 向上查找变量声明
    for i in range(line_num - 1, max(0, line_num - 20), -1):
        line = lines[i]
        # 匹配声明模式: Type varName;
        decl_match = re.search(rf'\b(\w+)\s+{var_name}\s*;', line)
        if decl_match:
            var_type = decl_match.group(1)
            # 根据类型添加默认值
            if var_type in ['int', 'long', 'short', 'byte']:
                default_val = '0'
            elif var_type in ['float', 'double']:
                default_val = '0.0'
            elif var_type == 'boolean':
                default_val = 'false'
            elif var_type == 'char':
                default_val = "'\\0'"
            else:
                default_val = 'null'
            
            new_line = line.replace(f'{var_name};', f'{var_name} = {default_val};')
            if new_line != line:
                lines[i] = new_line
                print(f"  [LOCAL FIX] 初始化变量 {var_name} = {default_val}: 第 {i+1} 行")
                return '\n'.join(lines)
    return code


def fix_java_missing_return(code, line_num):
    """添加缺少的 return 语句"""
    if not line_num:
        return code
    lines = code.split('\n')
    
    # 在报错行前查找方法结束位置
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1].strip()
        if line == '}':
            # 在 } 前添加 return null;
            indent = len(lines[line_num - 1]) - len(lines[line_num - 1].lstrip())
            lines.insert(line_num - 1, ' ' * indent + '    return null;')
            print(f"  [LOCAL FIX] 添加 return null: 第 {line_num} 行前")
            return '\n'.join(lines)
    return code


def fix_java_missing_method_body(code, line_num):
    """修复缺少方法体"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1].rstrip()
        # 在方法声明后添加 {}
        if not line.endswith('{') and not line.endswith(';'):
            lines[line_num - 1] = line + ' {}'
            print(f"  [LOCAL FIX] 添加方法体 {{}}: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_java_return_type(code, line_num):
    """添加缺少的返回类型 (void)"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        # 匹配方法声明缺少返回类型
        new_line = re.sub(r'^(\s*)(public|private|protected)?\s*(\w+)\s*\(', r'\1\2 void \3(', line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 添加 void 返回类型: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_java_static_context(code, error_msg):
    """在非静态引用前添加 new 或注释掉"""
    line_num = extract_line_num(error_msg)
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        # 注释掉问题行（保守处理）
        lines[line_num - 1] = '// ' + lines[line_num - 1].lstrip()
        print(f"  [LOCAL FIX] 注释静态上下文错误: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_java_abstract_body(code, line_num):
    """移除抽象方法的方法体"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        # 将 { ... } 替换为 ;
        new_line = re.sub(r'\s*\{[^}]*\}\s*$', ';', line)
        if new_line == line:
            # 如果只有 { 开头，替换为 ;
            new_line = re.sub(r'\s*\{\s*$', ';', line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 移除抽象方法体: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_java_remove_throws(code, error_msg):
    """移除未使用的 throws 声明"""
    line_num = extract_line_num(error_msg)
    match = re.search(r"exception (\w+) is never thrown", error_msg)
    if not line_num or not match:
        return code
    
    exception_name = match.group(1)
    lines = code.split('\n')
    
    for i in range(line_num - 1, max(0, line_num - 10), -1):
        line = lines[i]
        if 'throws' in line:
            # 移除特定异常
            new_line = re.sub(rf',?\s*{exception_name}', '', line)
            new_line = re.sub(r'throws\s*,', 'throws', new_line)
            new_line = re.sub(r'throws\s*(\)|\{{)', r'\1', new_line)
            if new_line != line:
                lines[i] = new_line
                print(f"  [LOCAL FIX] 移除 throws {exception_name}: 第 {i+1} 行")
                return '\n'.join(lines)
    return code


def fix_java_remove_override(code, line_num):
    """移除无效的 @Override 注解"""
    if not line_num:
        return code
    lines = code.split('\n')
    # 向上查找 @Override
    for i in range(line_num - 1, max(0, line_num - 5), -1):
        if '@Override' in lines[i]:
            lines[i] = '// ' + lines[i].lstrip()
            print(f"  [LOCAL FIX] 注释无效 @Override: 第 {i+1} 行")
            return '\n'.join(lines)
    return code


# ============================================================
# 新增 Java 修复函数
# ============================================================
def fix_java_cannot_find_symbol(code, error_msg):
    """修复 cannot find symbol - 尝试添加声明或导入"""
    line_num = extract_line_num(error_msg)
    if not line_num:
        return code
    
    # 提取符号名
    match = re.search(r'symbol:\s*\w+\s+(\w+)', error_msg)
    if not match:
        return code
    symbol = match.group(1)
    
    # 常见类的 import 映射
    common_imports = {
        'List': 'import java.util.List;',
        'ArrayList': 'import java.util.ArrayList;',
        'Map': 'import java.util.Map;',
        'HashMap': 'import java.util.HashMap;',
        'Set': 'import java.util.Set;',
        'HashSet': 'import java.util.HashSet;',
        'Arrays': 'import java.util.Arrays;',
        'Collections': 'import java.util.Collections;',
        'Scanner': 'import java.util.Scanner;',
        'File': 'import java.io.File;',
        'IOException': 'import java.io.IOException;',
        'BufferedReader': 'import java.io.BufferedReader;',
        'FileReader': 'import java.io.FileReader;',
        'PrintWriter': 'import java.io.PrintWriter;',
    }
    
    if symbol in common_imports:
        import_stmt = common_imports[symbol]
        if import_stmt not in code:
            # 在文件开头添加 import
            lines = code.split('\n')
            # 找到第一个 import 或 package 后插入
            insert_pos = 0
            for i, line in enumerate(lines):
                if line.strip().startswith('package '):
                    insert_pos = i + 1
                elif line.strip().startswith('import '):
                    insert_pos = i + 1
            lines.insert(insert_pos, import_stmt)
            print(f"  [LOCAL FIX] 添加 {import_stmt}")
            return '\n'.join(lines)
    
    return code


def fix_java_missing_import(code, error_msg):
    """修复 package does not exist - 注释错误的 import"""
    line_num = extract_line_num(error_msg)
    if not line_num:
        return code
    
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        if 'import ' in line:
            lines[line_num - 1] = '// ' + line.lstrip()  # 注释掉错误的 import
            print(f"  [LOCAL FIX] 注释无效 import: 第 {line_num} 行")
    return '\n'.join(lines)


def fix_java_array_access(code, line_num):
    """修复 array required, but found"""
    # 数组访问错误通常需要手动修复
    print(f"  [INFO] 数组访问错误需要手动修复或 LLM 处理")
    return code


def fix_java_constructor_args(code, error_msg):
    """修复 constructor cannot be applied"""
    # 构造函数参数错误通常需要手动修复
    print(f"  [INFO] 构造函数参数错误需要手动修复或 LLM 处理")
    return code


def fix_java_super_first(code, line_num):
    """修复 call to super must be first statement"""
    if not line_num:
        return code
    lines = code.split('\n')
    
    # 查找 super() 调用并移动到构造函数开头
    for i in range(line_num - 1, -1, -1):
        if 'super(' in lines[i]:
            super_line = lines.pop(i)
            # 找到构造函数的 {
            for j in range(i - 1, -1, -1):
                if '{' in lines[j]:
                    lines.insert(j + 1, super_line)
                    print(f"  [LOCAL FIX] 移动 super() 到构造函数开头")
                    return '\n'.join(lines)
            break
    return code


# ============================================================
# 新增 C/C++ 修复函数
# ============================================================
def fix_c_implicit_declaration(code, error_msg):
    """修复 implicit declaration of function - 添加常见头文件"""
    # 提取函数名
    match = re.search(r"implicit declaration of function '(\w+)'", error_msg)
    if not match:
        return code
    func_name = match.group(1)
    
    # 常见函数到头文件的映射
    func_headers = {
        'printf': '<stdio.h>',
        'scanf': '<stdio.h>',
        'fprintf': '<stdio.h>',
        'fscanf': '<stdio.h>',
        'fopen': '<stdio.h>',
        'fclose': '<stdio.h>',
        'fgets': '<stdio.h>',
        'fputs': '<stdio.h>',
        'malloc': '<stdlib.h>',
        'calloc': '<stdlib.h>',
        'realloc': '<stdlib.h>',
        'free': '<stdlib.h>',
        'exit': '<stdlib.h>',
        'atoi': '<stdlib.h>',
        'atof': '<stdlib.h>',
        'rand': '<stdlib.h>',
        'srand': '<stdlib.h>',
        'strlen': '<string.h>',
        'strcpy': '<string.h>',
        'strncpy': '<string.h>',
        'strcat': '<string.h>',
        'strcmp': '<string.h>',
        'strstr': '<string.h>',
        'memcpy': '<string.h>',
        'memset': '<string.h>',
        'sqrt': '<math.h>',
        'pow': '<math.h>',
        'sin': '<math.h>',
        'cos': '<math.h>',
        'abs': '<math.h>',
        'isdigit': '<ctype.h>',
        'isalpha': '<ctype.h>',
        'toupper': '<ctype.h>',
        'tolower': '<ctype.h>',
    }
    
    if func_name in func_headers:
        header = func_headers[func_name]
        include_stmt = f'#include {header}'
        if include_stmt not in code:
            # 在文件开头添加 include
            lines = code.split('\n')
            # 找到第一个非 #include 行
            insert_pos = 0
            for i, line in enumerate(lines):
                if line.strip().startswith('#include'):
                    insert_pos = i + 1
                elif line.strip() and not line.strip().startswith('//'):
                    break
            lines.insert(insert_pos, include_stmt)
            print(f"  [LOCAL FIX] 添加 {include_stmt}")
            return '\n'.join(lines)
    return code


def fix_c_undeclared(code, error_msg):
    """修复 undeclared identifier"""
    # 尝试通过 implicit declaration 修复
    return fix_c_implicit_declaration(code, error_msg)


def fix_c_unknown_type(code, error_msg):
    """修复 unknown type name - 添加常见头文件"""
    # 提取类型名
    match = re.search(r"unknown type name '(\w+)'", error_msg)
    if not match:
        return code
    type_name = match.group(1)
    
    # 常见类型到头文件的映射
    type_headers = {
        'size_t': '<stddef.h>',
        'ptrdiff_t': '<stddef.h>',
        'FILE': '<stdio.h>',
        'bool': '<stdbool.h>',
        'int8_t': '<stdint.h>',
        'int16_t': '<stdint.h>',
        'int32_t': '<stdint.h>',
        'int64_t': '<stdint.h>',
        'uint8_t': '<stdint.h>',
        'uint16_t': '<stdint.h>',
        'uint32_t': '<stdint.h>',
        'uint64_t': '<stdint.h>',
    }
    
    if type_name in type_headers:
        header = type_headers[type_name]
        include_stmt = f'#include {header}'
        if include_stmt not in code:
            lines = code.split('\n')
            insert_pos = 0
            for i, line in enumerate(lines):
                if line.strip().startswith('#include'):
                    insert_pos = i + 1
            lines.insert(insert_pos, include_stmt)
            print(f"  [LOCAL FIX] 添加 {include_stmt}")
            return '\n'.join(lines)
    return code


def fix_c_pointer_cast(code, line_num):
    """修复 pointer/integer 不匹配 - 添加显式转换"""
    # 指针转换问题通常需要手动修复
    print(f"  [INFO] 指针转换错误需要手动修复或 LLM 处理")
    return code


def fix_c_function_args(code, error_msg, arg_type):
    """修复 too few/many arguments to function"""
    # 函数参数错误通常需要手动修复
    print(f"  [INFO] 函数参数错误 ({arg_type}) 需要手动修复或 LLM 处理")
    return code


def fix_c_missing_return(code, line_num):
    """修复 control reaches end of non-void function - 添加 return"""
    if not line_num:
        return code
    lines = code.split('\n')
    
    # 在函数结束的 } 前添加 return 0;
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1].strip()
        if line == '}':
            indent = len(lines[line_num - 1]) - len(lines[line_num - 1].lstrip())
            lines.insert(line_num - 1, ' ' * indent + '    return 0;')
            print(f"  [LOCAL FIX] 添加 return 0: 第 {line_num} 行前")
            return '\n'.join(lines)
    return code


def fix_c_unused_variable(code, error_msg):
    """修复 unused variable - 注释或添加 (void)"""
    line_num = extract_line_num(error_msg)
    if not line_num:
        return code
    
    # 提取变量名
    match = re.search(r"unused variable '(\w+)'", error_msg)
    if not match:
        return code
    var_name = match.group(1)
    
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        # 在变量声明后添加 (void) 抑制警告
        line = lines[line_num - 1]
        indent = len(line) - len(line.lstrip())
        lines.insert(line_num, ' ' * indent + f'(void){var_name};  // suppress unused warning')
        print(f"  [LOCAL FIX] 抑制未使用变量警告: {var_name}")
        return '\n'.join(lines)
    return code


def fix_paren_before_brace(code, line_num):
    """修复函数参数缺少右括号（在 { 前添加 )）"""
    if not line_num:
        return code
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        
        # 匹配多种情况：
        # 1. func(args { -> func(args) {
        # 2. func(args {; -> func(args) {
        # 3. if (cond { -> if (cond) {
        
        # 先移除错误的 {; 或 { 后的分号
        line = re.sub(r'\{\s*;', '{', line)
        
        # 查找 (... { 模式，改为 (...) {
        match = re.search(r'\(([^)]+)\s*\{', line)
        if match:
            new_line = re.sub(r'\(([^)]+)\s*\{', r'(\1) {', line)
            if new_line != line:
                lines[line_num - 1] = new_line
                print(f"  [LOCAL FIX] 在 {{ 前添加 ): 第 {line_num} 行")
    return '\n'.join(lines)


def fix_undeclared_class(code, error_msg):
    """修复未声明的类（添加前向声明）"""
    # 提取类名
    match = re.search(r"'(\w+)' has not been declared", error_msg)
    if not match:
        return code
    classname = match.group(1)
    
    # 检查是否已经有该类的声明
    if f"class {classname}" in code or f"struct {classname}" in code:
        return code
    
    # 在文件开头（#include 之后）添加前向声明
    lines = code.split('\n')
    insert_pos = 0
    for i, line in enumerate(lines):
        if line.strip().startswith('#include') or line.strip().startswith('using '):
            insert_pos = i + 1
    
    # 添加前向声明和简单类定义
    declaration = f"class {classname} {{}};"
    lines.insert(insert_pos, declaration)
    print(f"  [LOCAL FIX] 添加类声明: {classname}")
    return '\n'.join(lines)


def fix_undeclared_symbol(code, error_msg):
    """修复未声明的符号（如 endl -> std::endl）"""
    # 提取符号名和行号
    match = re.search(r"'(\w+)' was not declared in this scope", error_msg)
    line_num = extract_line_num(error_msg)
    if not match or not line_num:
        return code
    symbol = match.group(1)
    
    # 常见的 std 命名空间符号
    std_symbols = ['endl', 'cout', 'cin', 'cerr', 'string', 'vector', 'map', 'set', 'pair', 'tuple']
    
    if symbol in std_symbols:
        lines = code.split('\n')
        if 0 < line_num <= len(lines):
            line = lines[line_num - 1]
            # 替换为 std::symbol，但避免重复替换
            if f'std::{symbol}' not in line:
                # 使用单词边界匹配
                new_line = re.sub(rf'\b(?<!std::){symbol}\b', f'std::{symbol}', line)
                if new_line != line:
                    lines[line_num - 1] = new_line
                    print(f"  [LOCAL FIX] 修复 {symbol} -> std::{symbol}: 第 {line_num} 行")
                    return '\n'.join(lines)
    
    return code


def fix_no_member(code, error_msg):
    """修复 'has no member named' 错误（移除不存在的方法调用）"""
    # 提取行号和方法名
    line_num = extract_line_num(error_msg)
    match = re.search(r"has no member named '(\w+)'", error_msg)
    if not line_num or not match:
        return code
    member = match.group(1)
    
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        line = lines[line_num - 1]
        # 移除 .member() 调用，保留变量本身
        # 模式: var.member() -> var
        new_line = re.sub(rf'\.(\s*){member}\s*\(\s*\)', '', line)
        if new_line != line:
            lines[line_num - 1] = new_line
            print(f"  [LOCAL FIX] 移除不存在的成员调用 .{member}(): 第 {line_num} 行")
    return '\n'.join(lines)


def fix_semicolon_before_brace(code, line_num):
    """修复 'expected ; before }' 错误（在当前行加分号）"""
    if not line_num:
        return code
    lines = code.split('\n')
    # 错误报告的是缺分号的行本身，不是 } 所在行
    target_line_idx = line_num - 1
    if 0 <= target_line_idx < len(lines):
        line = lines[target_line_idx].rstrip()
        # 跳过已有分号、花括号的行
        if line.endswith(';') or line.endswith('{') or line.endswith('}'):
            return code
        
        # 处理带注释的行
        comment_pos = -1
        for marker in ['//', '/*']:
            pos = line.find(marker)
            if pos != -1 and (comment_pos == -1 or pos < comment_pos):
                comment_pos = pos
        
        if comment_pos > 0:
            before = line[:comment_pos].rstrip()
            comment = line[comment_pos:]
            if before and not before.endswith(';'):
                lines[target_line_idx] = before + ';  ' + comment
                print(f"  [LOCAL FIX] 在第 {line_num} 行注释前加分号 (before '}}')")
                return '\n'.join(lines)
        else:
            lines[target_line_idx] = line + ';'
            print(f"  [LOCAL FIX] 在第 {line_num} 行末加分号 (before '}}')")
            return '\n'.join(lines)
    return code


def fix_semicolon_before(code, error_msg):
    """修复 'expected ; before identifier' 错误（在上一行加分号）"""
    # 逐行检查错误信息，确保行号和匹配内容来自同一行
    for err_line in error_msg.split('\n'):
        # 检查是否是 'before identifier' 模式（排除 before '}' 、before ')' 等）
        before_match = re.search(r"before '(\w+)'", err_line)
        if not before_match:
            continue
        
        # 确保是标识符（字母开头）
        before_token = before_match.group(1)
        if not before_token[0].isalpha() and before_token[0] != '_':
            continue
        
        # 从同一行提取行号
        line_match = re.search(r':(\d+):', err_line)
        if not line_match:
            continue
        line_num = int(line_match.group(1))
        if line_num < 2:
            continue
        
        lines = code.split('\n')
        # 在上一行加分号
        prev_line_idx = line_num - 2
        if 0 <= prev_line_idx < len(lines):
            line = lines[prev_line_idx].rstrip()
            # 跳过已有分号、花括号的行
            if line.endswith(';') or line.endswith('{') or line.endswith('}'):
                continue
            
            # 处理带注释的行
            comment_pos = -1
            for marker in ['//', '/*']:
                pos = line.find(marker)
                if pos != -1 and (comment_pos == -1 or pos < comment_pos):
                    comment_pos = pos
            
            if comment_pos > 0:
                before = line[:comment_pos].rstrip()
                comment = line[comment_pos:]
                if before and not before.endswith(';'):
                    lines[prev_line_idx] = before + ';  ' + comment
                    print(f"  [LOCAL FIX] 在第 {prev_line_idx + 1} 行注释前加分号 (before '{before_token}')")
                    return '\n'.join(lines)
            else:
                lines[prev_line_idx] = line + ';'
                print(f"  [LOCAL FIX] 在第 {prev_line_idx + 1} 行末加分号 (before '{before_token}')")
                return '\n'.join(lines)
    return code



def extract_json(reply):
    """从 LLM 回复中提取 JSON（增强容错）"""
    # 提取 JSON 部分
    if "```json" in reply:
        json_str = reply.split("```json")[1].split("```")[0].strip()
    elif "```" in reply:
        json_str = reply.split("```")[1].split("```")[0].strip()
    elif "{" in reply:
        start = reply.find("{")
        end = reply.rfind("}") + 1
        json_str = reply[start:end]
    else:
        json_str = reply.strip()
    
    # 容错处理：修复常见 JSON 格式问题
    # 1. 移除多余逗号
    json_str = re.sub(r',\s*}', '}', json_str)
    json_str = re.sub(r',\s*]', ']', json_str)
    # 2. 修复缺失逗号（两个 JSON 对象之间）
    json_str = re.sub(r'}\s*{', '},{', json_str)
    # 3. 处理多行 JSON（合并为单行）
    json_str = ' '.join(json_str.split())
    # 4. 尝试提取第一个完整的 JSON 对象
    if json_str.startswith('{'):
        depth = 0
        for i, c in enumerate(json_str):
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    json_str = json_str[:i+1]
                    break
    
    return json_str.strip()


def try_parse_json(json_str):
    """增强版 JSON 解析：支持多行代码、转义字符、容错解析"""
    if not json_str:
        return None
    
    # 方法1: 直接解析
    try:
        return json.loads(json_str)
    except:
        pass
    
    # 方法2: 修复常见转义问题
    try:
        # 处理多行代码中的换行符
        fixed = json_str
        # 将真实换行符转换为 \n 转义序列
        fixed = fixed.replace('\r\n', '\\n').replace('\n', '\\n')
        # 处理单引号
        fixed = fixed.replace("'", '"')
        return json.loads(fixed)
    except:
        pass
    
    # 方法3: 使用 ast.literal_eval
    try:
        import ast
        return ast.literal_eval(json_str)
    except:
        pass
    
    # 方法4: 正则提取 old/new 字段（支持多行）
    try:
        # 支持多行代码的提取
        old_pattern = r'"old"\s*:\s*"((?:[^"]|\\")*?)"'
        new_pattern = r'"new"\s*:\s*"((?:[^"]|\\")*?)"'
        
        old_match = re.search(old_pattern, json_str, re.DOTALL)
        new_match = re.search(new_pattern, json_str, re.DOTALL)
        
        if old_match and new_match:
            old_val = old_match.group(1).replace('\\"', '"').replace('\\n', '\n')
            new_val = new_match.group(1).replace('\\"', '"').replace('\\n', '\n')
            return {"old": old_val, "new": new_val}
    except:
        pass
    
    # 方法5: 处理代码块格式 (```json ... ```)
    try:
        code_block_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', json_str)
        if code_block_match:
            inner_json = code_block_match.group(1).strip()
            return try_parse_json(inner_json)  # 递归解析
    except:
        pass
    
    # 方法6: 智能提取包含冒号的修复（Python 缺冒号场景）
    try:
        # 匹配类似 {"old": "if x > 0", "new": "if x > 0:"}
        simple_fix_pattern = r'\{\s*"old"\s*:\s*"([^"]+)"\s*,\s*"new"\s*:\s*"([^"]+)"\s*\}'
        match = re.search(simple_fix_pattern, json_str)
        if match:
            return {"old": match.group(1), "new": match.group(2)}
    except:
        pass
    
    # 方法7: 处理括号内包含特殊字符的情况
    try:
        # 处理类似 "if len(parts < 3:" 的括号嵌套
        # 找到 old 和 new 之间的内容，即使包含括号
        old_start = json_str.find('"old"')
        new_start = json_str.find('"new"')
        if old_start >= 0 and new_start >= 0:
            # 提取 old 值
            old_val_start = json_str.find(':', old_start) + 1
            old_val_start = json_str.find('"', old_val_start) + 1
            old_val_end = find_string_end(json_str, old_val_start)
            if old_val_end > old_val_start:
                old_val = json_str[old_val_start:old_val_end]
                
                # 提取 new 值
                new_val_start = json_str.find(':', new_start) + 1
                new_val_start = json_str.find('"', new_val_start) + 1
                new_val_end = find_string_end(json_str, new_val_start)
                if new_val_end > new_val_start:
                    new_val = json_str[new_val_start:new_val_end]
                    return {"old": old_val, "new": new_val}
    except:
        pass
    
    return None


def find_string_end(s, start):
    """找到字符串结束位置（处理转义引号）"""
    i = start
    while i < len(s):
        if s[i] == '\\' and i + 1 < len(s):
            i += 2  # 跳过转义字符
        elif s[i] == '"':
            return i
        else:
            i += 1
    return -1

# ============================================================
# Docker 容器管理
# ============================================================
def check_docker_available():
    """检查 Docker 是否可用"""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0
    except Exception as e:
        print(f"[ERROR] Docker 不可用: {e}")
        return False


def check_container_running():
    """检查容器是否正在运行"""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0 and "true" in result.stdout.lower()
    except:
        return False


def restart_container_if_needed(lang):
    """如果容器未运行，自动重启"""
    if not check_container_running():
        print("[WARN] 容器未运行，正在重启...")
        setup_container(lang)
        return True
    return False


def setup_container(lang):
    """创建并配置容器（带状态检查）"""
    # 检查 Docker 是否可用
    if not check_docker_available():
        raise RuntimeError("Docker 未启动或未安装，请先启动 Docker")
    
    config = LANG_CONFIG[lang]
    
    # 清理旧容器
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    
    # 创建新容器
    result = subprocess.run([
        "docker", "run", "-d", "--name", CONTAINER,
        config["image"], "tail", "-f", "/dev/null"
    ], capture_output=True, text=True)
    
    if result.returncode != 0:
        raise RuntimeError(f"创建容器失败: {result.stderr}")
    
    # 创建工作目录
    subprocess.run(["docker", "exec", CONTAINER, "mkdir", "-p", WORKSPACE], capture_output=True)
    
    # 安装 linter（如果需要）
    if config.get("install_linter"):
        try:
            subprocess.run(
                ["docker", "exec", CONTAINER, "sh", "-c", config["install_linter"]],
                capture_output=True, timeout=120
            )
        except subprocess.TimeoutExpired:
            print("[WARN] Linter 安装超时，跳过")
    
    # 验证容器状态
    if not check_container_running():
        raise RuntimeError("容器启动失败")
    
    print(f"[OK] Docker container ready ({config['image']})")


def cleanup_container():
    """":清理容器"""
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


def copy_code_to_container(code, lang):
    """复制代码到容器（带自动恢复）"""
    config = LANG_CONFIG[lang]
    suffix = config["suffix"]
    
    # 检查容器状态，必要时重启
    restart_container_if_needed(lang)
    
    with tempfile.NamedTemporaryFile(mode='w', suffix=suffix, delete=False, encoding='utf-8') as f:
        f.write(code)
        temp_path = f.name
    try:
        filename = f"code{suffix}"
        result = subprocess.run(
            ["docker", "cp", temp_path, f"{CONTAINER}:{WORKSPACE}/{filename}"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"[WARN] 复制失败，重试: {result.stderr}")
            restart_container_if_needed(lang)
            subprocess.run(
                ["docker", "cp", temp_path, f"{CONTAINER}:{WORKSPACE}/{filename}"],
                capture_output=True
            )
    finally:
        os.unlink(temp_path)
    return filename


def run_in_container(cmd, timeout=60, lang=None, retry=True):
    """在容器中执行命令（带自动恢复）"""
    try:
        result = subprocess.run(
            ["docker", "exec", CONTAINER, "sh", "-c", cmd],
            capture_output=True, text=True, timeout=timeout,
            encoding='utf-8', errors='replace'
        )
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        print(f"[WARN] 命令超时 ({timeout}s): {cmd[:50]}...")
        return False, "", "Command timed out"
    except Exception as e:
        if retry and lang:
            print(f"[WARN] 执行失败，尝试重启容器: {e}")
            restart_container_if_needed(lang)
            return run_in_container(cmd, timeout, lang, retry=False)
        return False, "", str(e)

# ============================================================
# 静态检查
# ============================================================
def run_static_check(code, lang):
    """运行静态检查"""
    config = LANG_CONFIG[lang]
    filename = copy_code_to_container(code, lang)
    filepath = f"{WORKSPACE}/{filename}"
    
    # 先尝试自动修复（Python）
    if config.get("linter_fix"):
        fix_cmd = config["linter_fix"].format(file=filepath)
        run_in_container(fix_cmd, timeout=30)
        # 读取修复后的代码
        ok, fixed_code, _ = run_in_container(f"cat {filepath}")
        if ok and fixed_code:
            code = fixed_code
            copy_code_to_container(code, lang)
    
    # 运行 linter
    linter_cmd = config["linter"].format(file=filepath)
    ok, stdout, stderr = run_in_container(linter_cmd, timeout=30)
    
    errors = stderr or stdout
    return ok, errors, code

# ============================================================
# 编译与运行
# ============================================================
def compile_code(lang):
    """编译代码（C/C++/Java）"""
    config = LANG_CONFIG[lang]
    if not config.get("compile_cmd"):
        return True, "", ""
    
    filename = f"code{config['suffix']}"
    filepath = f"{WORKSPACE}/{filename}"
    compile_cmd = config["compile_cmd"].format(file=filepath, dir=WORKSPACE)
    
    return run_in_container(compile_cmd, timeout=60)

def run_code(lang):
    """运行代码"""
    config = LANG_CONFIG[lang]
    filename = f"code{config['suffix']}"
    filepath = f"{WORKSPACE}/{filename}"
    
    # 获取类名（Java）
    classname = "code"
    if lang == "java":
        classname = "code"  # 假设类名为 code
    
    run_cmd = config["run_cmd"].format(file=filepath, dir=WORKSPACE, classname=classname)
    return run_in_container(run_cmd, timeout=60)

# ============================================================
# 错误上下文提取（增强版）
# ============================================================
def extract_all_error_lines(error_msg, lang):
    """提取错误信息中的所有行号"""
    config = LANG_CONFIG[lang]
    pattern = config["error_pattern"]
    
    line_nums = []
    for match in re.finditer(pattern, error_msg):
        groups = match.groups()
        for g in groups:
            if g and g.isdigit():
                line_nums.append(int(g))
                break
    
    return sorted(set(line_nums))  # 去重并排序


def extract_error_context(code, error_msg, lang):
    """提取错误上下文（增强版：支持多行错误 + 智能上下文）"""
    config = LANG_CONFIG[lang]
    pattern = config["error_pattern"]
    
    # 1. 提取所有错误行号
    error_lines = extract_all_error_lines(error_msg, lang)
    
    if not error_lines:
        # 尝试从 Python 错误信息中提取
        py_match = re.search(r'line\s+(\d+)', error_msg, re.IGNORECASE)
        if py_match:
            error_lines = [int(py_match.group(1))]
        else:
            return None, None
    
    # 2. 取主要错误行（第一个或最后一个）
    primary_line = error_lines[0]
    
    lines = code.split('\n')
    
    # 3. 智能上下文范围
    context_before = CONTEXT_LINES
    context_after = CONTEXT_LINES
    
    # 如果有多个错误行，扩展上下文以包含所有错误
    if len(error_lines) > 1:
        min_line = min(error_lines)
        max_line = max(error_lines)
        # 确保上下文包含所有错误行
        context_before = max(context_before, primary_line - min_line + 2)
        context_after = max(context_after, max_line - primary_line + 2)
    
    # 4. 生成上下文（标记错误行）
    start = max(0, primary_line - context_before - 1)
    end = min(len(lines), primary_line + context_after)
    
    context_parts = []
    for i in range(start, end):
        line_num = i + 1
        line_content = lines[i]
        # 标记错误行
        if line_num in error_lines:
            prefix = f">>> {line_num}:"
        else:
            prefix = f"    {line_num}:"
        context_parts.append(f"{prefix} {line_content}")
    
    context = '\n'.join(context_parts)
    
    return primary_line, context


def extract_related_code(code, line_num, lang):
    """提取与错误行相关的代码块（函数/类/循环等）"""
    if not line_num:
        return None
    
    lines = code.split('\n')
    if not (0 < line_num <= len(lines)):
        return None
    
    # 往回查找代码块开始
    block_start = line_num - 1
    block_indent = len(lines[line_num - 1]) - len(lines[line_num - 1].lstrip())
    
    # 根据语言判断代码块
    if lang == 'python':
        # Python 按缩进判断代码块
        for i in range(line_num - 2, -1, -1):
            line = lines[i]
            if line.strip():
                indent = len(line) - len(line.lstrip())
                if indent < block_indent:
                    block_start = i
                    break
    else:
        # C/C++/Java 按花括号判断
        brace_count = 0
        for i in range(line_num - 1, -1, -1):
            line = lines[i]
            brace_count += line.count('}') - line.count('{')
            if brace_count < 0:
                block_start = i
                break
    
    # 返回代码块
    start = max(0, block_start)
    end = min(len(lines), line_num + CONTEXT_LINES)
    
    return '\n'.join(f"{i+1}: {lines[i]}" for i in range(start, end))

# ============================================================
# 本地规则修复
# ============================================================
def try_local_fix(code, error_msg, lang):
    """尝试使用本地规则修复（扫描所有错误，修复所有可修复的）"""
    original_code = code
    fixed_any = False
    
    # === 第1层：GCC/Clang fix-it hints 自动修复 ===
    if lang in ['c', 'cpp']:
        new_code, fixit_applied = try_fixit_suggestions(code, error_msg, lang)
        if fixit_applied:
            code = new_code
            fixed_any = True
    
    # === 第2层：编译器 "did you mean" 建议 ===
    if lang in ['c', 'cpp'] and 'did you mean' in error_msg.lower():
        suggestions = extract_compiler_suggestions(error_msg)
        if suggestions:
            new_code, suggest_applied = smart_symbol_replacement(code, error_msg, suggestions)
            if suggest_applied:
                code = new_code
                fixed_any = True
    
    # === 第3层：优先检查单冒号问题（构造函数初始化列表） ===
    if lang in ['cpp', 'c'] and "found ':' in nested-name-specifier" in error_msg:
        line_num = extract_line_num(error_msg)
        new_code = fix_single_colon(code, line_num)
        if new_code != code:
            return new_code, True
    
    # === 第4层：特殊处理 before 'xxx' 模式（在上一行加分号） ===
    if lang in ['cpp', 'c', 'java'] and "before '" in error_msg:
        new_code = fix_semicolon_before(code, error_msg)
        if new_code != code:
            return new_code, True
    
    # === 第5层：规则匹配修复 ===
    error_lines = error_msg.split('\n')
    rules = LOCAL_FIX_RULES.get(lang, {})
    
    for err_line in error_lines:
        if 'error:' not in err_line:
            continue
        
        # 尝试每个规则
        for pattern, fix_func in rules.items():
            match = re.search(pattern, err_line)
            if match and fix_func:
                try:
                    new_code = fix_func(match, code)
                    if new_code != code:
                        print(f"  [LOCAL FIX] 应用本地规则: {pattern[:30]}...")
                        code = new_code
                        fixed_any = True
                        break  # 每个错误行只应用一个规则
                except Exception:
                    pass
    
    return code, fixed_any

# ============================================================
# LLM 修复（优化 Prompt）
# ============================================================
def extract_error_line_code(code, line_num):
    """提取具体出错行的代码"""
    if not line_num:
        return None
    lines = code.split('\n')
    if 0 < line_num <= len(lines):
        return lines[line_num - 1]
    return None


def call_llm_for_fix(llm, error_msg, context, line_num, lang):
    """调用 LLM 修复（优化版：包含具体出错行 + 思维链）"""
    
    # 提取具体出错行代码
    error_line_code = None
    if line_num:
        context_lines = context.split('\n') if context else []
        for cl in context_lines:
            if cl.startswith(f"{line_num}:"):
                error_line_code = cl.split(':', 1)[1].strip() if ':' in cl else None
                break
    
    # 根据语言生成语法规则提示
    syntax_hints = {
        'python': """
- Python 语法：if/for/while/def/class 后需要冒号
- 括号必须配对：( ) [ ] {{ }}
- 缩进使用 4 个空格""",
        'java': """
- Java 语法：语句末尾需要分号 ;
- 括号必须配对：( ) [ ] {{ }}
- 代码块使用 {{ }}""",
        'c': """
- C 语法：语句末尾需要分号 ;
- 括号必须配对：( ) [ ] {{ }}
- 字符串用双引号 """,
        'cpp': """
- C++ 语法：语句末尾需要分号 ;
- 括号必须配对：( ) [ ] {{ }} < >
- 作用域使用 ::"""
    }
    
    prompt = f"""## {lang.upper()} 语法修复任务

### 错误信息
```
{error_msg[:400]}
```

### 出错位置
- 行号: 第 {line_num} 行
- 出错代码: `{error_line_code or '未知'}`

### 上下文代码
```{lang}
{context}
```

### 语法规则提示{syntax_hints.get(lang, '')}

### 修复要求（严格遵守）
1. **只修语法错误**：缺分号/冒号/括号不匹配/引号未关闭
2. **禁止改语义**：不改变量名/函数名/算法逻辑/注释
3. **最小修改**：只修出错位置，不重构代码
4. **【关键】import 路径保护**：严禁截断模块名，如 `step1_parsing` 不能变成 `step1`

### 返回格式
返回 **一个 JSON 对象**，包含最小修改片段：
```json
{{"old": "原始错误片段", "new": "修复后片段"}}
```

示例：
- 缺分号: {{"old": "x = 1", "new": "x = 1;"}}
- 缺冒号: {{"old": "if x > 0", "new": "if x > 0:"}}
- 缺右括号: {{"old": "print(x", "new": "print(x)"}}"""

    max_retries = 2
    for retry in range(max_retries):
        try:
            reply = llm(prompt)
            json_str = extract_json(reply)
            result = try_parse_json(json_str)
            if result:
                # 统一格式
                if isinstance(result, dict):
                    return [result]
                return result
            if retry < max_retries - 1:
                print(f"  [RETRY] JSON 解析失败，重试中...")
        except Exception as e:
            if retry < max_retries - 1:
                print(f"  [RETRY] 异常: {e}，重试中...")
            else:
                print(f"  [WARN] LLM 解析失败: {e}")
    return []

def call_llm_batch_fix(llm, errors, code, lang):
    """批量修复多个语法错误（精简提示词）"""
    if not errors:
        return []
    
    # 最多取 5 个错误
    error_list = errors[:5] if len(errors) > 5 else errors
    errors_text = '\n'.join(f"- {e[:100]}" for e in error_list)
    
    prompt = f"""{lang.upper()} 批量语法修复。

## 严格约束
1. 只修语法错误，禁止改语义/逻辑/变量名
2. 最小化修改，未报错代码保持原样
3. 【关键】严禁截断 import 路径，如 step1_parsing 不能变 step1

错误列表:
{errors_text}

返回JSON数组(每个修复只包含最小片段):
[{{"old": "原片段", "new": "修复"}}]"""

    try:
        reply = llm(prompt)
        json_str = extract_json(reply)
        return json.loads(json_str)
    except Exception:
        return []

# ============================================================
# 应用修复
# ============================================================
def apply_replacements(code, replacements):
    """应用修复指令（支持模糊匹配）"""
    for r in replacements:
        old = r.get('old', r.get('search', ''))
        new = r.get('new', r.get('replace', ''))
        
        # 去除行号前缀（如 "190: code" -> "code"）
        old = re.sub(r'^\d+:\s*', '', old)
        new = re.sub(r'\d+:\s*', '', new)
        
        if not old:
            continue
        
        # 方法1: 直接匹配
        if old in code:
            code = code.replace(old, new, 1)
            print(f"  [APPLIED] {old[:40]}...")
            continue
        
        # 方法2: 去除注释后匹配
        old_no_comment = re.split(r'\s*//.*$', old.strip())[0].strip()
        if old_no_comment and old_no_comment in code:
            # 找到包含该代码的行，进行替换
            lines = code.split('\n')
            for i, line in enumerate(lines):
                line_no_comment = re.split(r'\s*//.*$', line.strip())[0].strip()
                if old_no_comment == line_no_comment:
                    # 用新代码替换，保留原始缩进
                    indent = len(line) - len(line.lstrip())
                    new_no_comment = re.split(r'\s*//.*$', new.strip())[0].strip()
                    lines[i] = ' ' * indent + new_no_comment
                    code = '\n'.join(lines)
                    print(f"  [APPLIED NO-COMMENT] line {i+1}: {old_no_comment[:30]}...")
                    break
            continue
        
        # 方法3: 模糊匹配（规范化空格）
        normalized_old = ' '.join(old_no_comment.split()) if old_no_comment else ' '.join(old.split())
        found = False
        for line_idx, line in enumerate(code.split('\n')):
            line_no_comment = re.split(r'\s*//.*$', line)[0]
            normalized_line = ' '.join(line_no_comment.split())
            if normalized_old and normalized_old in normalized_line:
                lines = code.split('\n')
                original_line = lines[line_idx]
                # 加分号修复
                if ';' in new and ';' not in old:
                    stripped = original_line.rstrip()
                    if not stripped.endswith(';'):
                        comment_pos = stripped.find('//')
                        if comment_pos > 0:
                            before = stripped[:comment_pos].rstrip()
                            comment = stripped[comment_pos:]
                            lines[line_idx] = before + ';  ' + comment
                        else:
                            lines[line_idx] = stripped + ';'
                        code = '\n'.join(lines)
                        print(f"  [FUZZY APPLIED] line {line_idx+1}: added ';'")
                        found = True
                        break
                # 括号修复
                elif ')' in new and ')' not in old:
                    stripped = original_line.rstrip()
                    comment_pos = stripped.find('//')
                    if comment_pos > 0:
                        before = stripped[:comment_pos].rstrip()
                        comment = stripped[comment_pos:]
                        lines[line_idx] = before + ')  ' + comment
                    else:
                        lines[line_idx] = stripped + ')'
                    code = '\n'.join(lines)
                    print(f"  [FUZZY APPLIED] line {line_idx+1}: added ')'")
                    found = True
                    break
        if not found:
            print(f"  [NOT FOUND] '{old[:50]}' not in code")
    return code


def validate_fix_semantics(original_code, fixed_code, lang):
    """
    验证修复后的代码语义是否正确（防止引入新错误）
    返回: (is_valid, error_msg)
    """
    if not fixed_code or fixed_code == original_code:
        return True, None
    
    # 1. 检查代码长度变化（警告性检查）
    original_lines = len(original_code.split('\n'))
    fixed_lines = len(fixed_code.split('\n'))
    if abs(fixed_lines - original_lines) > original_lines * 0.5:
        return False, f"代码长度变化过大: {original_lines} -> {fixed_lines} 行"
    
    # 2. 检查关键标识符保持（函数名、类名、变量名）
    original_identifiers = set(re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', original_code))
    fixed_identifiers = set(re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', fixed_code))
    
    # 删除的标识符（可能是有意的，如删除未使用的 import）
    removed_ids = original_identifiers - fixed_identifiers
    # 新增的标识符（警告，可能引入新代码）
    added_ids = fixed_identifiers - original_identifiers
    
    # 过滤关键字
    keywords = {'if', 'else', 'for', 'while', 'return', 'class', 'def', 'try', 'except', 
                'finally', 'import', 'from', 'as', 'in', 'not', 'and', 'or', 'True', 'False',
                'None', 'public', 'private', 'static', 'void', 'int', 'string', 'float',
                'double', 'bool', 'boolean', 'char', 'const', 'auto', 'include', 'using',
                'namespace', 'std', 'new', 'delete', 'virtual', 'override'}
    
    added_ids = added_ids - keywords
    
    # 如果添加了过多新标识符，警告
    if len(added_ids) > 5:
        return False, f"修复引入了过多新标识符: {list(added_ids)[:5]}..."
    
    # 3. 检查括号平衡
    def count_brackets(code):
        return {
            'paren': (code.count('('), code.count(')')),
            'bracket': (code.count('['), code.count(']')),
            'brace': (code.count('{'), code.count('}'))
        }
    
    original_brackets = count_brackets(original_code)
    fixed_brackets = count_brackets(fixed_code)
    
    for bracket_type, (orig_open, orig_close) in original_brackets.items():
        fix_open, fix_close = fixed_brackets[bracket_type]
        # 修复后括号不平衡且原来是平衡的
        if fix_open != fix_close and orig_open == orig_close:
            return False, f"修复导致 {bracket_type} 不平衡: ({fix_open}, {fix_close})"
    
    # 4. 检查不应该删除的关键代码
    critical_patterns = {
        'python': [r'def\s+\w+', r'class\s+\w+', r'import\s+\w+'],
        'java': [r'public\s+class', r'public\s+static\s+void\s+main'],
        'c': [r'int\s+main', r'#include'],
        'cpp': [r'int\s+main', r'#include', r'class\s+\w+']
    }
    
    patterns = critical_patterns.get(lang, [])
    for pattern in patterns:
        orig_matches = len(re.findall(pattern, original_code))
        fix_matches = len(re.findall(pattern, fixed_code))
        if fix_matches < orig_matches:
            return False, f"修复删除了关键代码: {pattern}"
    
    # 5. 【关键】检查 import 路径完整性（防止 LLM 截断模块名）
    import_check = validate_import_paths(original_code, fixed_code, lang)
    if not import_check[0]:
        return import_check
    
    return True, None


def validate_import_paths(original_code, fixed_code, lang):
    """
    验证 import 路径完整性（防止 LLM 注意力涣散截断模块名）
    例如: from step1_parsing import X  ->  from step1 import X (错误)
    """
    if lang == 'python':
        # 提取原始 import 的模块路径
        orig_imports = extract_python_imports(original_code)
        fixed_imports = extract_python_imports(fixed_code)
        
        # 检查是否有模块路径被截断
        for orig_module in orig_imports:
            # 检查是否存在截断版本（前缀匹配但不完整）
            for fixed_module in fixed_imports:
                # 原始模块是 fixed_module 的扩展版本（说明被截断了）
                if orig_module.startswith(fixed_module + '_') or \
                   orig_module.startswith(fixed_module + '.'):
                    return False, f"import 路径被截断: {orig_module} -> {fixed_module}"
        
        # 检查关键模块是否丢失
        for orig_module in orig_imports:
            # 跳过标准库
            if orig_module.split('.')[0] in ('os', 'sys', 'typing', 're', 'json', 'time'):
                continue
            # 检查项目模块是否被保留
            if orig_module not in fixed_imports:
                # 检查是否被替换为截断版本
                base_name = orig_module.split('_')[0] if '_' in orig_module else orig_module.split('.')[0]
                if base_name in fixed_imports:
                    return False, f"import 路径被截断: {orig_module} -> {base_name}"
    
    elif lang == 'java':
        # 提取 Java import 路径
        orig_imports = set(re.findall(r'import\s+([\w.]+);', original_code))
        fixed_imports = set(re.findall(r'import\s+([\w.]+);', fixed_code))
        
        # 检查包路径是否被截断
        for orig in orig_imports:
            for fixed in fixed_imports:
                if orig.startswith(fixed + '.') and orig != fixed:
                    return False, f"import 路径被截断: {orig} -> {fixed}"
    
    return True, None


def extract_python_imports(code):
    """
    提取 Python 代码中的所有模块路径
    支持: import xxx, from xxx import yyy, from xxx.yyy import zzz
    """
    modules = set()
    
    # from xxx import yyy
    for match in re.finditer(r'from\s+([\w.]+)\s+import', code):
        modules.add(match.group(1))
    
    # import xxx
    for match in re.finditer(r'^import\s+([\w.]+)', code, re.MULTILINE):
        modules.add(match.group(1))
    
    # import xxx as yyy
    for match in re.finditer(r'import\s+([\w.]+)\s+as', code):
        modules.add(match.group(1))
    
    return modules


def safe_apply_fix(original_code, fixed_code, lang):
    """
    安全应用修复：验证后再应用
    返回: (result_code, was_applied)
    """
    is_valid, error_msg = validate_fix_semantics(original_code, fixed_code, lang)
    
    if is_valid:
        return fixed_code, True
    
    # 【新增】如果是 import 路径截断，尝试自动恢复
    if error_msg and 'import 路径被截断' in error_msg:
        # 提取截断信息: "import 路径被截断: step1_parsing -> step1"
        match = re.search(r'import 路径被截断: (\S+) -> (\S+)', error_msg)
        if match:
            full_module, truncated_module = match.groups()
            # 尝试恢复
            recovered_code = fix_python_import_truncated(fixed_code, truncated_module, full_module)
            if recovered_code != fixed_code:
                # 再次验证
                is_valid2, _ = validate_fix_semantics(original_code, recovered_code, lang)
                if is_valid2:
                    print(f"  [AUTO-RECOVER] 自动恢复截断的 import: {truncated_module} -> {full_module}")
                    return recovered_code, True
    
    print(f"  [VALIDATE] 修复被拒绝: {error_msg}")
    return original_code, False


def count_errors(error_msg):
    """统计错误数量"""
    if not error_msg:
        return 0
    error_lines = [l for l in error_msg.split('\n') if 'error' in l.lower()]
    return len(error_lines)


def is_fix_improving(original_error_count, new_error_count):
    """检查修复是否减少了错误"""
    return new_error_count < original_error_count

# ============================================================
# 主流程
# ============================================================
def auto_fix(input_file, output_file=None, lang=None):
    """主修复流程"""
    # 检测语言
    if not lang:
        lang = detect_language(input_file)
    
    if lang not in LANG_CONFIG:
        print(f"[ERROR] 不支持的语言: {lang}")
        return False
    
    if not output_file:
        base, ext = os.path.splitext(input_file)
        output_file = f"{base}_fixed{ext}"
    
    print("=" * 60)
    print(f"[AUTO DEBUG] 语言: {lang.upper()}, 文件: {input_file}")
    print("=" * 60)
    
    # 读取代码
    with open(input_file, 'r', encoding='utf-8') as f:
        code = f.read()
    
    # 初始化
    setup_container(lang)
    llm = get_llm_query_func('qwen')
    
    success = False
    
    # Phase 1: 静态检查
    print("\n[PHASE 1] 静态检查")
    ok, errors, code = run_static_check(code, lang)
    if ok:
        print("[OK] 静态检查通过")
    else:
        print(f"[WARN] 发现静态错误")
        # 尝试本地修复
        code, fixed = try_local_fix(code, errors, lang)
        if fixed:
            ok, errors, code = run_static_check(code, lang)
    
    # Phase 2: 编译检查（C/C++/Java）
    if LANG_CONFIG[lang].get("compile_cmd"):
        print("\n[PHASE 2] 编译检查")
        copy_code_to_container(code, lang)
        ok, stdout, stderr = compile_code(lang)
        
        compile_iter = 0
        while not ok and compile_iter < 3:
            compile_iter += 1
            error_msg = stderr or stdout
            print(f"[COMPILE ERROR] {error_msg[:200]}")
            
            # 尝试本地修复
            code, fixed = try_local_fix(code, error_msg, lang)
            if not fixed:
                # 调用 LLM
                line_num, context = extract_error_context(code, error_msg, lang)
                if context:
                    replacements = call_llm_for_fix(llm, error_msg, context, line_num, lang)
                    if replacements:
                        code = apply_replacements(code, replacements)
            
            copy_code_to_container(code, lang)
            ok, stdout, stderr = compile_code(lang)
        
        if ok:
            print("[OK] 编译成功")
        else:
            print("[FAIL] 编译失败，无法继续")
    
    # Phase 3: 运行时修复
    print("\n[PHASE 3] 运行时检查")
    for iteration in range(MAX_ITERATIONS):
        print(f"\n=== 迭代 {iteration + 1}/{MAX_ITERATIONS} ===")
        
        copy_code_to_container(code, lang)
        
        # 编译（如果需要）
        if LANG_CONFIG[lang].get("compile_cmd"):
            ok, _, stderr = compile_code(lang)
            if not ok:
                print(f"[COMPILE ERROR] {stderr[:100]}")
                break
        
        # 运行
        ok, stdout, stderr = run_code(lang)
        
        if ok:
            print("[SUCCESS] 代码执行成功!")
            if stdout.strip():
                print(f"输出: {stdout[:200]}")
            success = True
            break
        
        error_msg = stderr or stdout
        print(f"[RUNTIME ERROR] {error_msg[:200]}")
        
        # 尝试本地修复
        code, fixed = try_local_fix(code, error_msg, lang)
        if fixed:
            continue
        
        # 调用 LLM
        line_num, context = extract_error_context(code, error_msg, lang)
        if not context:
            print("[FAIL] 无法定位错误")
            break
        
        replacements = call_llm_for_fix(llm, error_msg, context, line_num, lang)
        if not replacements:
            print("[FAIL] LLM 未返回修复")
            break
        
        new_code = apply_replacements(code, replacements)
        if new_code == code:
            print("[WARN] 修复未应用")
            break
        code = new_code
    
    # 保存结果
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(code)
    
    print(f"\n{'=' * 60}")
    print(f"[FINAL] {'成功' if success else '部分修复'} -> {output_file}")
    print("=" * 60)
    
    cleanup_container()
    return success

# ============================================================
# 跨文件依赖分析
# ============================================================
def parse_dependencies(filepath, lang):
    """解析文件的依赖关系，返回依赖的文件名列表"""
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()
    
    deps = []
    dirname = os.path.dirname(filepath)
    
    if lang == 'python':
        # import xxx / from xxx import yyy
        for match in re.finditer(r'^\s*(?:from\s+(\w+)|import\s+(\w+))', content, re.MULTILINE):
            module = match.group(1) or match.group(2)
            dep_file = os.path.join(dirname, module + '.py')
            if os.path.exists(dep_file):
                deps.append(os.path.basename(dep_file))
    
    elif lang == 'java':
        # import package.ClassName; -> 查找同目录下的 ClassName.java
        for match in re.finditer(r'^\s*import\s+[\w.]+\.(\w+)\s*;', content, re.MULTILINE):
            classname = match.group(1)
            dep_file = os.path.join(dirname, classname + '.java')
            if os.path.exists(dep_file):
                deps.append(os.path.basename(dep_file))
        # 同包类引用 (无需import)
        for match in re.finditer(r'\bnew\s+(\w+)\s*\(|\b(\w+)\s+\w+\s*=', content):
            classname = match.group(1) or match.group(2)
            if classname and classname[0].isupper():  # 类名首字母大写
                dep_file = os.path.join(dirname, classname + '.java')
                if os.path.exists(dep_file) and os.path.basename(dep_file) != os.path.basename(filepath):
                    deps.append(os.path.basename(dep_file))
    
    elif lang in ['c', 'cpp']:
        # #include "xxx.h" (本地头文件)
        for match in re.finditer(r'#include\s*"([^"]+)"', content):
            header = match.group(1)
            dep_file = os.path.join(dirname, header)
            if os.path.exists(dep_file):
                deps.append(os.path.basename(dep_file))
            # 同时检查对应的 .c/.cpp 文件
            base = os.path.splitext(header)[0]
            for ext in ['.cpp', '.c', '.cc']:
                impl_file = os.path.join(dirname, base + ext)
                if os.path.exists(impl_file):
                    deps.append(os.path.basename(impl_file))
    
    return list(set(deps))  # 去重


def build_dependency_graph(source_files, lang):
    """构建依赖图：{文件: [依赖的文件列表]}"""
    graph = {}
    file_set = set(os.path.basename(f) for f in source_files)
    
    for filepath in source_files:
        filename = os.path.basename(filepath)
        deps = parse_dependencies(filepath, lang)
        # 只保留目录内存在的依赖
        graph[filename] = [d for d in deps if d in file_set and d != filename]
    
    return graph


def topological_sort(graph):
    """拓扑排序：返回按依赖顺序排列的文件列表（被依赖的在前）"""
    in_degree = {node: 0 for node in graph}
    
    # 计算入度（被多少文件依赖）
    for node in graph:
        for dep in graph[node]:
            if dep in in_degree:
                in_degree[dep] += 0  # dep 被 node 依赖
    
    # 反向：计算每个文件被依赖的次数
    reverse_deps = {node: [] for node in graph}
    for node in graph:
        for dep in graph[node]:
            if dep in reverse_deps:
                reverse_deps[dep].append(node)
    
    # 按被依赖次数排序（被依赖多的优先修复）
    priority = {node: len(reverse_deps.get(node, [])) for node in graph}
    
    # Kahn 算法拓扑排序
    visited = set()
    result = []
    
    while len(result) < len(graph):
        # 找出没有未修复依赖的文件
        candidates = []
        for node in graph:
            if node not in visited:
                unresolved_deps = [d for d in graph[node] if d not in visited]
                if not unresolved_deps:
                    candidates.append(node)
        
        if not candidates:
            # 有循环依赖，选择被依赖最多的
            remaining = [n for n in graph if n not in visited]
            candidates = sorted(remaining, key=lambda x: priority.get(x, 0), reverse=True)[:1]
        
        # 按优先级选择
        candidates.sort(key=lambda x: priority.get(x, 0), reverse=True)
        next_node = candidates[0]
        result.append(next_node)
        visited.add(next_node)
    
    return result


def extract_cross_file_error(error_msg, lang):
    """从错误信息中提取涉及的其他文件"""
    other_files = set()
    
    if lang == 'python':
        # ImportError: cannot import name 'xxx' from 'yyy'
        match = re.search(r"from '(\w+)'", error_msg)
        if match:
            other_files.add(match.group(1) + '.py')
    
    elif lang == 'java':
        # error: cannot find symbol ... class XXX
        # 或 error: package xxx does not exist
        for match in re.finditer(r'class\s+(\w+)', error_msg):
            other_files.add(match.group(1) + '.java')
    
    elif lang in ['c', 'cpp']:
        # xxx.h: No such file or directory
        # 或 undefined reference to `xxx'
        for match in re.finditer(r'([\w-]+\.h)', error_msg):
            other_files.add(match.group(1))
    
    return other_files


# ============================================================
# 多文件修复（支持依赖分析）
# ============================================================
def auto_fix_multifile(input_dir, output_dir=None, lang=None, analyze_deps=True):
    """多文件修复（批量处理目录中的源文件）"""
    import glob
    
    # 检测语言
    if not lang:
        # 根据文件后缀自动检测
        cpp_files = glob.glob(os.path.join(input_dir, "*.cpp")) + glob.glob(os.path.join(input_dir, "*.cc"))
        c_files = glob.glob(os.path.join(input_dir, "*.c"))
        java_files = glob.glob(os.path.join(input_dir, "*.java"))
        py_files = glob.glob(os.path.join(input_dir, "*.py"))
        
        if cpp_files:
            lang = "cpp"
        elif c_files:
            lang = "c"
        elif java_files:
            lang = "java"
        elif py_files:
            lang = "python"
        else:
            print(f"[ERROR] 目录中未找到支持的源文件: {input_dir}")
            return False
    
    if lang not in LANG_CONFIG:
        print(f"[ERROR] 不支持的语言: {lang}")
        return False
    
    config = LANG_CONFIG[lang]
    suffix = config["suffix"]
    
    # 获取所有源文件
    source_files = glob.glob(os.path.join(input_dir, f"*{suffix}"))
    if not source_files:
        print(f"[ERROR] 目录中没有 {suffix} 文件")
        return False
    
    # 设置输出目录
    if not output_dir:
        output_dir = input_dir + "_fixed"
    os.makedirs(output_dir, exist_ok=True)
    
    print("=" * 60)
    print(f"[MULTI-FILE AUTO DEBUG] 语言: {lang.upper()}")
    print(f"[INPUT] {input_dir} ({len(source_files)} 个文件)")
    print(f"[OUTPUT] {output_dir}")
    
    # 依赖分析与排序
    if analyze_deps:
        print(f"[DEPS] 分析跨文件依赖...")
        dep_graph = build_dependency_graph(source_files, lang)
        sorted_files = topological_sort(dep_graph)
        # 重新排序 source_files
        file_map = {os.path.basename(f): f for f in source_files}
        source_files = [file_map[f] for f in sorted_files if f in file_map]
        print(f"[DEPS] 修复顺序: {' -> '.join(sorted_files[:5])}{'...' if len(sorted_files) > 5 else ''}")
        # 显示依赖关系
        for f, deps in dep_graph.items():
            if deps:
                print(f"  {f} 依赖: {', '.join(deps)}")
    
    print("=" * 60)
    
    # 初始化
    setup_container(lang)
    llm = get_llm_query_func('qwen')
    
    total_errors_fixed = 0
    results = {}
    
    # 逐文件处理
    for i, filepath in enumerate(source_files):
        filename = os.path.basename(filepath)
        print(f"\n[{i+1}/{len(source_files)}] 处理: {filename}")
        print("-" * 40)
        
        # 读取代码
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            code = f.read()
        
        original_code = code
        file_errors_fixed = 0
        
        # 复制到容器
        copy_file_to_container(code, filename, lang)
        
        # 多轮修复
        for iteration in range(MAX_ITERATIONS):
            # 静态检查
            ok, errors = check_file_syntax(filename, lang)
            
            if ok:
                print(f"  [OK] 无语法错误")
                break
            
            print(f"  [ITER {iteration+1}] 发现错误: {errors[:100]}...")
            
            # 尝试本地修复
            code, fixed = try_local_fix(code, errors, lang)
            if fixed:
                file_errors_fixed += 1
                copy_file_to_container(code, filename, lang)
                continue
            
            # LLM 修复
            line_num, context = extract_error_context(code, errors, lang)
            if context:
                replacements = call_llm_for_fix(llm, errors, context, line_num, lang)
                if replacements:
                    new_code = apply_replacements(code, replacements)
                    if new_code != code:
                        code = new_code
                        file_errors_fixed += 1
                        copy_file_to_container(code, filename, lang)
                        continue
            
            print(f"  [STOP] 无法继续修复")
            break
        
        # 保存修复后的文件
        output_path = os.path.join(output_dir, filename)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(code)
        
        results[filename] = {
            "errors_fixed": file_errors_fixed,
            "changed": code != original_code
        }
        total_errors_fixed += file_errors_fixed
    
    # 汇总报告
    print("\n" + "=" * 60)
    print("[SUMMARY] 多文件修复完成")
    print("=" * 60)
    print(f"总文件数: {len(results)}")
    print(f"修复的错误数: {total_errors_fixed}")
    
    changed_files = [f for f, r in results.items() if r["changed"]]
    print(f"已修改文件: {len(changed_files)}")
    for f in changed_files:
        print(f"  - {f} ({results[f]['errors_fixed']} 处修复)")
    
    # 迭代修复：如果有文件被修复，重新检查依赖它的文件
    if analyze_deps and changed_files:
        print("\n[DEPS] 检查跨文件影响...")
        files_to_recheck = set()
        for changed in changed_files:
            # 找出依赖这个文件的其他文件
            for f, deps in dep_graph.items():
                if changed in deps and f not in changed_files:
                    files_to_recheck.add(f)
        
        if files_to_recheck:
            print(f"[DEPS] 需要重新检查: {', '.join(files_to_recheck)}")
            for filename in files_to_recheck:
                filepath = file_map.get(filename)
                if filepath and os.path.exists(os.path.join(output_dir, filename)):
                    # 读取已修复的版本
                    with open(os.path.join(output_dir, filename), 'r', encoding='utf-8') as f:
                        code = f.read()
                    copy_file_to_container(code, filename, lang)
                    ok, errors = check_file_syntax(filename, lang)
                    if ok:
                        print(f"  [OK] {filename} 无错误")
                    else:
                        print(f"  [WARN] {filename} 仍有错误，可能需要手动处理")
    
    cleanup_container()
    return True


def copy_file_to_container(code, filename, lang):
    """复制指定文件到容器"""
    with tempfile.NamedTemporaryFile(mode='w', suffix=os.path.splitext(filename)[1], delete=False, encoding='utf-8') as f:
        f.write(code)
        temp_path = f.name
    try:
        subprocess.run(["docker", "cp", temp_path, f"{CONTAINER}:{WORKSPACE}/{filename}"], capture_output=True)
    finally:
        os.unlink(temp_path)


def check_file_syntax(filename, lang):
    """检查单个文件的语法"""
    config = LANG_CONFIG[lang]
    filepath = f"{WORKSPACE}/{filename}"
    
    # 使用编译器检查语法
    if lang in ["cpp", "c"]:
        cmd = f"g++ -fsyntax-only -w {filepath} 2>&1" if lang == "cpp" else f"gcc -fsyntax-only -w {filepath} 2>&1"
    elif lang == "java":
        cmd = f"javac -Xlint:none {filepath} 2>&1"
    else:
        cmd = config["linter"].format(file=filepath)
    
    ok, stdout, stderr = run_in_container(cmd, timeout=30)
    errors = stderr or stdout
    return ok or not errors.strip(), errors


# ============================================================
# 项目级运行时多文件自动修复
# ============================================================
def parse_runtime_errors(error_msg, lang, project_dir):
    """解析运行时错误，提取涉及的文件和行号
    返回: [(filename, line_num, error_desc), ...]
    """
    errors_by_file = []
    
    if lang == 'python':
        # File "xxx.py", line 10, in <module>
        # Traceback: ... File "xxx.py", line 5
        for match in re.finditer(r'File "([^"]+)", line (\d+)', error_msg):
            filepath = match.group(1)
            line_num = int(match.group(2))
            filename = os.path.basename(filepath)
            # 只处理项目目录内的文件
            if project_dir in filepath or not filepath.startswith('/'):
                errors_by_file.append((filename, line_num, error_msg[match.start():match.start()+100]))
    
    elif lang == 'java':
        # Exception in thread "main" ... at com.xxx.Class.method(Class.java:15)
        # 或编译错误: Class.java:15: error: xxx
        for match in re.finditer(r'([\w]+\.java):(\d+)', error_msg):
            filename = match.group(1)
            line_num = int(match.group(2))
            errors_by_file.append((filename, line_num, error_msg[match.start():match.start()+100]))
        # Java 堆栈跟踪: at xxx.xxx(File.java:10)
        for match in re.finditer(r'at [\w.]+\(([\w]+\.java):(\d+)\)', error_msg):
            filename = match.group(1)
            line_num = int(match.group(2))
            if (filename, line_num, '') not in [(e[0], e[1], '') for e in errors_by_file]:
                errors_by_file.append((filename, line_num, f'Runtime error at line {line_num}'))
    
    elif lang in ['c', 'cpp']:
        # xxx.cpp:10:5: error: xxx
        # 或运行时 segfault 不会有行号，但编译错误有
        for match in re.finditer(r'([\w_-]+\.(?:c|cpp|cc|h|hpp)):(\d+):\d+:', error_msg):
            filename = match.group(1)
            line_num = int(match.group(2))
            errors_by_file.append((filename, line_num, error_msg[match.start():match.start()+100]))
    
    return errors_by_file


def compile_project(source_files, lang):
    """编译整个项目（多文件一起编译）
    返回: (success, error_msg)
    """
    config = LANG_CONFIG[lang]
    filenames = [os.path.basename(f) for f in source_files]
    filepaths = [f"{WORKSPACE}/{fn}" for fn in filenames]
    
    if lang == 'python':
        # Python 不需要编译，检查语法
        all_errors = []
        for fp in filepaths:
            ok, stdout, stderr = run_in_container(f"python -m py_compile {fp} 2>&1", timeout=30)
            if not ok:
                all_errors.append(stderr or stdout)
        if all_errors:
            return False, '\n'.join(all_errors)
        return True, ''
    
    elif lang == 'java':
        # Java: javac *.java
        cmd = f"javac {' '.join(filepaths)} 2>&1"
        ok, stdout, stderr = run_in_container(cmd, timeout=60)
        return ok, stderr or stdout
    
    elif lang in ['c', 'cpp']:
        # C/C++: gcc/g++ -c 各文件，然后链接
        compiler = 'g++' if lang == 'cpp' else 'gcc'
        obj_files = []
        all_errors = []
        for fp, fn in zip(filepaths, filenames):
            obj = f"/tmp/{os.path.splitext(fn)[0]}.o"
            cmd = f"{compiler} -c {fp} -o {obj} 2>&1"
            ok, stdout, stderr = run_in_container(cmd, timeout=30)
            if not ok:
                all_errors.append(stderr or stdout)
            else:
                obj_files.append(obj)
        
        if all_errors:
            return False, '\n'.join(all_errors)
        
        # 链接
        cmd = f"{compiler} {' '.join(obj_files)} -o /tmp/project_out 2>&1"
        ok, stdout, stderr = run_in_container(cmd, timeout=30)
        return ok, stderr or stdout
    
    return True, ''


def run_project(lang, main_file=None):
    """运行项目
    返回: (success, output, error)
    """
    config = LANG_CONFIG[lang]
    
    if lang == 'python':
        # 运行 main 文件
        main_path = f"{WORKSPACE}/{main_file}" if main_file else f"{WORKSPACE}/main.py"
        cmd = f"python {main_path} 2>&1"
    
    elif lang == 'java':
        # 运行 Main 类
        classname = os.path.splitext(main_file)[0] if main_file else 'Main'
        cmd = f"java -cp {WORKSPACE} {classname} 2>&1"
    
    elif lang in ['c', 'cpp']:
        cmd = "/tmp/project_out 2>&1"
    
    else:
        return False, '', 'Unsupported language'
    
    ok, stdout, stderr = run_in_container(cmd, timeout=60)
    return ok, stdout, stderr or stdout


def find_main_file(source_files, lang):
    """找到包含 main 函数的文件"""
    config = LANG_CONFIG[lang]
    main_pattern = config.get('main_pattern')
    if not main_pattern:
        return None
    
    for filepath in source_files:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        if re.search(main_pattern, content):
            return os.path.basename(filepath)
    
    return None


def auto_fix_project(input_dir, output_dir=None, lang=None, max_iterations=10):
    """项目级自动修复：编译+运行，根据报错定位文件并修复"""
    import glob
    
    # 检测语言
    if not lang:
        for ext, l in [('.py', 'python'), ('.java', 'java'), ('.cpp', 'cpp'), ('.c', 'c')]:
            if glob.glob(os.path.join(input_dir, f"*{ext}")):
                lang = l
                break
    
    if not lang or lang not in LANG_CONFIG:
        print(f"[ERROR] 无法检测语言或不支持: {lang}")
        return False
    
    config = LANG_CONFIG[lang]
    suffix = config['suffix']
    
    # 获取所有源文件
    source_files = glob.glob(os.path.join(input_dir, f"*{suffix}"))
    if not source_files:
        print(f"[ERROR] 目录中没有 {suffix} 文件")
        return False
    
    # 设置输出目录
    if not output_dir:
        output_dir = input_dir + "_fixed"
    os.makedirs(output_dir, exist_ok=True)
    
    # 找到 main 文件
    main_file = find_main_file(source_files, lang)
    if not main_file:
        print(f"[WARN] 未找到 main 入口，将使用默认")
        main_file = 'Main.java' if lang == 'java' else 'main.py' if lang == 'python' else None
    
    print("=" * 60)
    print(f"[PROJECT AUTO DEBUG] 语言: {lang.upper()}")
    print(f"[INPUT] {input_dir} ({len(source_files)} 个文件)")
    print(f"[MAIN] {main_file}")
    print(f"[OUTPUT] {output_dir}")
    print("=" * 60)
    
    # 初始化
    setup_container(lang)
    llm = get_llm_query_func('qwen')
    
    # 加载所有文件到内存和容器
    file_contents = {}
    for filepath in source_files:
        filename = os.path.basename(filepath)
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            code = f.read()
        file_contents[filename] = code
        copy_file_to_container(code, filename, lang)
    
    total_fixes = 0
    fixed_files = set()
    
    # 迭代修复循环
    for iteration in range(max_iterations):
        print(f"\n[ITER {iteration+1}/{max_iterations}] 编译项目...")
        
        # 编译
        compile_ok, compile_errors = compile_project(source_files, lang)
        
        if not compile_ok:
            print(f"  [COMPILE ERROR] {compile_errors[:200]}...")
            # 解析编译错误
            errors = parse_runtime_errors(compile_errors, lang, input_dir)
            if not errors:
                print(f"  [STOP] 无法解析错误位置")
                break
            
            # 修复第一个错误
            filename, line_num, err_desc = errors[0]
            print(f"  [TARGET] {filename}:{line_num}")
            
            if filename not in file_contents:
                print(f"  [SKIP] 文件不在项目中")
                continue
            
            code = file_contents[filename]
            
            # 尝试本地修复
            new_code, fixed = try_local_fix(code, compile_errors, lang)
            if fixed:
                file_contents[filename] = new_code
                copy_file_to_container(new_code, filename, lang)
                total_fixes += 1
                fixed_files.add(filename)
                print(f"  [LOCAL FIX] 已修复")
                continue
            
            # LLM 修复
            context = extract_error_context(code, compile_errors, lang)
            if context[1]:
                replacements = call_llm_for_fix(llm, compile_errors, context[1], line_num, lang)
                if replacements:
                    new_code = apply_replacements(code, replacements)
                    if new_code != code:
                        file_contents[filename] = new_code
                        copy_file_to_container(new_code, filename, lang)
                        total_fixes += 1
                        fixed_files.add(filename)
                        print(f"  [LLM FIX] 已修复")
                        continue
            
            print(f"  [STOP] 无法修复 {filename}")
            break
        
        # 编译成功，尝试运行
        print(f"  [COMPILE OK] 尝试运行...")
        run_ok, run_output, run_errors = run_project(lang, main_file)
        
        if run_ok:
            print(f"  [RUN OK] 项目运行成功!")
            break
        
        print(f"  [RUN ERROR] {run_errors[:200]}...")
        
        # 解析运行时错误
        errors = parse_runtime_errors(run_errors, lang, input_dir)
        if not errors:
            print(f"  [STOP] 无法解析运行时错误位置")
            break
        
        # 修复第一个运行时错误
        filename, line_num, err_desc = errors[0]
        print(f"  [TARGET] {filename}:{line_num} (runtime)")
        
        if filename not in file_contents:
            print(f"  [SKIP] 文件不在项目中")
            continue
        
        code = file_contents[filename]
        
        # 运行时错误通常需要 LLM
        context = extract_error_context(code, run_errors, lang)
        if context[1]:
            replacements = call_llm_for_fix(llm, run_errors, context[1], line_num, lang)
            if replacements:
                new_code = apply_replacements(code, replacements)
                if new_code != code:
                    file_contents[filename] = new_code
                    copy_file_to_container(new_code, filename, lang)
                    total_fixes += 1
                    fixed_files.add(filename)
                    print(f"  [LLM FIX] 已修复")
                    continue
        
        print(f"  [STOP] 无法修复运行时错误")
        break
    
    # 保存修复后的文件
    for filename, code in file_contents.items():
        output_path = os.path.join(output_dir, filename)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(code)
    
    # 汇总
    print("\n" + "=" * 60)
    print("[SUMMARY] 项目级修复完成")
    print("=" * 60)
    print(f"总文件数: {len(source_files)}")
    print(f"修复次数: {total_fixes}")
    print(f"已修改文件: {', '.join(fixed_files) if fixed_files else '无'}")
    
    cleanup_container()
    return True


# ============================================================
# 修复统计报告
# ============================================================
class FixStatistics:
    """修复统计数据收集器"""
    def __init__(self):
        self.reset()
    
    def reset(self):
        self.initial_errors = []      # 初始错误列表
        self.final_errors = []        # 最终剩余错误
        self.fixed_errors = []        # 已修复错误
        self.fix_methods = {}         # 修复方式统计 {method: count}
        self.hard_to_find = []        # 难以发现的错误
        self.files_processed = 0
        self.files_fixed = 0
        self.iterations = 0
        self.llm_calls = 0
        self.local_fixes = 0
    
    def add_initial_error(self, error_type, file, line, desc):
        self.initial_errors.append({
            'type': error_type,
            'file': file,
            'line': line,
            'desc': desc
        })
    
    def add_fixed_error(self, error_type, file, line, method, hard_to_find=False):
        self.fixed_errors.append({
            'type': error_type,
            'file': file,
            'line': line,
            'method': method,
            'hard_to_find': hard_to_find
        })
        self.fix_methods[method] = self.fix_methods.get(method, 0) + 1
        if hard_to_find:
            self.hard_to_find.append({
                'type': error_type,
                'file': file,
                'line': line,
                'reason': self._get_hard_reason(error_type)
            })
    
    def _get_hard_reason(self, error_type):
        """判断错误为何难以发现"""
        hard_reasons = {
            'cross_file_dep': '跨文件依赖错误，单文件检查无法发现',
            'runtime_error': '运行时错误，静态检查无法发现',
            'none_comparison': 'PEP8规范错误，编译器不报错',
            'mixed_indent': '混合缩进，肉眼难以分辨',
            'unused_import': '未使用导入，不影响运行但影响代码质量',
            'type_mismatch': '类型不匹配，动态语言运行时才暴露',
            'uninitialized_var': '未初始化变量，特定路径才触发',
        }
        return hard_reasons.get(error_type, '其他难以发现的错误')
    
    def generate_report(self, output_path=None):
        """生成统计报告"""
        report = []
        report.append("=" * 60)
        report.append("修复统计报告")
        report.append("=" * 60)
        
        # 基本统计
        report.append(f"\n## 基本统计")
        report.append(f"处理文件数: {self.files_processed}")
        report.append(f"修改文件数: {self.files_fixed}")
        report.append(f"迭代次数: {self.iterations}")
        
        # 错误数量对比
        report.append(f"\n## 错误数量对比")
        report.append(f"修复前错误数: {len(self.initial_errors)}")
        report.append(f"修复后错误数: {len(self.final_errors)}")
        report.append(f"成功修复数: {len(self.fixed_errors)}")
        if self.initial_errors:
            fix_rate = len(self.fixed_errors) / len(self.initial_errors) * 100
            report.append(f"修复率: {fix_rate:.1f}%")
        
        # 修复方式统计
        report.append(f"\n## 修复方式统计")
        report.append(f"本地规则修复: {self.local_fixes} 次")
        report.append(f"LLM修复: {self.llm_calls} 次")
        for method, count in sorted(self.fix_methods.items(), key=lambda x: -x[1]):
            report.append(f"  - {method}: {count} 次")
        
        # 难以发现的错误
        report.append(f"\n## 难以发现的错误 (共 {len(self.hard_to_find)} 个)")
        if self.hard_to_find:
            for err in self.hard_to_find:
                report.append(f"  - [{err['type']}] {err['file']}:{err['line']}")
                report.append(f"    原因: {err['reason']}")
        else:
            report.append("  无")
        
        # 错误类型分布
        report.append(f"\n## 错误类型分布")
        type_counts = {}
        for err in self.fixed_errors:
            t = err['type']
            type_counts[t] = type_counts.get(t, 0) + 1
        for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
            report.append(f"  - {t}: {c} 个")
        
        report_text = '\n'.join(report)
        print(report_text)
        
        if output_path:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(report_text)
            print(f"\n报告已保存到: {output_path}")
        
        return {
            'initial_errors': len(self.initial_errors),
            'final_errors': len(self.final_errors),
            'fixed_errors': len(self.fixed_errors),
            'fix_rate': len(self.fixed_errors) / max(len(self.initial_errors), 1) * 100,
            'hard_to_find': len(self.hard_to_find),
            'local_fixes': self.local_fixes,
            'llm_calls': self.llm_calls
        }

# 全局统计实例
STATS = FixStatistics()

# 难以发现的错误类型映射
HARD_TO_FIND_PATTERNS = {
    # Python
    r'E711.*comparison to None': 'none_comparison',
    r'E712.*comparison to True': 'none_comparison',
    r'E101.*mixed.*indent': 'mixed_indent',
    r'F401.*imported but unused': 'unused_import',
    # Java
    r'might not have been initialized': 'uninitialized_var',
    r'cannot find symbol.*class': 'cross_file_dep',
    # C/C++
    r'undefined reference': 'cross_file_dep',
    # 运行时
    r'Traceback': 'runtime_error',
    r'Exception in thread': 'runtime_error',
    r'segmentation fault': 'runtime_error',
}

def classify_error(error_msg):
    """分类错误类型，判断是否难以发现"""
    for pattern, error_type in HARD_TO_FIND_PATTERNS.items():
        if re.search(pattern, error_msg, re.IGNORECASE):
            return error_type, True
    
    # 普通错误分类
    if "';' expected" in error_msg or "expected ';'" in error_msg:
        return 'missing_semicolon', False
    elif "expected '}'" in error_msg or "'}' expected" in error_msg:
        return 'missing_brace', False
    elif "expected ')'" in error_msg or "')' expected" in error_msg:
        return 'missing_paren', False
    elif 'SyntaxError' in error_msg:
        return 'syntax_error', False
    elif 'unclosed' in error_msg.lower():
        return 'unclosed_string', False
    else:
        return 'other', False


def auto_fix_with_stats(input_path, output_dir=None, lang=None, mode='project'):
    """带统计功能的自动修复"""
    import glob
    
    STATS.reset()
    
    # 检测是文件还是目录
    if os.path.isfile(input_path):
        source_files = [input_path]
        input_dir = os.path.dirname(input_path)
    else:
        input_dir = input_path
        if not lang:
            for ext, l in [('.py', 'python'), ('.java', 'java'), ('.cpp', 'cpp'), ('.c', 'c')]:
                if glob.glob(os.path.join(input_dir, f"*{ext}")):
                    lang = l
                    break
        config = LANG_CONFIG.get(lang, {})
        suffix = config.get('suffix', '.py')
        source_files = glob.glob(os.path.join(input_dir, f"*{suffix}"))
    
    if not source_files:
        print("[ERROR] 未找到源文件")
        return None
    
    STATS.files_processed = len(source_files)
    
    # 设置输出目录
    if not output_dir:
        output_dir = input_dir + "_fixed"
    os.makedirs(output_dir, exist_ok=True)
    
    print("=" * 60)
    print(f"[AUTO FIX WITH STATS] 语言: {lang.upper()}")
    print(f"[INPUT] {input_path} ({len(source_files)} 个文件)")
    print(f"[OUTPUT] {output_dir}")
    print("=" * 60)
    
    # 初始化
    setup_container(lang)
    
    # === 第一阶段：收集初始错误 ===
    print("\n[阶段 1] 收集初始错误...")
    for filepath in source_files:
        filename = os.path.basename(filepath)
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            code = f.read()
        copy_file_to_container(code, filename, lang)
        
        # 检查该文件的错误
        ok, errors = check_file_syntax(filename, lang)
        if not ok and errors:
            for line in errors.split('\n'):
                if 'error' in line.lower():
                    error_type, is_hard = classify_error(line)
                    line_num = extract_line_num(line) or 0
                    STATS.add_initial_error(error_type, filename, line_num, line[:100])
    
    print(f"  初始错误数: {len(STATS.initial_errors)}")
    
    # === 第二阶段：执行修复 ===
    print("\n[阶段 2] 执行修复...")
    llm = get_llm_query_func('qwen')
    
    file_contents = {}
    for filepath in source_files:
        filename = os.path.basename(filepath)
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            file_contents[filename] = f.read()
    
    fixed_files = set()
    max_iterations = 20
    
    for iteration in range(max_iterations):
        STATS.iterations = iteration + 1
        
        # 根据模式选择检查方式
        if mode == 'project':
            compile_ok, errors = compile_project(source_files, lang)
        else:
            # 逐文件检查
            all_errors = []
            compile_ok = True
            for fn in file_contents:
                ok, err = check_file_syntax(fn, lang)
                if not ok:
                    compile_ok = False
                    all_errors.append(err)
            errors = '\n'.join(all_errors)
        
        if compile_ok:
            print(f"  [ITER {iteration+1}] 编译成功")
            break
        
        # 解析错误
        error_targets = parse_runtime_errors(errors, lang, input_dir)
        if not error_targets:
            # 尝试从错误消息提取
            for fn in file_contents:
                if fn in errors:
                    line_num = extract_line_num(errors) or 1
                    error_targets.append((fn, line_num, errors[:100]))
                    break
        
        if not error_targets:
            print(f"  [ITER {iteration+1}] 无法解析错误位置")
            break
        
        filename, line_num, err_desc = error_targets[0]
        print(f"  [ITER {iteration+1}] {filename}:{line_num}")
        
        if filename not in file_contents:
            continue
        
        code = file_contents[filename]
        error_type, is_hard = classify_error(errors)
        
        # 尝试本地修复
        new_code, fixed = try_local_fix(code, errors, lang)
        if fixed and new_code != code:
            file_contents[filename] = new_code
            copy_file_to_container(new_code, filename, lang)
            STATS.local_fixes += 1
            STATS.add_fixed_error(error_type, filename, line_num, 'local_rule', is_hard)
            fixed_files.add(filename)
            continue
        
        # LLM 修复
        context = extract_error_context(code, errors, lang)
        if context[1]:
            replacements = call_llm_for_fix(llm, errors, context[1], line_num, lang)
            if replacements:
                new_code = apply_replacements(code, replacements)
                if new_code != code:
                    file_contents[filename] = new_code
                    copy_file_to_container(new_code, filename, lang)
                    STATS.llm_calls += 1
                    STATS.add_fixed_error(error_type, filename, line_num, 'llm', is_hard)
                    fixed_files.add(filename)
                    continue
        
        print(f"  [ITER {iteration+1}] 无法修复 {filename}")
        break
    
    STATS.files_fixed = len(fixed_files)
    
    # === 第三阶段：收集最终错误 ===
    print("\n[阶段 3] 收集最终错误...")
    for filename, code in file_contents.items():
        copy_file_to_container(code, filename, lang)
        ok, errors = check_file_syntax(filename, lang)
        if not ok and errors:
            for line in errors.split('\n'):
                if 'error' in line.lower():
                    error_type, _ = classify_error(line)
                    line_num = extract_line_num(line) or 0
                    STATS.final_errors.append({
                        'type': error_type,
                        'file': filename,
                        'line': line_num,
                        'desc': line[:100]
                    })
    
    # 保存修复后的文件
    for filename, code in file_contents.items():
        output_path = os.path.join(output_dir, filename)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(code)
    
    cleanup_container()
    
    # 生成报告
    report_path = os.path.join(output_dir, 'fix_report.txt')
    return STATS.generate_report(report_path)


# ============================================================
# 分层项目修复 (jyn_test 结构)
# ============================================================
def detect_layered_structure(project_dir):
    """
    检测是否为分层项目结构 (jyn_test 类型)
    返回: (is_layered, layers_info)
    layers_info = {
        'data_structure': [文件列表],
        'code': [文件列表, 按step顺序],
        'entry': [入口文件列表]
    }
    """
    data_dir = os.path.join(project_dir, 'data_structure')
    code_dir = os.path.join(project_dir, 'code')
    
    has_data = os.path.isdir(data_dir)
    has_code = os.path.isdir(code_dir)
    
    # 检查入口文件
    entry_files = []
    for f in os.listdir(project_dir):
        if f.endswith('.py') and os.path.isfile(os.path.join(project_dir, f)):
            entry_files.append(f)
    
    if not (has_data or has_code):
        return False, None
    
    layers = {
        'data_structure': [],
        'code': [],
        'entry': entry_files
    }
    
    # 收集 data_structure 文件
    if has_data:
        for f in sorted(os.listdir(data_dir)):
            if f.endswith('.py'):
                layers['data_structure'].append(f)
    
    # 收集 code 文件 (按 step 顺序排序)
    if has_code:
        code_files = [f for f in os.listdir(code_dir) if f.endswith('.py')]
        # 按 step 编号排序
        def extract_step(fname):
            match = re.search(r'step(\d+)', fname.lower())
            return int(match.group(1)) if match else 999
        layers['code'] = sorted(code_files, key=extract_step)
    
    return True, layers


def auto_fix_layered(project_dir, output_dir=None, lang='python'):
    """
    分层项目修复 (jyn_test 结构)
    修复顺序: data_structure/ → code/ (step顺序) → 入口文件
    """
    print("=" * 60)
    print("[LAYERED PROJECT FIX] 分层项目修复")
    print("=" * 60)
    
    # 1. 检测结构
    is_layered, layers = detect_layered_structure(project_dir)
    if not is_layered:
        print(f"[ERROR] 目录 {project_dir} 不是分层结构")
        print("期望结构: data_structure/ + code/ + 入口文件")
        return False
    
    print(f"\n[结构检测]")
    print(f"  data_structure/: {len(layers['data_structure'])} 个文件")
    print(f"  code/:           {len(layers['code'])} 个文件")
    print(f"  入口文件:        {layers['entry']}")
    
    # 2. 设置输出目录
    if not output_dir:
        output_dir = project_dir + '_fixed'
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.join(output_dir, 'data_structure'), exist_ok=True)
    os.makedirs(os.path.join(output_dir, 'code'), exist_ok=True)
    
    # 3. 初始化容器 (设置 PYTHONPATH)
    setup_container(lang)
    # 在容器中创建目录结构
    run_in_container(f"mkdir -p {WORKSPACE}/data_structure {WORKSPACE}/code", timeout=10)
    
    llm = get_llm_query_func('qwen')
    total_fixed = 0
    results = {}
    
    # 4. 按层级修复
    fix_order = []
    
    # Layer 0: data_structure (基础层)
    for f in layers['data_structure']:
        fix_order.append(('data_structure', f))
    
    # Layer 1: code (步骤层)
    for f in layers['code']:
        fix_order.append(('code', f))
    
    # Layer 2: entry (入口层)
    for f in layers['entry']:
        fix_order.append(('.', f))
    
    print(f"\n[修复顺序] 共 {len(fix_order)} 个文件")
    for i, (subdir, f) in enumerate(fix_order):
        layer = 'L0-基础' if subdir == 'data_structure' else ('L1-步骤' if subdir == 'code' else 'L2-入口')
        print(f"  {i+1}. [{layer}] {subdir}/{f}")
    
    print("\n" + "-" * 60)
    
    # 5. 逐文件修复
    for idx, (subdir, filename) in enumerate(fix_order):
        if subdir == '.':
            src_path = os.path.join(project_dir, filename)
            container_path = f"{WORKSPACE}/{filename}"
            out_path = os.path.join(output_dir, filename)
        else:
            src_path = os.path.join(project_dir, subdir, filename)
            container_path = f"{WORKSPACE}/{subdir}/{filename}"
            out_path = os.path.join(output_dir, subdir, filename)
        
        print(f"\n[{idx+1}/{len(fix_order)}] {subdir}/{filename}")
        
        # 读取代码
        with open(src_path, 'r', encoding='utf-8', errors='replace') as f:
            code = f.read()
        original_code = code
        file_fixed = 0
        
        # 复制到容器 (保持目录结构)
        copy_layered_file_to_container(code, subdir, filename)
        
        # 多轮修复
        for iteration in range(MAX_ITERATIONS):
            # 检查语法 (第一次迭代运行 ruff --fix)
            run_ruff_fix = (iteration == 0)
            ok, errors = check_layered_syntax(subdir, filename, auto_fix=run_ruff_fix)
            
            # 如果第一次迭代运行了 ruff --fix，从容器读回修复后的代码
            if run_ruff_fix:
                if subdir == '.':
                    cat_path = f"{WORKSPACE}/{filename}"
                else:
                    cat_path = f"{WORKSPACE}/{subdir}/{filename}"
                _, fixed_code, _ = run_in_container(f"cat {cat_path}", timeout=10)
                if fixed_code and fixed_code != code:
                    code = fixed_code
                    file_fixed += 1
                    print(f"  [RUFF] 自动修复应用")
            
            if ok:
                print(f"  [OK] 无语法错误")
                break
            
            print(f"  [ITER {iteration+1}] {errors[:80]}...")
            
            # 本地修复
            new_code, fixed = try_local_fix(code, errors, lang)
            if fixed and new_code != code:
                code = new_code
                file_fixed += 1
                copy_layered_file_to_container(code, subdir, filename)
                continue
            
            # LLM 修复
            line_num, context = extract_error_context(code, errors, lang)
            if context:
                replacements = call_llm_for_fix(llm, errors, context, line_num, lang)
                if replacements:
                    new_code = apply_replacements(code, replacements)
                    if new_code != code:
                        code = new_code
                        file_fixed += 1
                        copy_layered_file_to_container(code, subdir, filename)
                        continue
            
            print(f"  [STOP] 无法继续修复")
            break
        
        # 保存
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(code)
        
        results[f"{subdir}/{filename}"] = {
            'fixed': file_fixed,
            'changed': code != original_code
        }
        total_fixed += file_fixed
    
    # 6. 汇总
    print("\n" + "=" * 60)
    print("[SUMMARY] 分层项目修复完成")
    print("=" * 60)
    print(f"总文件数: {len(results)}")
    print(f"修复次数: {total_fixed}")
    
    changed = [k for k, v in results.items() if v['changed']]
    print(f"已修改: {len(changed)} 个文件")
    for f in changed:
        print(f"  - {f} ({results[f]['fixed']} 处)")
    
    # 7. 验证整体
    print("\n[验证] 检查入口文件...")
    for entry in layers['entry']:
        copy_layered_file_to_container(
            open(os.path.join(output_dir, entry), 'r', encoding='utf-8').read(),
            '.', entry
        )
        ok, errors = check_layered_syntax('.', entry)
        status = '✓ 通过' if ok else f'✗ 错误: {errors[:50]}'
        print(f"  {entry}: {status}")
    
    cleanup_container()
    print(f"\n[输出] {output_dir}")
    return True


def copy_layered_file_to_container(code, subdir, filename):
    """复制文件到容器，保持目录结构"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
        f.write(code)
        temp_path = f.name
    try:
        if subdir == '.':
            dest = f"{CONTAINER}:{WORKSPACE}/{filename}"
        else:
            dest = f"{CONTAINER}:{WORKSPACE}/{subdir}/{filename}"
        subprocess.run(["docker", "cp", temp_path, dest], capture_output=True)
    finally:
        os.unlink(temp_path)


def check_layered_syntax(subdir, filename, auto_fix=True):
    """
    检查分层项目中的文件语法
    关键：设置 PYTHONPATH 为 WORKSPACE，使跨目录 import 生效
    auto_fix: 是否先运行 ruff --fix 自动修复
    """
    if subdir == '.':
        filepath = f"{WORKSPACE}/{filename}"
    else:
        filepath = f"{WORKSPACE}/{subdir}/{filename}"
    
    # 1. 先运行 ruff --fix 自动修复（第一优先级）
    if auto_fix:
        fix_cmd = f"cd {WORKSPACE} && ruff check {filepath} --fix --unsafe-fixes 2>&1"
        run_in_container(fix_cmd, timeout=30)
    
    # 2. 使用 PYTHONPATH 确保 import 能找到 data_structure 和 code
    cmd = f"cd {WORKSPACE} && PYTHONPATH={WORKSPACE} python -m py_compile {filepath} 2>&1"
    ok, stdout, stderr = run_in_container(cmd, timeout=30)
    errors = stderr or stdout
    return ok or not errors.strip(), errors


# ============================================================
# 命令行入口
# ============================================================
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("用法:")
        print("  单文件: python auto_fix_multilang.py <文件> [语言]")
        print("  多文件: python auto_fix_multilang.py --dir <目录> [语言]")
        print("  项目级: python auto_fix_multilang.py --project <目录> [语言]")
        print("  分层项目: python auto_fix_multilang.py --layered <目录>")
        print("  统计模式: python auto_fix_multilang.py --stats <目录> [语言]")
        print("支持语言: python, java, c, cpp")
        print("示例:")
        print("  python auto_fix_multilang.py code.py")
        print("  python auto_fix_multilang.py --dir test_samples/groot_test cpp")
        print("  python auto_fix_multilang.py --project my_java_project java")
        print("  python auto_fix_multilang.py --layered jyn_test  # 分层项目(data_structure + code + 入口)")
        print("  python auto_fix_multilang.py --stats test_project java  # 生成修复统计报告")
        sys.exit(1)
    
    if sys.argv[1] == "--dir":
        # 多文件模式（静态检查）
        input_dir = sys.argv[2] if len(sys.argv) > 2 else "."
        lang = sys.argv[3] if len(sys.argv) > 3 else None
        auto_fix_multifile(input_dir, lang=lang)
    elif sys.argv[1] == "--project":
        # 项目级模式（编译+运行）
        input_dir = sys.argv[2] if len(sys.argv) > 2 else "."
        lang = sys.argv[3] if len(sys.argv) > 3 else None
        auto_fix_project(input_dir, lang=lang)
    elif sys.argv[1] == "--layered":
        # 分层项目模式（jyn_test 结构）
        input_dir = sys.argv[2] if len(sys.argv) > 2 else "."
        auto_fix_layered(input_dir)
    elif sys.argv[1] == "--stats":
        # 统计模式（生成详细报告）
        input_path = sys.argv[2] if len(sys.argv) > 2 else "."
        lang = sys.argv[3] if len(sys.argv) > 3 else None
        auto_fix_with_stats(input_path, lang=lang)
    else:
        # 单文件模式
        input_file = sys.argv[1]
        lang = sys.argv[2] if len(sys.argv) > 2 else None
        auto_fix(input_file, lang=lang)
