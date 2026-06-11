"""
services/sanitizer.py — UniGrade HTML Sanitization Service

CRITICAL RULE (CLAUDE.md §6.1):
    NEVER send raw HTML from streamlit-quill to the Groq API.
    ALWAYS call strip_html_tags() before constructing any AI prompt.
    Store raw HTML in student_responses.answer_text,
    plain text in student_responses.sanitized_text.
"""

from bs4 import BeautifulSoup

# Tags whose presence in Quill output implies a logical line break.
# <p>  — paragraph blocks
# <br> — explicit line break inside a paragraph
# <li> — ordered or unordered list item
_NEWLINE_TAGS = ["p", "br", "li"]


def strip_html_tags(html_content: str) -> str:
    """
    Strip all HTML tags from Quill rich-text output and return plain text.

    Newlines are preserved for structural tags (<p>, <br>, <li>) by inserting
    a newline character immediately before each such tag in the parse tree.
    This means a student answer like:

        <p>Osmosis is the movement of water</p><p>across a membrane.</p>

    becomes:

        Osmosis is the movement of water
        across a membrane.

    rather than the tag-stripped-but-concatenated form:

        Osmosis is the movement of wateracross a membrane.

    Args:
        html_content: Raw HTML string from streamlit-quill, or None/empty.

    Returns:
        Plain-text string with structural whitespace preserved, leading/
        trailing whitespace stripped. Returns "" for None or empty input.
    """
    if not html_content or not html_content.strip():
        return ""

    soup = BeautifulSoup(html_content, "html.parser")

    # Insert a newline NavigableString before each structural tag so that
    # get_text() picks it up as part of the text stream.
    for tag in soup.find_all(_NEWLINE_TAGS):
        tag.insert_before("\n")

    return soup.get_text(separator="").strip()