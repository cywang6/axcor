# 任务：罗马数字转换器（romanconv）

实现一个小型罗马数字转换库，满足下述需求。worker 按 `spec → plan → execute → verify → retrospect`
五阶段推进，每阶段产物写到**工作根目录**，不要放到 **sample-project** 下：
`spec.md` / `plan.md` / `impl/` / `verify.md` / `retro.md`。

## 功能

两个函数（放在 `impl/romanconv/` 下）：

- `to_roman(n: int) -> str`：阿拉伯数字 → 罗马数字。
- `from_roman(s: str) -> int`：罗马数字 → 阿拉伯数字。

## 规则

- 符号：`I=1, V=5, X=10, L=50, C=100, D=500, M=1000`。
- 减法表示：`IV=4, IX=9, XL=40, XC=90, CD=400, CM=900`。
- 有效范围：`1..3999`。

## 验收（verify 阶段照这个验）

- `to_roman(1)=="I"`、`to_roman(4)=="IV"`、`to_roman(9)=="IX"`、`to_roman(58)=="LVIII"`、
  `to_roman(1994)=="MCMXCIV"`、`to_roman(3999)=="MMMCMXCIX"`。
- `from_roman("I")==1`、`from_roman("IV")==4`、`from_roman("MCMXCIX")==1999`、
  `from_roman("MMMCMXCIX")==3999`。
- 往返一致：`from_roman(to_roman(n)) == n` for `n in 1..3999`。

## 边界 case（spec 阶段应列出、verify 阶段应覆盖）

- `to_roman(0)` / 负数 / `>3999`：应如何处理（抛错 or 明确不支持）。
- `from_roman` 非法输入：空串、小写、乱序（`"IIII"`、`"IC"`）、含非法字符。
- 大小写：是否接受小写、是否归一。

> 这个 README 是**给 worker 的任务说明**——产物（`spec.md` 等）由 worker 在五阶段里产出，
> 不预置（预置会糊弄门禁）。`verify` 门禁本样例用 `non_placeholder`（领域无关、能跑）；
