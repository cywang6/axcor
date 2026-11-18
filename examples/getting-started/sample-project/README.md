# wordstat

一个迷你词频统计工具 —— **Workflow Controller getting-started demo 的探索目标**。

worker（由 Workflow Controller 驱动）会读这些源文件、理解架构、写一份 `SUMMARY.md`。
你可以在 demo 中实时观测它读文件、思考、写产出的全过程，并中途暂停 / 改方向 / 继续。

## 用法

```bash
python -m wordstat.cli example.txt
```

输出 `example.txt` 中出现次数最多的前 10 个词。

## 结构

| 文件 | 职责 |
|------|------|
| `wordstat/counter.py` | 词频统计核心（`count_words` / `top_n` / `count_file`） |
| `wordstat/filters.py` | 停用词过滤（`STOPWORDS` / `remove_stopwords`） |
| `wordstat/cli.py` | 命令行入口 |
| `example.txt` | 样例文本 |

> 这个小项目**故意留了几个可改进点**（大小写未归一、标点未过滤、停用词未接入主流程、无测试）——
> 这样 worker 写出的 SUMMARY.md 才有内容，也方便你观测它的分析过程。
