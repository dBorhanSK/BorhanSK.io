import pytest

from telegram_web import TelegramWeb, TelegramWebError


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+491234567890", "+491234567890"),
        ("+49 123 456 7890", "+491234567890"),
        ("0049 123 456 7890", "+491234567890"),
        ("+۹۸ ۹۱۲ ۳۴۵ ۶۷۸۹", "+989123456789"),
        ("+٩٦٦ ٥٠ ١٢٣ ٤٥٦٧", "+966501234567"),
    ],
)
def test_normalise_phone_accepts_international_and_unicode_digits(raw, expected):
    assert TelegramWeb._normalise_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "123456789", "+123", "+123456789012345678", "+49+123456789"])
def test_normalise_phone_rejects_invalid_numbers(raw):
    with pytest.raises(TelegramWebError):
        TelegramWeb._normalise_phone(raw)
