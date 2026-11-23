"""BODYSTRUCTURE responses as parsed by imapclient, captured from GreenMail 2.1.3 (20.11.2026).

``imapclient.response_types.BodyData`` is a tuple subclass; plain tuples/lists behave the same for the parser.
"""

from __future__ import annotations

from typing import Any

PLAIN: Any = (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"8bit", 15, 1, None, None, None)
HTML_ONLY: Any = (b"text", b"html", (b"charset", b"utf-8"), None, None, b"7bit", 8, 1, None, None, None)
ALTERNATIVE_WITH_PDF: Any = (
    [
        (
            [
                (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", 1, 1, None, None, None),
                (b"text", b"html", (b"charset", b"utf-8"), None, None, b"7bit", 8, 1, None, None, None),
            ],
            b"alternative",
            (b"boundary", b"===============2433144652286236505=="),
            None,
            None,
        ),
        (
            b"APPLICATION",
            b"PDF",
            None,
            None,
            None,
            b"base64",
            16,
            None,
            (b"attachment", (b"filename*", b"utf-8''Rechnung%20M%C3%A4rz.pdf")),
            None,
        ),
    ],
    b"mixed",
    (b"boundary", b"===============4613726095660933168=="),
    None,
    None,
)
RELATED_INLINE_IMAGE: Any = (
    [
        (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", 1, 1, None, None, None),
        (b"IMAGE", b"PNG", None, b"<img1@x>", None, b"base64", 8, None, (b"inline", None), None),
    ],
    b"related",
    (b"boundary", b"===============2598071263002745766=="),
    None,
    None,
)
FORWARDED_MESSAGE: Any = (
    [
        (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", 12, 1, None, None, None),
        (
            b"MESSAGE",
            b"RFC822",
            None,
            None,
            None,
            b"8bit",
            123,
            (b"null", b"inner", None, None, None, None, None, None, None, None),
            (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", 10, 1),
            6,
            None,
            (b"attachment", None),
            None,
        ),
    ],
    b"mixed",
    (b"boundary", b"===============4267404389974175612=="),
    None,
    None,
)
TEXT_ATTACHMENT: Any = (
    [
        (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", 4, 1, None, None, None),
        (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", 5, 1, None, (b"attachment", (b"filename", b"notes.txt")), None),
    ],
    b"mixed",
    (b"boundary", b"===============5343944213710562776=="),
    None,
    None,
)
