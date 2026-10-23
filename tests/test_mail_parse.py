from email.message import EmailMessage

from gateway.connectors.mail.parse import addresses, build_draft, decode_words, parse_message


def make(html=False, attach=False) -> bytes:
    m = EmailMessage()
    m["From"] = "=?utf-8?q?J=C3=BCrgen?= <j@example.org>"
    m["To"] = "me@example.com"
    m["Subject"] = "=?utf-8?b?R3LDvMOfZQ==?="
    m["Date"] = "Sat, 17 Oct 2026 10:00:00 +0200"
    m["Message-ID"] = "<orig@example.org>"
    m["References"] = "<root@example.org>"
    if html:
        m.set_content("<html><body><p>Hallo <b>Welt</b></p><script>x()</script></body></html>", subtype="html")
    else:
        m.set_content("Hallo Welt\n" + "x" * 50)
    if attach:
        m.add_attachment(b"%PDF-1.4 data", maintype="application", subtype="pdf", filename="rechnung.pdf")
    return m.as_bytes()


def test_headers_decoded_and_text_body():
    out = parse_message(make(), 1000)
    assert out["from"] == "Jürgen <j@example.org>" and out["subject"] == "Grüße"
    assert out["date"] == "2026-10-17T10:00:00+02:00"
    assert out["body"].startswith("Hallo Welt") and out["body_format"] == "plain" and not out["truncated"]


def test_html_to_text_cap_and_attachments():
    out = parse_message(make(html=True, attach=True), 1000)
    assert "Hallo Welt" in out["body"] and "<b>" not in out["body"]
    assert out["attachments"] == [{"filename": "rechnung.pdf", "content_type": "application/pdf", "size": 13}]
    assert parse_message(make(), 5)["truncated"] is True


def test_reply_draft_threads():
    orig = parse_message(make(), 1000)
    d = build_draft("me@example.com", ["j@example.org"], [], "", "Danke!", orig)
    assert d["Subject"] == "Re: Grüße" and d["In-Reply-To"] == "<orig@example.org>"
    assert d["References"] == "<root@example.org> <orig@example.org>"
    assert d["Message-ID"].endswith("@example.com>")


def test_addresses_and_words():
    assert addresses("A <A@x.org>, b@y.org", None, "") == {"a@x.org", "b@y.org"}
    assert decode_words(b"=?iso-8859-1?q?M=FCller?=") == "Müller"
