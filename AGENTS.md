# 仓库约定

## 规则

- git commit 信息不携带 Co-Authored-By 等 agent 协作信息
- main code（各包 `src/`）禁止用 `getattr`/`setattr`/`hasattr`/`delattr` 的字符串成员名形态访问受保护成员；外部访问受保护成员须在所属类提供公有成员函数或 `@property`。该字符串形态仅限测试与 `examples/`。门禁：`tests/architecture/test_protected_member_escape.py`（棘轮冻结既有清单，只减不增）
- 所有发行包共享 `workflow_controller` PEP 420 命名空间：`workflow_controller/`、`workflow_controller/plugins/` 不携带 `__init__.py`，包内子目录的 `__init__.py` 随包维护
- 修改代码前先修改设计文档；修改或生成文档前，先阅读目标目录或项目相关的 `README.md`
- 内核测试住 `packages/kernel/kernel_tests/`（包名与仓根 `tests/` 错开），插件测试随插件包，跨包架构门禁与共享测试基建住仓根 `tests/`
- 注释是代码的补充，只有代码不能自表达的信息才需要注释；计划后续移除的过渡代码，其注释必须包含「删除」并说明何时可删
- 新增类或函数应有单行简洁注释
- 测试只保留必要用例；命名须易读，不限于单词；删除无用与冗余代码

## 质量门禁

```bash
uv sync
uv run ruff format --check . && uv run ruff check .
uv run lint-imports --no-cache
uv run codespell $(git ls-files '*.md')
uv run pytest -q
uv build --package <包名>   # 各发行包可独立构建
```
