"""Tests for server_watchdog.utils."""

import pytest

from server_watchdog.utils import _apply_inline_markdown, escape_html, markdown_to_html


# ── escape_html ───────────────────────────────────────────────────────────────

class TestEscapeHtml:
    def test_ampersand(self):
        assert escape_html("a & b") == "a &amp; b"

    def test_less_than(self):
        assert escape_html("<tag>") == "&lt;tag&gt;"

    def test_plain_text_unchanged(self):
        assert escape_html("hello world") == "hello world"


# ── _apply_inline_markdown ────────────────────────────────────────────────────

class TestApplyInlineMarkdown:
    def test_bold(self):
        assert _apply_inline_markdown("**hello**") == "<strong>hello</strong>"

    def test_inline_code(self):
        assert _apply_inline_markdown("`/etc/passwd`") == "<code>/etc/passwd</code>"

    def test_inline_code_processed_before_bold(self):
        # Asterisks inside a code span must not become <strong>
        result = _apply_inline_markdown("`**not bold**`")
        assert "<strong>" not in result
        assert "<code>" in result

    def test_bold_and_code_in_same_line(self):
        result = _apply_inline_markdown("see **this** and `that`")
        assert "<strong>this</strong>" in result
        assert "<code>that</code>" in result

    def test_no_markup_unchanged(self):
        assert _apply_inline_markdown("plain text") == "plain text"


# ── markdown_to_html ──────────────────────────────────────────────────────────

class TestMarkdownToHtml:
    def test_heading_h2(self):
        assert markdown_to_html("# Title") == "<h2>Title</h2>"

    def test_heading_h3(self):
        assert markdown_to_html("## Sub") == "<h3>Sub</h3>"

    def test_heading_h4(self):
        assert markdown_to_html("### Deep") == "<h4>Deep</h4>"

    def test_horizontal_rule(self):
        assert markdown_to_html("---") == "<hr>"
        assert markdown_to_html("------") == "<hr>"

    def test_blank_line_becomes_br(self):
        # A blank line within a document becomes <br>
        assert "<br>" in markdown_to_html("first\n\nsecond")
        # A whitespace-only line also becomes <br>
        assert "<br>" in markdown_to_html("first\n   \nsecond")

    def test_paragraph_bold(self):
        result = markdown_to_html("This is **important**.")
        assert "<strong>important</strong>" in result
        assert "<p>" in result

    def test_paragraph_inline_code(self):
        result = markdown_to_html("Edit `/etc/hosts` now.")
        assert "<code>/etc/hosts</code>" in result

    def test_bullet_dash_bold(self):
        """Bold inside a dash-bullet must be converted (the original bug)."""
        result = markdown_to_html("- **critical**: fix this")
        assert "<li>" in result
        assert "<strong>critical</strong>" in result
        # Must not leave raw asterisks
        assert "**" not in result

    def test_bullet_star_bold(self):
        """Bold inside a star-bullet must be converted."""
        result = markdown_to_html("* **warning**: check that")
        assert "<li>" in result
        assert "<strong>warning</strong>" in result
        assert "**" not in result

    def test_bullet_inline_code(self):
        result = markdown_to_html("- Update `/etc/tigervnc/vncserver.users`")
        assert "<li>" in result
        assert "<code>/etc/tigervnc/vncserver.users</code>" in result

    def test_bullet_html_escaped(self):
        result = markdown_to_html("- a < b & c > d")
        assert "&lt;" in result
        assert "&amp;" in result
        assert "&gt;" in result

    def test_multiline(self):
        text = "# Header\n- **item one**\n- item two\n\nParagraph."
        result = markdown_to_html(text)
        assert "<h2>Header</h2>" in result
        assert "<strong>item one</strong>" in result
        assert "<li>item two</li>" in result
        assert "<br>" in result
        assert "<p>Paragraph.</p>" in result
