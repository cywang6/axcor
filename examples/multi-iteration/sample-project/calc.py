# Copyright (c) 2026 Workflow Controller Contributors
#
# Licensed under the MIT License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://opensource.org/licenses/MIT
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""计算器核心逻辑：parse 算术表达式并计算结果。

例如：evaluate("1+2*3") → 7.0；evaluate("(1+2)*3") → 9.0
支持 + - * / 和括号、一元负号。
"""

import ast
import operator

# AST 节点类型 → 对应的运算函数
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def evaluate(expr: str) -> float:
    """计算算术表达式，返回 float。

    >>> evaluate("1+2*3")
    7.0
    >>> evaluate("(1+2)*3")
    9.0
    """
    tree = ast.parse(expr, mode="eval")
    return _eval_node(tree.body)


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):  # 数字字面量
        return float(node.value)
    if isinstance(node, ast.BinOp):  # 二元运算 a op b
        op = _OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"不支持的运算符: {type(node.op).__name__}")
        return op(_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp):  # 一元运算（如 -3）
        op = _OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"不支持的一元运算符: {type(node.op).__name__}")
        return op(_eval_node(node.operand))
    raise ValueError(f"不支持的表达式节点: {ast.dump(node)}")
