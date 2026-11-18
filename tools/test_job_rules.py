import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_rules import engagement_of, qualification, title_match


class ConfiguredTitleFilterTests(unittest.TestCase):
    def test_explicit_software_roles_are_not_rejected_for_department_words(self):
        for title in ('Rates and Credit Pricing Engineering Lead',
                      'Software Engineering Manager - International Pricing',
                      'Software developer In Test, Sales Engineering'):
            with self.subTest(title=title):
                self.assertTrue(title_match(title, self.title_filter)[0])
        for title in ('Pricing Manager', 'Sales Engineering Director',
                      'Account Executive - Software Engineering', 'Junior Software Engineer in Test'):
            with self.subTest(title=title):
                self.assertFalse(title_match(title, self.title_filter)[0])

    def test_role_equivalents_are_candidates_without_enumerating_seniority(self):
        for title in (
            'Principal QA Engineer', 'Staff QA Engineer', 'Senior QA Engineer – (Security)',
            'Senior Quality Assurance Engineer', 'Quality Assurance Lead', 'QA Engineer Lead',
            'Software Engineer in Test / Automation Tester',
            'Software Dev. Engineer in Test, Conversational shopping',
            'Test Automation Manager - London', 'Principal Test Engineer', 'Sr. Test Analyst',
            'Test Automation Engineer', 'Automation Tester',
            'Founding Senior Engineer', 'Founding Senior Software Engineer', 'Founding FDE',
            'Founding Deployments Engineer', 'Forward-Deployed Engineer',
            'Lead Software Engineer', 'Lead Backend Engineer', 'Engineering Team Lead',
            'Team Lead (Engineering)', 'Tech Lead Manager - AI Squad', 'VP, Engineering',
            'Head of Software Development - DevEx & Agentic AI',
            'Head of Product & Engineering - Web', 'Software Engineer (Leadership) - Product',
            'Head of Product and Engineering - AI', 'Head of Applied AI',
            'Director, Software Engineering (Platform)', 'Director, Engineering - Partnerships',
            'Technical Team Lead', 'Engineering Site Lead', 'Principal Backend Engineer',
            'Founding Machine Learning Engineer', 'Artificial Intelligence Engineer',
            'Lead Software Developer In Test', 'Quality and Testing Manager',
            'Director of Technology', 'Director – AI Engineering',
            'Technical Deployment Lead', 'Deployed Engineer - Europe',
            'Head of Artificial Intelligence', 'Principal, Quality Eng; Data & Analytics',
            'Head of Development', 'Senior Specialist, Quality Assurance',
            'Senior Manager, Data Engineering', 'Sr Manager Quality Assurance',
            'Director - Hyperion Platform Lead, Software Engineering',
            'Principle Engineer - Director', 'Senior Software Engineer (ML QA)',
            'Technology Manager', 'Team Lead, AI & Machine Learning',
            'Technical Director | Digital Engineering & AI | UK',
            'Senior Director of Agentic Software Development', 'AI Research Engineer, Post-Training',
            'Technology Director Lead Architect', 'Director, Security Engineering & Operations',
            'Senior Lead - AI Engineering', 'AI Native SW Engineer - Manager / Director',
            'Microsoft AI Senior Engineer', 'Global AI/ML Engineer Senior Manager',
            'Director, Quality & Release Engineering',
            'Director, Applications and AI Automation', 'Director – AI & Innovation',
            'Staff Validation Systems Engineer, AI Validation', 'AI and Data Verification Engineer',
            'Data & AI Lead', 'Forward Development Engineer', 'Founding Robot Learning Engineer',
            'Director, Full-Stack Engineer (10x)', 'Former Technical Founders',
            'Assurance Manager (Tech) (Rgn)',
        ):
            with self.subTest(title=title):
                self.assertTrue(title_match(title, self.title_filter)[0])

    def test_role_equivalents_preserve_negative_and_word_boundaries(self):
        for title in ('Junior QA Engineer', 'Graduate Software Engineer in Test',
                      'Staff QA Engineer - SC Cleared', 'Manual Automation Tester',
                      'QA Lead - SC-Cleared',
                      'Mechanical Lead Software Engineer', 'Hardware Test Engineer',
                      'Sales Forward-Deployed Engineer', 'Civil Engineering Team Lead',
                      'QA Engineering Assistant', 'Founding Investor', 'Test Equipment Operator',
                      'Quality Assurance Pharmacovigilance', 'Lead Backend Engineering Assistant',
                      'Director of Toy Quality & Safety', 'AI Data Entry Assistant',
                      'Junior AI Verification Engineer', 'Former Technical Founders - Sales'):
            with self.subTest(title=title):
                self.assertFalse(title_match(title, self.title_filter)[0])

    def test_recent_alert_title_gaps_keep_narrow_relevant_roles(self):
        for title in ('Vice President of Engineering', 'Technical Lead', 'Lead Engineer - Platform',
                      'Staff Software Engineer', 'Senior Software Development Engineer in Test',
                      'QA Automation Engineer', 'Applied AI Lead, Health', 'Applied AI & ML Lead',
                      'Engineering Leader', 'Deployment Engineer - London'):
            with self.subTest(title=title):
                self.assertTrue(title_match(title, self.title_filter)[0])
        for title in ('Quality Director Pharmaceuticals', 'Director GCP Quality Assurance',
                      'Junior Staff Software Engineer', 'Mechanical Lead Engineer'):
            with self.subTest(title=title):
                self.assertFalse(title_match(title, self.title_filter)[0])

    @classmethod
    def setUpClass(cls):
        config = Path(__file__).resolve().parents[1] / "config" / "portals.yml"
        cls.title_filter = yaml.safe_load(config.read_text())["title_filter"]

    def test_leadership_variants_observed_in_original_alerts_are_candidates(self):
        for title in (
            "Director of Software Engineering",
            "Engineering Director",
            "Director of Product Engineering",
            "Chief Technology Officer",
            "SVP, Engineering",
            "European Head of Production Engineering",
        ):
            with self.subTest(title=title):
                self.assertTrue(title_match(title, self.title_filter)[0])
                self.assertTrue(title_match(title.lower(), self.title_filter)[0])

    def test_existing_alert_search_families_are_candidates(self):
        for title in ("Director of Quality", "Founding Software Engineer"):
            with self.subTest(title=title):
                self.assertTrue(title_match(title, self.title_filter)[0])

    def test_ai_testing_titles_observed_in_recovered_alerts_are_candidates(self):
        for title, alias in (
            ("Lead AI Test Engineer", "AI Test Engineer"),
            ("Industry Principal - AI-first Strategic QE, AI Led QE, AI Assurance", "AI Assurance"),
        ):
            with self.subTest(title=title):
                self.assertEqual(title_match(title, self.title_filter), (True, f"title:{alias}"))
                self.assertEqual(title_match(title.lower(), self.title_filter), (True, f"title:{alias}"))
                self.assertEqual(qualification({"title": title})["engagement"], "unknown")

    def test_ai_testing_aliases_preserve_clearance_junior_and_sales_vetoes(self):
        for title, negative in (
            ("Lead AI Test Engineer - SC Cleared", "SC Cleared"),
            ("AI Assurance Consultant - Clearance required", "Clearance"),
            ("Junior AI Test Engineer", "Junior"),
            ("Junior AI Assurance Consultant", "Junior"),
            ("AI Test Engineer - Sales", "Sales"),
            ("AI Assurance Sales Consultant", "Sales"),
        ):
            with self.subTest(title=title):
                self.assertEqual(title_match(title, self.title_filter), (False, f"negative:{negative}"))

    def test_ai_testing_aliases_do_not_admit_generic_qe_or_partial_words(self):
        for title in ("Strategic QE", "AI Led QE", "QE Consultant", "Lead AI Test Engineering",
                      "AI Assurances Consultant"):
            with self.subTest(title=title):
                self.assertEqual(title_match(title, self.title_filter), (False, "no-title-match"))

    def test_leadership_aliases_keep_existing_negative_vetoes(self):
        for title, negative in (
            ("Sales Engineering Director", "Sales"),
            ("Head of Legal Engineering, EMEA", "Legal"),
            ("Director of Software Engineering — Manufacturing", "Manufacturing"),
            ("Head of Production Engineering — Mechanical", "Mechanical"),
            ("Junior Founding Software Engineer", "Junior"),
        ):
            with self.subTest(title=title):
                self.assertEqual(title_match(title, self.title_filter),
                                 (False, f"negative:{negative}"))

    def test_aliases_do_not_admit_generic_titles_or_confirm_unknown_terms(self):
        for title in ("Director", "Production Manager", "Head of Procurement",
                      "Site Lead, Data & ML", "Founding Software Engineering Assistant"):
            with self.subTest(title=title):
                self.assertEqual(title_match(title, self.title_filter), (False, "no-title-match"))
        result = qualification({"title": "European Head of Production Engineering"})
        self.assertEqual((result["domain"], result["engagement"]), ("unknown", "unknown"))
        physical = qualification({"title": "Head of Production Engineering",
                                  "text": "Manage mechanical engineering and plant maintenance on a manufacturing site."})
        self.assertEqual((physical["domain"], physical["eligible"]), ("outside", False))


class QualificationTests(unittest.TestCase):
    def test_owned_llm_delivery_establishes_software_remit_without_software_word(self):
        for text in (
            'The work is primarily Python across an AI engineering stack, including agent workflows and RAG.',
            'Your work involves Python, LLM orchestration, guardrails and evals.',
            'You will build production LLM systems with retrieval and evaluation pipelines.',
            'Design LLM agent workflows and guardrails for manufacturing plant customers.',
        ):
            with self.subTest(text=text):
                self.assertEqual(qualification(dict(title='Forward Deployed Engineer', text=text))['domain'], 'software')

    def test_llm_mentions_need_owned_nonnegated_delivery(self):
        for text in (
            'Our other team builds production LLM systems.',
            'You will not build production LLM systems.',
            'The work is not Python across an AI engineering stack.',
            'Manage facilities maintenance using production LLM systems.',
            'The work is mainly Python handling equipment diagnostics.',
            'Build production LLM systems while another team owns delivery.',
        ):
            for physical in ('', ' Manage electrical engineering at a manufacturing plant.'):
                with self.subTest(text=text, physical=physical):
                    expected = 'outside' if physical or 'facilities maintenance' in text else 'unknown'
                    self.assertEqual(qualification(dict(title='Forward Deployed Engineer', text=text+physical))['domain'], expected)

    def test_compensation_bonus_does_not_create_a_desirable_candidate_requirement(self):
        from job_rules import requirements
        for text in ('Engineering Director | £100,000 - £135,000 | Annual Bonus',
                     'Salary £100,000 plus bonus.', 'We offer a discretionary bonus.',
                     'You will receive a performance bonus.'):
            with self.subTest(text=text):
                self.assertEqual(requirements(text), ([], False))
        for text in ('Experience with Snowflake would be a bonus.',
                     'Python experience is a bonus.', 'Bonus points for experience with Snowflake.'):
            with self.subTest(text=text):
                result, truncated = requirements(text)
                self.assertEqual([(item['text'], item['importance']) for item in result], [(text, 'desirable')])
                self.assertFalse(truncated)

    def test_flattened_role_skills_and_offer_headings_keep_requirements_separate(self):
        from job_rules import requirements
        for heading in ('Your Skills & Experience', 'About You'):
            with self.subTest(heading=heading):
                text = ('Engineering Director | £100,000 - £135,000 | Annual Bonus '
                        'The Role Build data integrations. '
                        + heading + ' Experience with cloud data platforms. '
                        'What They Offer Fully remote working.')
                result, truncated = requirements(text)
                self.assertEqual([(item['text'], item['importance']) for item in result],
                                 [('Experience with cloud data platforms.', 'ambiguous')])
                self.assertFalse(truncated)

    def test_incidental_software_tools_do_not_override_physical_duties(self):
        for title, text in (
            ('Head of Quality Engineering', 'Lead quality engineering at a manufacturing plant, responsible for electrical engineering. Use software testing tools for equipment diagnostics.'),
            ('QA Manager', 'Own food safety and HACCP compliance. Use test automation tools to inspect production equipment.'),
            ('Engineering Manager', 'Manage facilities maintenance using software applications for scheduling.'),
            ('Engineering Manager', 'Manage facilities maintenance and use software applications for scheduling.'),
            ('Head of Quality Engineering — AI-enabled manufacturing', 'Lead quality engineering at a manufacturing plant, responsible for electrical engineering. Use software testing tools for equipment diagnostics.'),
            ('Head of Quality Engineering', 'Lead quality engineering at a manufacturing plant, responsible for electrical engineering. Implement software testing tools for equipment diagnostics.'),
        ):
            with self.subTest(title=title):
                result = qualification(dict(title=title, text=text))
                self.assertEqual((result['domain'], result['eligible']), ('outside', False))

    def test_software_delivery_must_belong_to_the_role_in_a_physical_context(self):
        for text, expected in (
            ('Our software team builds data integrations. You will manage electrical engineering at a manufacturing plant.', 'outside'),
            ('You will not develop software. Manage mechanical engineering and plant maintenance.', 'outside'),
            ('You will develop software for electrical engineering customers.', 'software'),
            ('Drive software engineering teams delivering software for manufacturing plants.', 'software'),
            ('Build software that automates building services maintenance.', 'software'),
            ('Build software testing tools for equipment diagnostics at manufacturing plants.', 'software'),
        ):
            with self.subTest(text=text):
                self.assertEqual(qualification(dict(title='Engineering Manager', text=text))['domain'], expected)

    def test_general_competency_classification_preserves_specialist_qualifiers(self):
        from job_rules import general_competency
        for text in ('Excellent communication skills are essential.',
                     'Strong written and verbal communication skills are required.',
                     'You must have effective communication skills.'):
            with self.subTest(text=text):
                self.assertTrue(general_competency(text))
        for text in ('Communication skills and police-system experience are essential.',
                     'Communication protocols experience is essential.',
                     'German communication skills are required.',
                     'Excellent communication skills in a regulated banking environment are required.'):
            with self.subTest(text=text):
                self.assertFalse(general_competency(text))

    def test_collective_culture_statement_does_not_become_an_essential_skill_gap(self):
        from job_rules import requirements
        clauses, _ = requirements("We care about our work and know it is essential that we be humble.\n"
                                  "Essential requirements: Must have experience in Python.\n"
                                  "You must not have a conflict of interest.")
        essential = [c['text'] for c in clauses if c['importance'] == 'essential']
        self.assertFalse(any('humble' in c for c in essential))
        self.assertTrue(any('Python' in c for c in essential))
        self.assertTrue(any('must not' in c for c in essential))

    def test_not_a_mandatory_checklist_is_not_an_essential_requirement(self):
        from job_rules import requirements
        clauses, _ = requirements('The skills description is an ideal, not a mandatory checklist.')
        self.assertFalse(any(c['importance'] == 'essential' for c in clauses))

    def test_description_permanent_overrides_feed_contract_and_explains_conflict(self):
        for description in (
            "Contract: Full Time, Permanent. Lead our software engineering team.",
            "Engineering Manager. Permanent role. Salary £95,000 per annum.",
        ):
            with self.subTest(description=description):
                job = {"title": "Engineering Manager", "employment_type": "contract", "text": description}
                result = qualification(job)
                self.assertEqual(result["engagement"], "permanent")
                self.assertEqual(engagement_of(job, {"text_markers": ["contract"]}), "permanent")
                self.assertTrue(result["conflicts"])
                self.assertIn("employment_type", result["conflicts"][0])
                self.assertIn("permanent", result["conflicts"][0])

    def test_fixed_term_is_more_specific_than_contract_metadata(self):
        result = qualification({"title": "Head of Quality Engineering", "employment_type": "contract",
                                "text": "Fixed-term (12 months). Lead software testing and automation."})
        self.assertEqual((result["domain"], result["engagement"], result["conflicts"]),
                         ("software", "ftc", []))

    def test_true_day_rate_contract(self):
        result = qualification({"title": "Test Lead", "text": "Software testing. £700 per day outside IR35."})
        self.assertEqual((result["domain"], result["engagement"], result["eligible"]),
                         ("software", "contract", True))

    def test_unstated_terms_and_annual_salary_stay_unknown(self):
        for extras in ({}, {"feed_pay": [150000, 180000, "GBP"]}, {"rate": "£120,000 per year"},
                       {"employment_type": "full-time"}, {"text": "B2B sales, an employment contract and £120,000 salary."}):
            with self.subTest(extras=extras):
                result = qualification({"title": "Engineering Manager", **extras})
                self.assertEqual(result["engagement"], "unknown")
                self.assertTrue(result["eligible"])

    def test_stored_record_fields_use_the_same_classifier(self):
        incoming = qualification({"title": "Engineering Manager", "text": "Permanent role managing HVAC maintenance.",
                                  "employment_type": "contract"})
        stored = qualification({"name": "Engineering Manager", "summary": "Permanent role managing HVAC maintenance.",
                                "engagement": "contract"})
        self.assertEqual((incoming["domain"], incoming["engagement"], incoming["eligible"]),
                         (stored["domain"], stored["engagement"], stored["eligible"]))
        self.assertEqual(stored["domain"], "outside")
        self.assertTrue(stored["conflicts"])

    def test_delimited_permanent_term_in_a_stored_summary_overrides_contract(self):
        result = qualification({"name": "AI Engineering Manager", "engagement": "contract",
                                "summary": "AI Engineering Manager - Permanent - £75-85k - London / Hybrid"})
        self.assertEqual((result["domain"], result["engagement"]), ("software", "permanent"))
        self.assertTrue(result["conflicts"])

    def test_permanent_facilities_role_is_not_a_contract_due_to_client_contract(self):
        result = qualification({"name": "Head of Engineering", "engagement": "contract",
                                "summary": "Advertised a permanent Head of Engineering role on a Central London facilities-management contract."})
        self.assertEqual((result["domain"], result["engagement"], result["eligible"]),
                         ("outside", "permanent", False))
        self.assertTrue(result["conflicts"])

    def test_specific_physical_engineering_roles_are_outside(self):
        for title, description in (
            ("BMS Engineering Manager", "Deliver installation and commissioning of building management systems."),
            ("Engineering Manager", "Lead facilities management and building services maintenance."),
            ("Engineering Manager", "Responsible for HVAC maintenance and electrical systems in commercial buildings."),
            ("Engineering Manager", "Manage mechanical engineering and plant maintenance on a manufacturing site."),
            ("Head of Quality", "Own food safety, HACCP and BRCGS audits at our food manufacturing plant."),
            ("Head of Quality Engineering", "Lead quality engineering and HACCP compliance at a food manufacturing plant."),
        ):
            with self.subTest(title=title, description=description):
                result = qualification({"title": title, "text": description, "department": "Engineering Jobs"})
                self.assertEqual((result["domain"], result["eligible"]), ("outside", False))
                self.assertTrue(result["reasons"])

    def test_software_work_in_physical_industries_is_retained(self):
        for title, description in (
            ("Software Engineer", "Develop building-management software for HVAC and BMS installations."),
            ("Engineering Manager", "Lead a software engineering team building management software for facilities."),
            ("Head of Engineering", "Develop cloud software for industrial manufacturing and mechanical engineering customers."),
            ("Engineering Manager", "Build software that automates building services maintenance."),
            ("Head of Quality Engineering", "Lead software testing for factory automation software."),
        ):
            with self.subTest(title=title, description=description):
                result = qualification({"title": title, "text": description, "department": "Engineering Jobs"})
                self.assertEqual((result["domain"], result["eligible"]), ("software", True))

    def test_leadership_quality_and_ai_roles_are_retained(self):
        for title in ("Interim CTO", "Head of Engineering", "VP Engineering", "Engineering Manager",
                      "Head of Quality Engineering", "AI Evaluation Lead", "Test Lead", "QA Architect"):
            with self.subTest(title=title):
                self.assertTrue(qualification({"title": title})["eligible"])
        self.assertEqual(qualification({"title": "Interim CTO", "employment_type": "contract"})["engagement"], "interim")

    def test_explicit_software_quality_and_release_remits_are_recognised(self):
        for text in ('Permanent Head of Quality Engineering. Lead software release readiness and incident prevention.',
                     'Permanent software quality engineering leadership and release readiness.'):
            with self.subTest(text=text):
                result = qualification({'title': 'Head of Quality Engineering', 'text': text})
                self.assertEqual(result['domain'], 'software')

    def test_software_testing_remits_are_recognised(self):
        # Value gate, 10 October: a stored advert describing SIT, UAT and defect triage was still asked
        # "is this a software-engineering remit?".
        for title, text in [("Programme Test Manager", "Lead SIT and UAT across the programme."),
                            ("Test Lead", "Own system integration testing and defect triage for the release."),
                            ("QA Lead", ""), ("Head of QA", ""), ("SDET", "")]:
            with self.subTest(title=title):
                self.assertEqual(qualification({"title": title, "text": text})["domain"], "software")
        self.assertEqual(qualification({"title": "Site Test Engineer", "text": "Electrical test engineering for HVAC plant."})["domain"],
                         "outside")

    def test_generic_building_and_engineering_category_do_not_exclude(self):
        result = qualification({"title": "Engineering Manager", "text": "Building a high-performing team.",
                                "department": "Engineering Jobs"})
        self.assertEqual((result["domain"], result["eligible"]), ("unknown", True))

    def test_incidental_permanent_team_does_not_override_a_contract(self):
        result = qualification({"title": "Contract Software Engineer", "employment_type": "contract",
                                "text": "Work alongside our permanent team on a six month contract."})
        self.assertEqual(result["engagement"], "contract")
        self.assertEqual(result["conflicts"], [])

    def test_classifier_does_not_mutate_input(self):
        record = {"name": "Engineering Manager", "engagement": "contract", "summary": "Permanent role"}
        original = dict(record)
        qualification(record)
        self.assertEqual(record, original)

    def test_advert_offering_permanent_or_contract_preserves_uncertainty(self):
        result = qualification({"title": "Software Engineer", "employment_type": "contract",
                                "text": "A permanent position or a contract role at £700 per day."})
        self.assertEqual(result["engagement"], "unknown")
        self.assertTrue(result["conflicts"])


class TitleMatcherReuseTests(unittest.TestCase):
    """Ranking, CV routing and intake ask about the same titles on every workspace build."""

    def test_a_title_is_rewritten_once_however_often_it_is_compared(self):
        import job_rules
        job_rules.title_equivalent.cache_clear()
        title = 'Senior Specialist QA (Payments) - Contract'
        first = job_rules.title_equivalent(title)
        self.assertEqual(first, 'quality engineer payments contract')
        for _ in range(3):
            self.assertEqual(job_rules.title_veto(title, ['sales', 'pricing']), '')
            self.assertEqual(job_rules.title_match(title, {'positive': ['quality engineer'], 'negative': ['sales']}),
                             (True, 'title-equivalent:quality engineer'))
        info = job_rules.title_equivalent.cache_info()
        self.assertEqual(info.misses, 1)
        self.assertGreaterEqual(info.hits, 9)
        self.assertEqual(job_rules.title_equivalent('Manager QA'), 'qa manager')
        self.assertEqual(job_rules.title_equivalent.cache_info().misses, 2)

    def test_ranking_and_intake_share_one_word_matcher(self):
        import job_rules
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'workspace'))
        import relevance
        self.assertIs(relevance.word, job_rules.word)
        self.assertIs(job_rules._word, job_rules.word)
        self.assertTrue(job_rules.word('CTO', 'interim cto'))
        self.assertFalse(job_rules.word('CTO', 'director'))


class QualificationReuseTests(unittest.TestCase):
    """Every workspace build qualifies each advert; an unchanged advert reuses its verdict."""

    def setUp(self):
        import job_rules
        self.rules = job_rules
        job_rules._QUALIFIED.clear()

    def test_an_unchanged_advert_is_qualified_once_and_each_caller_gets_its_own_copy(self):
        from unittest.mock import patch
        job = {"name": "Engineering Manager", "summary": "Contract role, day rate £600.", "location": "London"}
        with patch.object(self.rules, "_qualify", wraps=self.rules._qualify) as qualify:
            first = qualification(dict(job))
            first["reasons"].append("changed by a caller")
            first["conflicts"].append("changed by a caller")
            second = qualification(dict(job))
        self.assertEqual(qualify.call_count, 1)
        self.assertNotIn("changed by a caller", second["reasons"] + second["conflicts"])
        self.assertEqual(second, {**first, "reasons": first["reasons"][:-1], "conflicts": first["conflicts"][:-1]})

    def test_each_input_that_decides_the_verdict_is_part_of_its_identity(self):
        base = {"title": "Engineering Manager", "text": "Lead the software team."}
        variants = {"title": {"title": "Head of Plant Maintenance"},
                    "text": {"text": "Permanent role leading the software team."},
                    "employment_type": {"employment_type": "contract"},
                    "engagement": {"engagement": "interim"},
                    "location": {"location": "London; Fixed-term"}}
        expected = {field: self.rules._qualify({**base, **change}) for field, change in variants.items()}
        qualification(dict(base))
        for field, change in variants.items():
            with self.subTest(field=field):
                self.assertNotEqual(expected[field], self.rules._qualify(dict(base)))
                self.assertEqual(qualification({**base, **change}), expected[field])

    def test_requirement_cache_is_byte_bounded_and_safe_for_concurrent_callers(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        text = 'Essential requirements:\n' + '\n'.join('Experience with ' + str(i) + 'x' * 350 for i in range(32))
        with patch.object(self.rules, '_QUALIFIED_MAX_BYTES', 1200):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda i: qualification({'title': str(i), 'text': text}), range(12)))
            self.assertTrue(all(len(item['requirements']) == 32 for item in results))
            self.assertLessEqual(self.rules._QUALIFIED_BYTES, 1200)
            self.assertLessEqual(sum(len(value) for value in self.rules._QUALIFIED.values()), 1200)
            results[0]['requirements'][0]['text'] = 'Caller mutation'
            self.assertNotEqual(results[0], results[1])

    def test_the_cache_is_bounded(self):
        from unittest.mock import patch
        with patch.object(self.rules, "_QUALIFIED_MAX", 3):
            for i in range(10):
                qualification({"title": f"Engineering Manager {i}"})
            self.assertLessEqual(len(self.rules._QUALIFIED), 3)

class RequirementEvidenceTests(unittest.TestCase):
    def test_application_admin_guidance_is_not_a_candidate_requirement(self):
        for text in (
            'If you have a preferred name, please enter it here.',
            'If you have a preferred name, please use it to apply.',
            'Please use your preferred name when completing this form.',
            'Email address is required.',
            'You must provide your email address.',
            'You must upload your CV.',
            'A resume is required.',
            'All fields marked with an asterisk are required.',
            'Please enter your name in this required field.',
            'Your full legal name is required to complete this form.',
        ):
            with self.subTest(text=text):
                self.assertEqual(qualification({'text': text})['requirements'], [])

    def test_application_sections_reset_inherited_requirement_importance(self):
        for heading in ('Application form:', 'How to apply:', 'Apply for this job:', 'Apply now:',
                        'Application form', 'Application', 'How to apply', 'Apply for this job', 'Apply now'):
            for separator in ('\n', ' '):
                with self.subTest(heading=heading, separator=repr(separator)):
                    text = separator.join(('Essential requirements:', 'Experience with Python.',
                                           heading, 'Full name', 'Email address', 'Upload CV'))
                    items = qualification({'text': text})['requirements']
                    self.assertEqual([(item['text'], item['importance']) for item in items],
                                     [('Experience with Python.', 'essential')])

    def test_application_questions_preserve_skills_eligibility_and_qualifiers(self):
        text = ('Essential requirements:\nExperience with Python.\nApplication form:\n'
                'Do you have Kubernetes experience?\nAre you legally authorised to work in the UK?\n'
                'Do you require visa sponsorship?\nYou must have the right to work in the UK.\n'
                'A degree is not required.\nEither C++ or Rust is required.\n'
                'Security clearance is mandatory.\n'
                'Experience building email upload systems is required.\n'
                'You must provide your email address and have the right to work in the UK.')
        items = qualification({'text': text})['requirements']
        self.assertEqual([(item['text'], item['importance'], item['negated']) for item in items], [
            ('Experience with Python.', 'essential', False),
            ('Do you have Kubernetes experience?', 'ambiguous', False),
            ('Are you legally authorised to work in the UK?', 'ambiguous', False),
            ('Do you require visa sponsorship?', 'ambiguous', False),
            ('You must have the right to work in the UK.', 'essential', False),
            ('A degree is not required.', 'ambiguous', True),
            ('Either C++ or Rust is required.', 'essential', False),
            ('Security clearance is mandatory.', 'essential', False),
            ('Experience building email upload systems is required.', 'essential', False),
            ('You must provide your email address and have the right to work in the UK.', 'essential', False),
        ])
        self.assertEqual(items[6]['alternatives'], ['C++', 'Rust'])
        self.assertEqual(qualification({'text': 'How do we share knowledge?'})['requirements'], [])

    def test_admin_fields_do_not_fill_the_bounded_candidate_requirements(self):
        text = '\n'.join(f'Your email address is required to complete this form section {n}.' for n in range(60))
        result = qualification({'text': text + '\nSecurity clearance is mandatory.'})
        self.assertEqual([(item['text'], item['importance']) for item in result['requirements']],
                         [('Security clearance is mandatory.', 'essential')])
        self.assertFalse(result['requirements_truncated'])

    def test_specialist_requirements_can_mention_form_fields_and_uploads(self):
        for text, importance in (
            ('You must build email upload systems.', 'essential'),
            ('You must provide email infrastructure.', 'essential'),
            ('Preferred name handling is required in identity systems.', 'ambiguous'),
            ('Application form design experience is required.', 'essential'),
            ('Application Form Design experience is required.', 'essential'),
            ('Experience with preferred name fields is required.', 'ambiguous'),
            ('You must upload your CV to complete a security clearance check.', 'essential'),
        ):
            with self.subTest(text=text):
                items = qualification({'text': text})['requirements']
                self.assertEqual([(item['text'], item['importance']) for item in items], [(text, importance)])

    def test_admin_prefix_never_discards_unknown_age_residency_or_upload_conditions(self):
        for text, importance in (
            ('You must provide your phone number, be at least 18 years old and live in the UK.', 'essential'),
            ('Your email address is required along with UK residency.', 'essential'),
            ('You must upload your CV, be over 18 and live in the UK.', 'essential'),
            ('Your resume is required in addition to current UK residence.', 'essential'),
            ('Please enter your name in this required field and be at least 18 years old.', 'essential'),
            ('All fields marked with an asterisk are required along with UK residency.', 'essential'),
        ):
            with self.subTest(text=text):
                items = qualification({'text': text})['requirements']
                self.assertEqual([(item['text'], item['importance']) for item in items], [(text, importance)])
        # Existing clause splitting keeps independently marked importance separate.
        items = qualification({'text': 'If you have a preferred name, you must be at least 18.'})['requirements']
        self.assertEqual([(item['text'], item['importance']) for item in items], [
            ('If you have a preferred name', 'desirable'), ('you must be at least 18.', 'essential')])

    def test_flattened_explicit_heading_still_marks_requirements_essential(self):
        text = 'Improve release readiness. Minimum requirements Police-system experience Benefits Flexible working.'
        result = qualification({'text': text})
        self.assertEqual([(item['text'], item['importance']) for item in result['requirements']],
                         [('Police-system experience', 'essential')])

    def test_flattened_sections_do_not_turn_duties_or_perks_into_requirements(self):
        text = 'Minimum requirements Test automation Responsibilities Build data integrations Benefits Flexible working.'
        result = qualification({'text': text})
        self.assertEqual([(item['text'], item['importance']) for item in result['requirements']],
                         [('Test automation', 'essential')])

    def test_mixed_importance_keeps_the_independent_essential_requirement(self):
        items = qualification({'text': 'Police-system experience is essential and Python is preferred.'})['requirements']
        self.assertEqual([(item['text'], item['importance']) for item in items],
                         [('Police-system experience is essential', 'essential'), ('Python is preferred.', 'desirable')])

    def test_independent_conjunction_does_not_become_one_alternative(self):
        items = qualification({'text': 'Police systems experience is required and either data integration or release readiness is required.'})['requirements']
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['alternatives'], [])
        self.assertEqual(items[1]['alternatives'], ['data integration', 'release readiness'])

    def test_unrecognised_mixed_and_or_wording_is_not_flattened(self):
        items = qualification({'text': 'Police systems and data integration or release readiness are required.'})['requirements']
        self.assertEqual(items[0]['importance'], 'essential')
        self.assertEqual(items[0]['alternatives'], [])

    def test_negation_does_not_erase_a_separate_essential_clause(self):
        result = qualification({'text': 'Python is not required but Java is essential.\nYou do not need Kubernetes.'})
        items = result['requirements']
        self.assertEqual([(x['importance'], x['negated']) for x in items],
                         [('ambiguous', True), ('essential', False), ('ambiguous', True)])
        self.assertEqual(items[1]['text'], 'Java is essential.')

    def test_prohibition_is_preserved_as_an_unestablished_constraint(self):
        item = qualification({'text': 'You must not have a conflict of interest.'})['requirements'][0]
        self.assertEqual(item['importance'], 'essential')
        self.assertTrue(item['negated'])
        self.assertFalse(item['waived'])

    def test_long_source_does_not_silently_hide_unknown_requirements(self):
        result = qualification({'text': 'Lead software engineering. ' * 1000 + 'Police systems required.'})
        self.assertTrue(result['requirements_truncated'])

    def test_explicit_headings_and_inline_markers_preserve_verbatim_clauses(self):
        text = ('Essential requirements:\nExperience leading engineering teams.\nPolice-system experience is mandatory.\n'
                'Desirable:\nExperience with Kubernetes.\nResponsibilities:\nImprove release readiness.')
        result = qualification(dict(title='Engineering Manager', text=text))
        items = {x['text']: x for x in result['requirements']}
        self.assertEqual(items['Experience leading engineering teams.']['importance'], 'essential')
        self.assertEqual(items['Police-system experience is mandatory.']['importance'], 'essential')
        self.assertEqual(items['Experience with Kubernetes.']['importance'], 'desirable')
        self.assertNotIn('Improve release readiness.', items)

    def test_alternatives_negation_and_unspecified_tech_are_not_mandatory(self):
        text = ('Experience with Python or Java is required.\nKubernetes experience is not required.\n'
                'Experience with Go would be useful.\nOur team uses Rust.')
        items = {x['text']: x for x in qualification(dict(title='Software Engineer', text=text))['requirements']}
        self.assertEqual(items['Experience with Python or Java is required.']['alternatives'], ['Python', 'Java'])
        self.assertTrue(items['Kubernetes experience is not required.']['negated'])
        self.assertEqual(items['Experience with Go would be useful.']['importance'], 'desirable')
        self.assertNotIn('Our team uses Rust.', items)

    def test_unqualified_requirements_heading_is_ambiguous_and_bounded(self):
        result = qualification(dict(title='Engineering Manager', text='Requirements:\n' + '\n'.join(
            f'Experience with system {i}.' for i in range(60))))
        self.assertEqual(len(result['requirements']), 32)
        self.assertTrue(result['requirements_truncated'])
        self.assertTrue(all(x['importance'] == 'ambiguous' for x in result['requirements']))

    def test_cache_preserves_quote_case_and_does_not_share_mutable_requirements(self):
        item = dict(title='Engineering Manager', text='Must have API integration experience.')
        first = qualification(item)
        first['requirements'][0]['alternatives'].append('invented')
        first['requirements'][0]['text'] = 'invented'
        again = qualification(item)
        self.assertEqual(again['requirements'][0]['text'], item['text'])
        self.assertEqual(again['requirements'][0]['alternatives'], [])
        lower = qualification(dict(item, text=item['text'].lower()))
        self.assertEqual(lower['requirements'][0]['text'], item['text'].lower())


class ConstructionValueEngineeringTests(unittest.TestCase):
    def test_construction_value_engineering_is_not_software_leadership(self):
        result = qualification(dict(title='Value Engineering Manager, Value Engineering',
            text='Lead value engineering and construction cost optimisation for data centre buildings.'))
        self.assertEqual((result['domain'], result['eligible']), ('outside', False))

    def test_software_role_at_construction_employer_keeps_software_qualification(self):
        result = qualification(dict(title='Software Engineering Manager',
            text='Build software platforms for construction cost optimisation and value engineering.'))
        self.assertEqual((result['domain'], result['eligible']), ('software', True))


class IntakeTests(unittest.TestCase):
    def test_only_demonstrated_pay_conflicts_are_rejected(self):
        from job_rules import intake_rejection
        portals = {'title_filter': {}, 'location_filter': {}}
        pay = {'floor_day_gbp': 500, 'floor_year_gbp': 70000}
        cases = [({}, ''), ({'text': 'Salary from £60,000 per year.'}, ''),
                 ({'text': '£60,000+ per year, negotiable.'}, ''),
                 ({'text': 'Day rate starting at £400 per day.'}, ''), ({'pay_min': '40000', 'pay_currency': 'GBP', 'pay_period': 'year'}, ''),
                 ({'pay_max': '0', 'pay_currency': 'GBP', 'pay_period': 'year'}, ''),
                 ({'pay_max': '60000', 'pay_currency': 'USD', 'pay_period': 'year'}, ''),
                 ({'pay_max': '60000', 'pay_currency': 'GBP', 'pay_period': ''}, ''),
                 ({'text': 'Salary £50,000–£65,000 per year.'}, 'below-pay-floor'),
                 ({'text': 'Salary £60,000–£90,000 per year.'}, ''),
                 ({'pay': [400, 450, 'GBP', 'day']}, 'below-pay-floor'),
                 ({'pay': [450, 500, 'GBP', 'day']}, '')]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                self.assertEqual(intake_rejection({'title': 'Engineering Manager', **fields}, portals, pay), expected)

    def test_expiry_requires_a_valid_explicit_past_date(self):
        import datetime as dt
        from job_rules import intake_rejection
        for expiry, expected in [('2026-10-28', 'source-expired'), ('2026-10-29', ''), ('unknown', ''), ('', '')]:
            self.assertEqual(intake_rejection({'expires': expiry}, {}, {}, today=dt.date(2026, 10, 29)), expected)


class SubstantiveDutyTests(unittest.TestCase):
    def test_substantive_work_needs_an_owned_duty_and_specific_work_not_promotional_copy(self):
        from job_rules import substantive_role_action
        for text in ('Lead two software teams and improve release reliability.',
                     'You will lead software engineering teams.',
                     'Build software services for our payments platform.',
                     'Grow software engineering teams.','Develop software applications.',
                     'Manage mechanical engineering maintenance.'):
            with self.subTest(text=text):self.assertTrue(substantive_role_action(text))
        for text in ('Build a career you can be proud of.','Build a career in software engineering.',
                     'Grow your software engineering career with us.',
                     'Develop as a software engineer in a supportive workplace.',
                     'Improve your software engineering skills.',
                     'Grow into a software engineering role.',
                     'Develop within our supportive software engineering team.',
                     'Engineering Manager','Our advertised title is Engineering Manager.',
                     'Pay: GBP 101600–152400/year','You will not lead software engineering teams.',
                     'Another team will build software services.','You might lead software engineering teams.'):
            with self.subTest(text=text):self.assertEqual(substantive_role_action(text),'')


if __name__ == '__main__':
    unittest.main()
