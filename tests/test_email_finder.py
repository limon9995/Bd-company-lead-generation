from app.services.email_finder import choose_email


def test_prefers_personal_then_company_then_guess():
    assert choose_email("Rahim Uddin", ["info@acme.com.bd", "rahim@acme.com.bd"], "acme.com.bd") == ("rahim@acme.com.bd", "found")
    assert choose_email("Rahim Uddin", ["sales@acme.com.bd", "info@acme.com.bd"], "acme.com.bd") == ("info@acme.com.bd", "company")
    assert choose_email("Rahim Uddin", [], "acme.com.bd", mx_check=lambda d: True) == ("rahim@acme.com.bd", "guessed")
    assert choose_email("Rahim Uddin", [], "acme.com.bd", mx_check=lambda d: False) == ("", "unknown")
    assert choose_email("", ["acmehospital@gmail.com"], None) == ("acmehospital@gmail.com", "company")
