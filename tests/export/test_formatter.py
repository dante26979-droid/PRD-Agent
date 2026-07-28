from prd_agent.export.formatter import FeishuExportFormatter
from prd_agent.export.models import ExportBlockType


def test_formatter_creates_deterministic_supported_blocks_and_hash():
    markdown = """# 订单筛选

## 目标

支持创建时间筛选。

- 保留未知项
- 不泄露内部状态

> 风险：历史数据时区待确认。

| 字段 | 类型 |
| --- | --- |
| start_at | datetime |
"""
    formatter = FeishuExportFormatter()

    first = formatter.format(
        markdown,
        document_version=3,
        unresolved_items=("历史数据时区待确认",),
    )
    second = formatter.format(
        markdown,
        document_version=3,
        unresolved_items=("历史数据时区待确认",),
    )

    assert first.title == "订单筛选"
    assert [block.block_type for block in first.blocks] == [
        ExportBlockType.HEADING,
        ExportBlockType.HEADING,
        ExportBlockType.PARAGRAPH,
        ExportBlockType.UNORDERED_LIST,
        ExportBlockType.QUOTE,
        ExportBlockType.TABLE,
    ]
    assert first.blocks[1].level == 2
    assert first.blocks[3].items == ("保留未知项", "不泄露内部状态")
    assert first.content_hash == second.content_hash
