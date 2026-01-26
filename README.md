# 多语言静态错误自动修复框架

> 负责人：何唐瑕  
> 交接给：朱宇帆（负责语义分析/运行时调试部分）

---

## 一、功能概述

本框架实现**静态语法错误**的自动检测与修复，支持以下语言：

| 语言 | 静态检查工具 | 编译器 |
|------|-------------|--------|
| Python | ruff + py_compile | - |
| Java | javac -Xlint | javac |
| C | gcc -fsyntax-only | gcc |
| C++ | g++ -fsyntax-only | g++ |

**核心能力：**
1. 静态语法错误检测（缺分号、括号不匹配、字符串未闭合等）
2. **175+ 条本地规则**自动修复（无需调用 LLM，零成本）
3. **依赖导入错误修复**（模块路径截断自动恢复）
4. **import 路径保护**（防止 LLM 注意力涣散截断模块名）
5. LLM（Qwen）辅助修复复杂语义错误
6. Docker 沙箱隔离执行
7. GCC/Clang fix-it hints 自动应用
8. **分层项目修复**（多文件依赖分析）

---

## 二、环境准备

### 1. 安装依赖
```bash
pip install openai ruff
```

### 2. 配置 API Key
在 `api.txt` 中填入 Qwen API Key：
```
sk-xxxxxxxxxxxxxxxxxxxxxxxx
```

### 3. 启动 Docker Desktop
框架依赖 Docker 进行沙箱测试，确保 Docker Desktop 已启动。

验证 Docker：
```bash
docker version
```

### 4. 拉取所需镜像（首次运行）
```bash
docker pull python:3.10-slim
docker pull amazoncorretto:17
docker pull gcc:12
```

---

## 三、使用方法

### 基本命令（单文件）
```bash
python auto_fix_multilang.py <待修复文件> [语言]
```

### 分层项目修复（多文件）
```bash
python auto_fix_multilang.py --layered <项目目录>
```

### 示例
```bash
# 单文件修复
python auto_fix_multilang.py test_samples/test_python.py
python auto_fix_multilang.py test_samples/test_java.java

# 分层项目修复（推荐）
python auto_fix_multilang.py --layered jyn_test
```

### 输出
- 单文件：`<原文件名>_fixed.<扩展名>`
- 分层项目：`<项目名>_fixed/` 目录

---

## 四、工作流程

```
┌─────────────────────────────────────────────────────────┐
│                    auto_fix_multilang.py                │
└─────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│  PHASE 1: 静态检查 + 本地规则修复                          │
│  ─────────────────────────────────────────────────────  │
│  5层修复优先级：                                            │
│    1️⃣ GCC/Clang fix-it hints 自动应用                     │
│    2️⃣ 编译器 "did you mean" 建议                          │
│    3️⃣ 特殊错误模式规则（如 stray ')'）                    │
│    4️⃣ 通用本地规则（175+条，分语法/语义/依赖）               │
│    5️⃣ LLM 辅助修复（仅当本地无法处理）                     │
└─────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│  PHASE 2: 语义校验 + import 路径保护                        │
│  ─────────────────────────────────────────────────────  │
│  - 检测 LLM 是否截断了 import 路径                       │
│  - 自动恢复截断的模块名 (step1 -> step1_parsing)          │
│  - 防止引入新错误                                           │
└─────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│  PHASE 3: 编译检查（仅 Java/C/C++）                          │
│  - 编译错误检测                                           │
│  - LLM 辅助修复复杂语义错误                               │
└─────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│  PHASE 4: 运行时检查 ← 朱宇帆负责扩展                     │
│  - 运行代码检测运行时错误                                 │
│  - 语义错误分析（待扩展）                                   │
└─────────────────────────────────────────────────────────┘
```

---

## 五、本地规则统计（175+条）

### 规则分布

| 语言 | 语法错误 | 语义错误 | 依赖错误 | 总计 |
|------|---------|---------|---------|------|
| Python | 20 | 4 | 6 | **30** |
| Java | 19 | 29 | 3 | **51** |
| C | 18 | 8 | 3 | **29** |
| C++ | 32 | 21 | 3 | **56** |
| **维护模块** | - | - | 9 | **9** |
| **总计** | **89** | **62** | **24** | **175+** |

### 新增依赖修复规则

| 规则 | 说明 |
|------|------|
| `ModuleNotFoundError` | 模块不存在，自动搜索项目中的完整模块名 |
| `ImportError: cannot import` | 无法从模块导入，恢复完整路径 |
| `relative import error` | 相对导入错误，转换为绝对导入 |
| `import 路径截断` | LLM 截断模块名，自动恢复 |

---

## 六、分层项目修复（--layered）

适用于多文件 Python 项目，自动分析依赖关系并按层修复。

### 使用方法
```bash
python auto_fix_multilang.py --layered <项目目录>
```

### 支持的项目结构
```
项目目录/
├── data_structure/     # L0 基础层（先修复）
│   ├── __init__.py
│   ├── model1.py
│   └── model2.py
├── code/               # L1 逻辑层（依赖 data_structure）
│   ├── step1_xxx.py
│   └── step2_xxx.py
└── run.py              # L2 入口层（最后修复）
```

### 修复顺序
1. **L0 基础层**: `data_structure/` 下的所有文件
2. **L1 逻辑层**: `code/` 下的所有文件
3. **L2 入口层**: `run.py` 等入口文件

### 输出示例
```
[LAYERED PROJECT FIX] 分层项目修复

[结构检测]
  data_structure/: 7 个文件
  code/:           6 个文件
  入口文件:        ['run.py']

[修复顺序] 共 14 个文件
  1. [L0-基础] data_structure/__init__.py
  ...
  14. [L2-入口] ./run.py

[SUMMARY] 分层项目修复完成
总文件数: 14
已修改: 9 个文件
```

---

## 六、核心文件说明

| 文件 | 功能 |
|------|------|
| `auto_fix_multilang.py` | **主入口**，多语言修复框架（含 147 条本地规则） |
| `sandbox_executor.py` | Docker 沙盒执行器 |
| `static_checker.py` | 静态检查工具封装 |
| `run_auto_fix.py` | LLM 调用封装（get_llm_query_func） |
| `prompts/debug_fix.md` | 调试用提示词模板 |
| `test_samples/` | 测试用例（带错误 + 修复后对比） |

---

## 七、框架架构图

```
auto_fix_multilang.py
├── LANG_CONFIG              # 语言配置（Docker镜像、编译命令、linter等）
├── LOCAL_FIX_RULES          # 175+条本地修复规则
│   ├── python: 30条          #   - 导入、空白、语法、缩进、依赖等
│   ├── java: 51条            #   - 分号、括号、字符串、控制流、语义等
│   ├── c: 29条               #   - 分号、括号、字符串、符号解析等
│   └── cpp: 56条             #   - 分号、括号、作用域、模板等
├── 修复函数
│   ├── 通用函数              # add_semicolon_at_line, fix_missing_paren...
│   ├── Python特有            # fix_python_module_not_found, fix_python_import_error...
│   ├── Java特有              # fix_java_init_variable, fix_java_missing_return...
│   └── C++特有               # fix_add_typename, fix_undeclared_symbol...
├── 依赖修复模块
│   ├── scan_project_modules()      # 扫描项目模块结构
│   ├── fix_python_module_not_found()  # 修复模块不存在
│   ├── fix_python_import_error()      # 修复导入错误
│   ├── fix_python_relative_import()   # 修复相对导入
│   └── fix_python_import_truncated()  # 修复截断的import
├── 语义校验模块
│   ├── validate_fix_semantics()    # 验证修复语义
│   ├── validate_import_paths()     # 验证import路径完整性
│   └── safe_apply_fix()            # 安全应用修复(自动恢复截断)
├── 编译器集成
│   ├── parse_gcc_fixits()    # 解析 GCC/Clang fix-it hints
│   ├── apply_fixits()        # 应用 fix-it 建议
│   └── extract_compiler_suggestions()  # 提取 "did you mean" 建议
├── try_local_fix()          # 5层优先级修复入口
├── fix_code()               # 单文件修复流程
└── fix_layered_project()    # 分层项目修复流程
```

---

## 八、测试用例说明

`test_samples/` 目录包含各语言的测试文件：

| 文件 | 错误类型 | 预期修复 |
|------|----------|----------|
| `test_python.py` | 除零错误 | 添加除零检查 |
| `test_java.java` | 缺分号 + 除零 | 补分号 + 除零检查 |
| `test_c.c` | 缺分号 + 除零 | 补分号 + 除零检查 |
| `test_cpp.cpp` | 缺分号 + 除零 | 补分号 + 除零检查 |
| `groot_test/` | C++语法错误(107个) | 本地规则修复率 100% |

运行测试验证框架：
```bash
python auto_fix_multilang.py test_samples/test_python.py
# 预期输出：[SUCCESS] 代码执行成功!
```

---

## 九、扩展指南（给朱宇帆）

### 你需要扩展的部分：PHASE 3 运行时/语义分析

当前 PHASE 3 只做简单的运行时错误捕获，你可以：

1. **结合文章需求描述**进行语义分析
2. **扩展错误分类**（区分语法错误 vs 语义错误）
3. **添加测试用例构建逻辑**

### 关键函数位置

```python
# auto_fix_multilang.py

# 5层修复优先级入口
def try_local_fix(code, error_msg, lang):
    """
    修复优先级：
    1. GCC/Clang fix-it hints
    2. 编译器 "did you mean" 建议
    3. 特殊错误模式
    4. 通用本地规则
    5. LLM 辅助
    """

# 主修复流程
def fix_code(input_file, lang=None):
    # Phase 1: 静态检查 + 本地规则
    # Phase 2: 编译检查
    # Phase 3: 运行时检查 ← 你可以扩展这里
```

### 本地规则添加方法

如果需要添加新规则：

```python
# 1. 在 LOCAL_FIX_RULES 中添加规则
LOCAL_FIX_RULES = {
    "java": {
        # [语法错误] 分类
        r"你的错误模式": lambda m, code: your_fix_func(code, ...),
        # [语义错误] 分类 - 需要 LLM
        r"复杂错误模式": None,
    }
}

# 2. 实现对应的修复函数
def your_fix_func(code, line_num):
    lines = code.split('\n')
    # ... 修复逻辑
    return '\n'.join(lines)
```

### 建议新增文件

- `semantic_analyzer.py` - 语义分析模块
- `test_case_builder.py` - 测试用例构建

---

## 十、常见问题

### Q1: Docker 连接失败
```
Error: Cannot connect to Docker daemon
```
**解决**：启动 Docker Desktop，等待托盘图标变绿。

### Q2: 镜像拉取失败
```
Error: failed to resolve reference
```
**解决**：检查网络，或使用国内镜像源。

### Q3: LLM 解析失败
```
[WARN] LLM 解析失败: Expecting value
```
**原因**：Qwen 返回格式不规范，框架会自动重试。

### Q4: 本地规则未触发
**检查**：查看错误信息是否匹配 `LOCAL_FIX_RULES` 中的正则。

---

## 十一、联系方式

- **框架开发**：何唐瑭
- **语义分析扩展**：朱宇帆

如有问题，联系何唐瑭。
