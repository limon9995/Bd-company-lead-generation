from app.services.extractor import Source, extract_candidates, merge_and_score
from app.services.scoring import bucket


class FakeLLM:
    def __init__(self, out):
        self.out, self.prompts = out, []

    def generate_json(self, prompt, system=""):
        self.prompts.append(prompt)
        return self.out

    def generate_text(self, prompt, system="", max_tokens=400):
        return "Nice to see Acme growing in Dhaka."


WEB = [Source("website", "https://acme.com.bd/board", "Board of Directors\nDr. Rahim Uddin, Managing Director\nKarim Ahmed, Director")]


def test_hallucinated_names_are_discarded_and_inexact_quotes_penalised():
    llm = FakeLLM([
        {"name": "Dr. Rahim Uddin", "title": "Managing Director", "source_index": 0,
         "evidence_quote": "Dr. Rahim Uddin, Managing Director"},
        {"name": "Karim Ahmed", "title": "Director", "source_index": 0, "evidence_quote": "Karim is our director"},
        {"name": "Invented Person", "title": "CEO", "source_index": 0, "evidence_quote": "Invented Person, CEO"},
        {"name": "Bad Index", "title": "CEO", "source_index": 7, "evidence_quote": ""},
    ])
    cands = merge_and_score(extract_candidates(llm, "Acme Hospital", ["Managing Director"], WEB), ["Managing Director"])
    names = [c.name for c in cands]
    assert names == ["Dr. Rahim Uddin", "Karim Ahmed"]
    rahim, karim = cands
    assert rahim.confidence == 40 + 15 + 10 and bucket(rahim.confidence) == "medium"
    assert karim.confidence == 40 - 30
    assert "Acme Hospital" in llm.prompts[0]


def test_search_candidates_need_company_mention_and_merge_with_website():
    search = [
        Source("search", "https://bd.linkedin.com/in/rahim", "Rahim Uddin - Managing Director - Acme Hospital | LinkedIn"),
        Source("search", "https://news.com/x", "Rahim Uddin, managing director of Other Corp"),
    ]
    llm_web = FakeLLM([{"name": "Rahim Uddin", "title": "Managing Director", "source_index": 0,
                        "evidence_quote": "Rahim Uddin, Managing Director"}])
    llm_search = FakeLLM([
        {"name": "Rahim Uddin", "title": "Managing Director", "source_index": 0, "evidence_quote": "Rahim Uddin - Managing Director"},
        {"name": "Rahim Uddin", "title": "Managing Director", "source_index": 1, "evidence_quote": "Rahim Uddin, managing director"},
    ])
    c1 = extract_candidates(llm_web, "Acme Hospital", [], WEB)
    c2 = extract_candidates(llm_search, "Acme Hospital", [], search)
    assert len(c2) == 1  # the Other Corp snippet is rejected
    [m] = merge_and_score(c1 + c2, ["Managing Director"])
    assert m.on_website and m.in_search and m.linkedin_url == "https://bd.linkedin.com/in/rahim"
    assert m.confidence == 100 and bucket(m.confidence) == "high"


def test_llm_dict_wrapper_and_garbage_are_tolerated():
    assert extract_candidates(FakeLLM({"people": []}), "Acme", [], WEB) == []
    assert extract_candidates(FakeLLM("nonsense"), "Acme", [], WEB) == []
