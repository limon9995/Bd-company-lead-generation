from app.services.normalize import (find_bd_phones, find_emails, name_key, normalize_bd_phone, normalize_domain,
                                    social_kind)


def test_domain_normalization_handles_bd_cctld_and_social_hosts():
    assert normalize_domain("https://www.squarehospital.com/about") == "squarehospital.com"
    assert normalize_domain("http://www.nsu.edu.bd") == "nsu.edu.bd"
    assert normalize_domain("https://shop.example.com.bd/x") == "example.com.bd"
    assert normalize_domain("https://www.facebook.com/SomeClinic") is None
    assert normalize_domain("https://m.facebook.com/SomeClinic") is None
    assert normalize_domain("") is None


def test_social_kind():
    assert social_kind("https://www.facebook.com/abc") == "facebook"
    assert social_kind("https://bd.linkedin.com/company/abc") == "linkedin"
    assert social_kind("https://example.com") is None


def test_bd_phone_normalization():
    assert normalize_bd_phone("01712-345678") == "+8801712345678"
    assert normalize_bd_phone("+880 1712 345678") == "+8801712345678"
    assert normalize_bd_phone("02-9661491") == "+88029661491"
    phones = find_bd_phones("Call 01812345678 or +88 01912-345678. Office: 02-58811234")
    assert "+8801812345678" in phones and "+8801912345678" in phones and "+880258811234" in phones


def test_email_extraction_with_obfuscation_and_junk():
    text = "Mail info [at] clinic [dot] com.bd or md@clinic.com.bd. logo@2x.png"
    assert find_emails(text) == ["info@clinic.com.bd", "md@clinic.com.bd"]


def test_name_key_strips_company_suffixes():
    assert name_key("Square Hospitals Ltd.") == name_key("SQUARE HOSPITALS LIMITED")
