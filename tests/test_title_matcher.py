from app.services.crawler import is_bot_wall
from app.services.extractor import Source, merge_and_score
from app.services.title_matcher import find_people

TARGETS = ["Chairman", "Vice Chancellor", "Managing Director", "CEO"]


def pairs(company, sources, targets=TARGETS):
    return [(c.name, c.title) for c in find_people(company, targets, sources)]


def test_title_first_list_with_degrees_pairs_each_title_with_the_following_name():
    text = ("Leadership\nThe Vice-Chancellor\nProfessor Syed Ferhat Anwar, PhD\n"
            "Pro-Vice-Chancellor\nProfessor Arshad Mahmud Chowdhury, PhD\nRequest for Information")
    assert pairs("BRAC University", [Source("website", "https://x.edu.bd/leadership", text)]) == [
        ("Professor Syed Ferhat Anwar", "Vice-Chancellor"),
        ("Professor Arshad Mahmud Chowdhury", "Pro-Vice-Chancellor"),
    ]


def test_name_first_cards_and_all_caps_names():
    text = "Board\nMd. Rahim Uddin\nManaging Director\nKarim Ahmed\nDirector\nChairman of the BoT\nMOHAMMED SHAMSUL ALAM"
    assert pairs("Acme Ltd", [Source("website", "https://acme.com.bd/board", text)]) == [
        ("Md. Rahim Uddin", "Managing Director"),
        ("Karim Ahmed", "Director"),
        ("Mohammed Shamsul Alam", "Chairman of the BoT"),
    ]


def test_menus_former_people_and_other_organisations_are_ignored():
    text = ("Administration\nThe Vice Chancellor\nMessage\nProfile\nThe Chairman\nThe Founders\n"
            "Important Links\nMessage from Chairman\n"
            "Mirza Fakhrul Islam Alamgir\nHon'ble President\nPeople's Republic of Bangladesh\n"
            "Karim Ahmed\nFormer Chairman\nEWU Directory\nDirectory\n"
            "Professor Choudhury was Chairman of Management Studies")
    assert pairs("Acme University", [Source("website", "https://acme.edu.bd/", text)]) == []


def test_company_acronym_slogans_are_not_people():
    text = "Go Ahead AUB\nFounder & Founder Vice Chancellor\nProf. Dr. Abul Hasan Muhammad Sadeq"
    assert pairs("Asian University of Bangladesh (AUB)", [Source("website", "https://aub.ac.bd/", text)]) == [
        ("Prof. Dr. Abul Hasan Muhammad Sadeq", "Founder & Founder Vice Chancellor")]


def test_news_and_department_pages_are_skipped():
    text = "Ms. Alexandra Khlevnoy\nDirector"
    for url in ("https://acme.edu.bd/news/3371", "https://acme.edu.bd/department/cse"):
        assert pairs("Acme", [Source("website", url, text)]) == []


def test_linkedin_search_results_are_read_when_they_mention_the_company():
    found = find_people("Acme Hospital", TARGETS, [
        Source("search", "https://bd.linkedin.com/in/rahim", "Rahim Uddin - CEO - Acme Hospital | LinkedIn — Dhaka"),
        Source("search", "https://bd.linkedin.com/in/other", "Jamal Khan - CEO - Other Corp | LinkedIn"),
    ])
    assert [(c.name, c.title, c.linkedin_url) for c in found] == [
        ("Rahim Uddin", "CEO", "https://bd.linkedin.com/in/rahim")]
    assert found[0].in_search and not found[0].on_website


def test_evidence_is_verbatim_so_confidence_is_not_penalised():
    web = [Source("website", "https://acme.com.bd/about", "Dr. Rahim Uddin, Managing Director")]
    (c,) = merge_and_score(find_people("Acme", TARGETS, web), TARGETS)
    assert c.evidence_exact and c.confidence == 40 + 15 + 10


def test_bangla_titles_and_names():
    text = ("অধ্যক্ষের বাণী\nপ্রফেসর মোঃ রহিম উদ্দিন\n"
            "গভর্নিং বডি\nসভাপতিঃ জনাব করিম আহমেদ\n"
            "যোগাযোগ\nমোঃ জামাল হোসেন\nসাবেক অধ্যক্ষ\n"  # former principal
            "আমাদের সম্পর্কে\nব্যবস্থাপনা পরিচালক\nআমাদের সম্পর্কে")
    assert pairs("Acme College", [Source("website", "https://acme.edu.bd/", text)], ["Principal"]) == [
        ("প্রফেসর মোঃ রহিম উদ্দিন", "অধ্যক্ষ (Principal)"),
        ("জনাব করিম আহমেদ", "সভাপতি (President)"),
    ]


def test_bangla_title_is_ranked_by_its_english_equivalent():
    web = [Source("website", "https://acme.com.bd/", "মোঃ রহিম উদ্দিন\nব্যবস্থাপনা পরিচালক")]
    (c,) = merge_and_score(find_people("Acme", TARGETS, web), TARGETS)
    assert c.title == "ব্যবস্থাপনা পরিচালক (Managing Director)" and c.rank == 1 and c.confidence == 65


def test_bangla_leadership_links_are_crawled_first():
    from app.services.crawler import rank_candidate_links

    links = [("https://acme.edu.bd/gallery", "Gallery"),
             ("https://acme.edu.bd/%E0%A6%85%E0%A6%A7%E0%A7%8D%E0%A6%AF%E0%A6%95%E0%A7%8D%E0%A6%B7", ""),  # /অধ্যক্ষ
             ("https://acme.edu.bd/p/12", "পরিচালনা পর্ষদ"),
             ("https://acme.edu.bd/trustees", "Board of Trustees")]
    ranked = rank_candidate_links(links, "acme.edu.bd")
    assert "https://acme.edu.bd/gallery" not in ranked
    assert set(ranked) == {links[1][0], links[2][0], links[3][0]}


def test_bot_wall_detection():
    assert is_bot_wall("www.x.edu\nPerforming security verification\nThis website uses a security service")
    assert not is_bot_wall("Welcome to Acme. Our Managing Director is Rahim Uddin.")
