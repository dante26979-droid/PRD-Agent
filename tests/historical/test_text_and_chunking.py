from prd_agent.historical.chunking import chunk_markdown
from prd_agent.historical.text import normalize_markdown, safe_excerpt, tokenize


def test_normalization_removes_active_html_and_normalizes_unicode_and_newlines():
    value = "Cafe\u0301\r\n<script>alert(1)</script>\r\n## 规则\r\n正文"

    normalized = normalize_markdown(value)

    assert normalized == "Café\n\n## 规则\n正文"
    assert "script" not in normalized


def test_chinese_tokenizer_emits_characters_bigrams_and_ascii_words():
    tokens = tokenize("订单 Status V2")

    assert {"订", "单", "订单", "status", "v2"}.issubset(tokens)


def test_chunking_preserves_section_path_is_bounded_and_deterministic():
    markdown = "# 产品\n\n简介\n\n## 规则\n\n" + ("订单状态说明。" * 180)

    first = chunk_markdown(
        markdown,
        document_version_id="docver-" + "a" * 32,
        title="订单产品",
        target_limit=300,
        hard_limit=400,
    )
    second = chunk_markdown(
        markdown,
        document_version_id="docver-" + "a" * 32,
        title="订单产品",
        target_limit=300,
        hard_limit=400,
    )

    assert first == second
    assert max(len(item.content) for item in first) <= 400
    assert ("产品", "规则") in {item.section_path for item in first}
    assert [item.ordinal for item in first] == list(range(len(first)))


def test_safe_excerpt_redacts_secrets_and_applies_length_limit():
    excerpt, changed = safe_excerpt("password=secret " + "x" * 100, limit=40)

    assert changed
    assert "secret" not in excerpt
    assert len(excerpt) <= 40
